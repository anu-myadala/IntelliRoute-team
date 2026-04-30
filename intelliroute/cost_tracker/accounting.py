"""In-memory cost accounting.

This is the eventually-consistent side of the system. Cost events are
published asynchronously from the router after a successful completion;
the cost tracker consumes them and maintains a running aggregate per
tenant.

In a production system this would be backed by a durable append-only log
(Kafka, NATS JetStream) and a columnar store for the rollups. For the
course project we keep it in-memory: the aggregation logic is still
representative and fully tested.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..common.models import CostEvent, CostSummary


@dataclass
class _TenantRollup:
    total_requests: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    by_provider: dict[str, float] = field(default_factory=lambda: defaultdict(float))


@dataclass
class ScopeRollup:
    total_requests: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0


class CostAccountant:
    def __init__(self, budgets: dict[str, float] | None = None) -> None:
        self._rollups: dict[str, _TenantRollup] = defaultdict(_TenantRollup)
        self._team_rollups: dict[str, ScopeRollup] = defaultdict(ScopeRollup)
        self._workflow_rollups: dict[str, ScopeRollup] = defaultdict(ScopeRollup)
        self._budgets: dict[str, float] = dict(budgets or {})
        self._team_budgets: dict[str, float] = {}
        self._workflow_budgets: dict[str, float] = {}
        self._team_premium_caps: dict[str, float] = {}
        self._team_premium_spend: dict[str, float] = defaultdict(float)
        self._alerts: list[str] = []
        # Set of tenants that have already crossed their budget; prevents
        # alert spam once the threshold has been breached.
        self._crossed: set[str] = set()
        self._lock = threading.Lock()

    def set_budget(self, tenant_id: str, budget_usd: float) -> None:
        with self._lock:
            self._budgets[tenant_id] = budget_usd

    def set_team_budget(self, team_id: str, budget_usd: float) -> None:
        with self._lock:
            self._team_budgets[team_id] = budget_usd

    def set_workflow_budget(self, workflow_id: str, budget_usd: float) -> None:
        with self._lock:
            self._workflow_budgets[workflow_id] = budget_usd

    def set_team_premium_cap(self, team_id: str, cap_usd: float) -> None:
        with self._lock:
            self._team_premium_caps[team_id] = cap_usd

    def get_budget(self, tenant_id: str) -> float | None:
        with self._lock:
            b = self._budgets.get(tenant_id)
            return b if b is not None else None

    def get_team_budget(self, team_id: str) -> float | None:
        with self._lock:
            b = self._team_budgets.get(team_id)
            return b if b is not None else None

    def get_workflow_budget(self, workflow_id: str) -> float | None:
        with self._lock:
            b = self._workflow_budgets.get(workflow_id)
            return b if b is not None else None

    def get_team_premium_cap(self, team_id: str) -> float | None:
        with self._lock:
            c = self._team_premium_caps.get(team_id)
            return c if c is not None else None

    @staticmethod
    def _is_premium_provider(provider: str) -> bool:
        p = provider.lower()
        return "smart" in p or "gemini" in p

    def record(self, event: CostEvent) -> None:
        with self._lock:
            r = self._rollups[event.tenant_id]
            r.total_requests += 1
            r.total_tokens += event.prompt_tokens + event.completion_tokens
            r.total_cost_usd += event.estimated_cost_usd
            r.by_provider[event.provider] += event.estimated_cost_usd
            if event.team_id:
                tr = self._team_rollups[event.team_id]
                tr.total_requests += 1
                tr.total_tokens += event.prompt_tokens + event.completion_tokens
                tr.total_cost_usd += event.estimated_cost_usd
                if self._is_premium_provider(event.provider):
                    self._team_premium_spend[event.team_id] += event.estimated_cost_usd
            if event.workflow_id:
                wr = self._workflow_rollups[event.workflow_id]
                wr.total_requests += 1
                wr.total_tokens += event.prompt_tokens + event.completion_tokens
                wr.total_cost_usd += event.estimated_cost_usd

            # Budget alerting: fire exactly once per tenant per crossing.
            budget = self._budgets.get(event.tenant_id)
            if (
                budget is not None
                and r.total_cost_usd > budget
                and event.tenant_id not in self._crossed
            ):
                self._crossed.add(event.tenant_id)
                self._alerts.append(
                    f"BUDGET_EXCEEDED tenant={event.tenant_id} "
                    f"spent={r.total_cost_usd:.4f} budget={budget:.4f}"
                )

    def summary(self, tenant_id: str) -> CostSummary:
        with self._lock:
            r = self._rollups.get(tenant_id)
            if r is None:
                return CostSummary(
                    tenant_id=tenant_id,
                    total_requests=0,
                    total_tokens=0,
                    total_cost_usd=0.0,
                    by_provider={},
                )
            return CostSummary(
                tenant_id=tenant_id,
                total_requests=r.total_requests,
                total_tokens=r.total_tokens,
                total_cost_usd=round(r.total_cost_usd, 6),
                by_provider={k: round(v, 6) for k, v in r.by_provider.items()},
            )

    def alerts(self) -> list[str]:
        with self._lock:
            return list(self._alerts)

    @staticmethod
    def _summary_from_rollup(scope_id: str, r: ScopeRollup) -> dict:
        return {
            "id": scope_id,
            "total_requests": r.total_requests,
            "total_tokens": r.total_tokens,
            "total_cost_usd": round(r.total_cost_usd, 6),
        }

    def team_summary(self, team_id: str) -> dict:
        with self._lock:
            r = self._team_rollups.get(team_id, ScopeRollup())
            return self._summary_from_rollup(team_id, r)

    def workflow_summary(self, workflow_id: str) -> dict:
        with self._lock:
            r = self._workflow_rollups.get(workflow_id, ScopeRollup())
            return self._summary_from_rollup(workflow_id, r)

    def team_summaries(self) -> list[dict]:
        with self._lock:
            return [self._summary_from_rollup(k, v) for k, v in self._team_rollups.items()]

    def workflow_summaries(self) -> list[dict]:
        with self._lock:
            return [
                self._summary_from_rollup(k, v)
                for k, v in self._workflow_rollups.items()
            ]

    def team_budget_status(self, team_id: str) -> dict:
        with self._lock:
            spent = self._team_rollups.get(team_id, ScopeRollup()).total_cost_usd
            budget = self._team_budgets.get(team_id)
            cap = self._team_premium_caps.get(team_id)
            premium_spend = self._team_premium_spend.get(team_id, 0.0)
            util = None if budget in (None, 0) else spent / budget
            cap_hit = cap is not None and premium_spend >= cap
            return {
                "team_id": team_id,
                "spent_usd": round(spent, 6),
                "budget_usd": budget,
                "utilization": None if util is None else round(util, 4),
                "premium_spend_usd": round(premium_spend, 6),
                "premium_cap_usd": cap,
                "premium_cap_hit": cap_hit,
            }

    def workflow_budget_status(self, workflow_id: str) -> dict:
        with self._lock:
            spent = self._workflow_rollups.get(workflow_id, ScopeRollup()).total_cost_usd
            budget = self._workflow_budgets.get(workflow_id)
            util = None if budget in (None, 0) else spent / budget
            return {
                "workflow_id": workflow_id,
                "spent_usd": round(spent, 6),
                "budget_usd": budget,
                "utilization": None if util is None else round(util, 4),
                "budget_pressure": bool(util is not None and util >= 0.85),
            }

    def team_budgets(self) -> dict[str, float]:
        with self._lock:
            return dict(self._team_budgets)

    def workflow_budgets(self) -> dict[str, float]:
        with self._lock:
            return dict(self._workflow_budgets)

    def headroom(self, tenant_id: str) -> Optional[float]:
        """Return remaining USD budget, or ``None`` if no budget is set.

        Negative headroom means the tenant has already exceeded its budget.
        """
        with self._lock:
            budget = self._budgets.get(tenant_id)
            if budget is None:
                return None
            spent = self._rollups[tenant_id].total_cost_usd if tenant_id in self._rollups else 0.0
            return budget - spent

    def would_exceed(self, tenant_id: str, projected_cost_usd: float) -> bool:
        """Is the projected cost large enough to push spend past the budget?

        Returns False when no budget is set (unbounded), otherwise returns
        True iff (current_spend + projected) exceeds the budget. Used by the
        router's pre-call gate to demote toward a cheaper provider.
        """
        h = self.headroom(tenant_id)
        if h is None:
            return False
        return projected_cost_usd > h
