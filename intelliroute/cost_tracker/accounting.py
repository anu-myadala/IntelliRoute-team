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

# Maximum number of raw cost events to retain in the in-memory audit log.
# Once the cap is reached the oldest entry is evicted (FIFO) to prevent
# unbounded memory growth during long-running demo or test sessions.
# In production this would be an append-only durable log (e.g. Kafka),
# so the cap is intentionally generous at 10 000 — enough for hours of
# demo traffic without any risk of OOM.
_MAX_EVENT_HISTORY: int = 10_000


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
    def __init__(
        self,
        budgets: dict[str, float] | None = None,
        max_history: int = _MAX_EVENT_HISTORY,
    ) -> None:
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
        # Append-only audit log of raw CostEvent objects, bounded by max_history.
        # Provides a queryable record of every completion that incurred cost,
        # enabling the /history endpoint to replay or filter events post-hoc.
        self._event_log: list[CostEvent] = []
        self._max_history: int = max_history
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
            # Append to the audit log first so the event is always present even
            # if the rollup update raises an unexpected exception.
            self._event_log.append(event)
            # Evict the oldest entry when the cap is reached so memory stays bounded.
            if len(self._event_log) > self._max_history:
                del self._event_log[0]
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

    def event_history(
        self,
        tenant_id: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CostEvent]:
        """Return a paginated slice of the raw cost-event audit log.

        Events are ordered oldest-first, matching the order they arrived at
        the service. This ordering allows callers to paginate forward in time
        by incrementing ``offset`` by ``limit`` on each successive request.

        Parameters
        ----------
        tenant_id:
            When supplied, only events whose ``tenant_id`` matches are
            included. Pass ``None`` to retrieve events across all tenants
            (useful for operator dashboards).
        limit:
            Maximum number of events to return per page (default: 100).
            The actual count may be less if the log has fewer entries.
        offset:
            Zero-based start index into the filtered event list (default: 0).
            Combine with ``limit`` for pagination.
        """
        with self._lock:
            if tenant_id is not None:
                # Filter inside the lock so we hold a consistent snapshot.
                events = [e for e in self._event_log if e.tenant_id == tenant_id]
            else:
                events = list(self._event_log)
        # Slice outside the lock — we hold an independent copy at this point.
        return events[offset : offset + limit]

    def total_event_count(self, tenant_id: str | None = None) -> int:
        """Return the total number of events in the log, optionally filtered by tenant.

        Cheaper than event_history() when the caller only needs the count for
        pagination metadata (e.g. to compute the ``total`` field in a response).
        """
        with self._lock:
            if tenant_id is not None:
                return sum(1 for e in self._event_log if e.tenant_id == tenant_id)
            return len(self._event_log)

    def reset(self, *, clear_budgets: bool = True) -> None:
        """Clear in-memory rollups/alerts for isolated experiment runs."""
        with self._lock:
            self._rollups.clear()
            self._team_rollups.clear()
            self._workflow_rollups.clear()
            self._alerts.clear()
            self._crossed.clear()
            self._team_premium_spend.clear()
            # Always clear the event log on reset so history doesn't bleed
            # across test scenarios.
            self._event_log.clear()
            if clear_budgets:
                self._budgets.clear()
                self._team_budgets.clear()
                self._workflow_budgets.clear()
                self._team_premium_caps.clear()
