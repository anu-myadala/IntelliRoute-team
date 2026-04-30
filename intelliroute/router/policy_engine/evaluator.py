"""Composable control-plane rules applied before multi-objective ranking."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ...common.models import (
    BrownoutStatus,
    CompletionRequest,
    Intent,
    PolicyEvaluationResult,
    ProviderInfo,
)
from .complexity import ComplexityResult, compute_complexity
from .config import PolicyEngineConfig


class PolicyEvaluator:
    """Runs ordered rules that mutate a block set and attach audit metadata."""

    def __init__(self, config: PolicyEngineConfig | None = None) -> None:
        self._config = config or PolicyEngineConfig.from_env()
        self._rules = (
            self._rule_batch_avoids_premium,
            self._rule_premium_requires_reasoning_or_complexity,
            self._rule_budget_downgrades_premium,
            self._rule_team_budget_controls,
            self._rule_workflow_budget_controls,
            self._rule_brownout_degradation,
            self._rule_interactive_latency_gate,
        )

    def evaluate(
        self,
        providers: Sequence[ProviderInfo],
        intent: Intent,
        request: CompletionRequest,
        *,
        tenant_budget_usd: float | None,
        tenant_spent_usd: float,
        team_id: str | None = None,
        workflow_id: str | None = None,
        team_budget_usd: float | None = None,
        team_spent_usd: float = 0.0,
        workflow_budget_usd: float | None = None,
        workflow_spent_usd: float = 0.0,
        team_premium_cap_usd: float | None = None,
        team_premium_spend_usd: float = 0.0,
        brownout_status: BrownoutStatus | None = None,
        brownout_max_latency_ms: int | None = None,
        brownout_block_premium: bool = True,
        brownout_prefer_low_latency: bool = True,
    ) -> tuple[list[ProviderInfo], PolicyEvaluationResult]:
        if not self._config.enabled or not providers:
            names = [p.name for p in providers]
            return list(providers), PolicyEvaluationResult(
                complexity_score=0.0,
                complexity_signals=[],
                allowed_providers=names,
                blocked_providers=[],
                matched_rules=[],
                downgrade_reason=None,
                fail_open=False,
            )

        complexity = compute_complexity(request)
        blocked: set[str] = set()
        matched: list[str] = []
        budget_actions: list[dict[str, str]] = []
        downgrade_reason: str | None = None

        ctx = _RuleContext(
            providers=tuple(providers),
            intent=intent,
            request=request,
            complexity=complexity,
            tenant_budget_usd=tenant_budget_usd,
            tenant_spent_usd=tenant_spent_usd,
            team_id=team_id,
            workflow_id=workflow_id,
            team_budget_usd=team_budget_usd,
            team_spent_usd=team_spent_usd,
            workflow_budget_usd=workflow_budget_usd,
            workflow_spent_usd=workflow_spent_usd,
            team_premium_cap_usd=team_premium_cap_usd,
            team_premium_spend_usd=team_premium_spend_usd,
            brownout_status=brownout_status,
            brownout_max_latency_ms=brownout_max_latency_ms,
            brownout_block_premium=brownout_block_premium,
            brownout_prefer_low_latency=brownout_prefer_low_latency,
            blocked=blocked,
            matched=matched,
            budget_actions=budget_actions,
            downgrade_reason_holder=[downgrade_reason],
            config=self._config,
        )

        for rule in self._rules:
            rule(ctx)
        downgrade_reason = ctx.downgrade_reason_holder[0]

        all_names = [p.name for p in providers]
        allowed = [p for p in providers if p.name not in blocked]
        fail_open = False
        if not allowed:
            fail_open = True
            allowed = list(providers)
            matched.append("fail_open_restore_full_provider_set")

        return allowed, PolicyEvaluationResult(
            complexity_score=complexity.score,
            complexity_signals=list(complexity.signals),
            allowed_providers=[p.name for p in allowed],
            blocked_providers=sorted(blocked),
            matched_rules=matched,
            budget_actions=budget_actions,
            downgrade_reason=downgrade_reason,
            fail_open=fail_open,
        )

    def _is_premium(self, p: ProviderInfo) -> bool:
        if p.name in self._config.premium_provider_names:
            return True
        return False

    def _rule_batch_avoids_premium(self, ctx: "_RuleContext") -> None:
        if ctx.intent != Intent.BATCH:
            return
        touched = False
        for p in ctx.providers:
            if self._is_premium(p):
                ctx.blocked.add(p.name)
                touched = True
        if touched:
            ctx.matched.append("batch_avoids_premium")

    def _rule_premium_requires_reasoning_or_complexity(self, ctx: "_RuleContext") -> None:
        if ctx.intent == Intent.REASONING:
            return
        if ctx.complexity.score >= self._config.complexity_threshold_premium:
            return
        touched = False
        for p in ctx.providers:
            if self._is_premium(p):
                ctx.blocked.add(p.name)
                touched = True
        if touched:
            ctx.matched.append("premium_requires_reasoning_or_high_complexity")

    def _rule_budget_downgrades_premium(self, ctx: "_RuleContext") -> None:
        budget = ctx.tenant_budget_usd
        if budget is None or budget <= 0:
            return
        util = ctx.tenant_spent_usd / budget
        if util < self._config.budget_utilization_downgrade:
            return
        touched = False
        for p in ctx.providers:
            if self._is_premium(p):
                ctx.blocked.add(p.name)
                touched = True
        if touched:
            ctx.matched.append("tenant_budget_pressure_blocks_premium")
            ctx.downgrade_reason_holder[0] = (
                f"budget_utilization {util:.2f} >= "
                f"{self._config.budget_utilization_downgrade:.2f}"
            )
            ctx.budget_actions.append(
                {
                    "scope": "tenant",
                    "action": "restrict_premium",
                    "reason": "budget_pressure",
                    "status": f"{util:.2f}",
                }
            )

    def _rule_team_budget_controls(self, ctx: "_RuleContext") -> None:
        if not ctx.team_id:
            return
        touched = False
        budget = ctx.team_budget_usd
        if budget is not None and budget > 0:
            util = ctx.team_spent_usd / budget
            if util >= ctx.config.team_budget_utilization_downgrade:
                for p in ctx.providers:
                    if self._is_premium(p):
                        ctx.blocked.add(p.name)
                        touched = True
                if touched:
                    ctx.matched.append("team_budget_pressure_blocks_premium")
                    ctx.budget_actions.append(
                        {
                            "scope": "team",
                            "action": "restrict_premium",
                            "reason": "budget_pressure",
                            "status": f"{util:.2f}",
                        }
                    )
                    if ctx.downgrade_reason_holder[0] is None:
                        ctx.downgrade_reason_holder[0] = (
                            f"team_budget_utilization {util:.2f} >= "
                            f"{ctx.config.team_budget_utilization_downgrade:.2f}"
                        )
        cap = ctx.team_premium_cap_usd
        if cap is not None and cap > 0:
            cap_util = ctx.team_premium_spend_usd / cap
            if cap_util >= ctx.config.team_premium_cap_utilization:
                cap_touched = False
                for p in ctx.providers:
                    if self._is_premium(p):
                        ctx.blocked.add(p.name)
                        cap_touched = True
                if cap_touched:
                    ctx.matched.append("team_premium_cap_reached")
                    ctx.budget_actions.append(
                        {
                            "scope": "team",
                            "action": "premium_cap_hit",
                            "reason": "premium_cap",
                            "status": f"{cap_util:.2f}",
                        }
                    )

    def _rule_workflow_budget_controls(self, ctx: "_RuleContext") -> None:
        if not ctx.workflow_id:
            return
        budget = ctx.workflow_budget_usd
        if budget is None or budget <= 0:
            return
        util = ctx.workflow_spent_usd / budget
        if util < ctx.config.workflow_budget_utilization_downgrade:
            return
        touched = False
        for p in ctx.providers:
            if self._is_premium(p):
                ctx.blocked.add(p.name)
                touched = True
            if ctx.intent == Intent.BATCH and p.cost_per_1k_tokens > 0.005:
                ctx.blocked.add(p.name)
                touched = True
        if touched:
            ctx.matched.append("workflow_budget_pressure_cost_optimize")
            ctx.budget_actions.append(
                {
                    "scope": "workflow",
                    "action": "prefer_cheaper",
                    "reason": "budget_pressure",
                    "status": f"{util:.2f}",
                }
            )

    def _rule_interactive_latency_gate(self, ctx: "_RuleContext") -> None:
        if not self._config.apply_interactive_latency_gate:
            return
        if ctx.intent != Intent.INTERACTIVE:
            return
        # High-complexity interactive prompts may use slower providers.
        if ctx.complexity.score >= self._config.complexity_threshold_premium:
            return
        max_ms = self._config.interactive_max_latency_ms
        gated: list[str] = []
        for p in ctx.providers:
            if p.typical_latency_ms > max_ms:
                ctx.blocked.add(p.name)
                gated.append(p.name)
        if gated:
            ctx.matched.append(f"interactive_latency_gate_max_ms={max_ms}")

    def _rule_brownout_degradation(self, ctx: "_RuleContext") -> None:
        bs = ctx.brownout_status
        if bs is None or not bs.is_degraded:
            return
        if ctx.intent in {Intent.INTERACTIVE, Intent.CODE}:
            return

        touched = False
        if ctx.brownout_block_premium:
            for p in ctx.providers:
                if self._is_premium(p):
                    ctx.blocked.add(p.name)
                    touched = True
        if ctx.brownout_prefer_low_latency and ctx.brownout_max_latency_ms is not None:
            for p in ctx.providers:
                if p.typical_latency_ms > ctx.brownout_max_latency_ms:
                    ctx.blocked.add(p.name)
                    touched = True
        if touched:
            ctx.matched.append("brownout_degrade_low_priority_routing")
            if ctx.downgrade_reason_holder[0] is None:
                ctx.downgrade_reason_holder[0] = (
                    f"brownout_active reason={bs.reason}"
                )


@dataclass
class _RuleContext:
    providers: tuple[ProviderInfo, ...]
    intent: Intent
    request: CompletionRequest
    complexity: ComplexityResult
    tenant_budget_usd: float | None
    tenant_spent_usd: float
    team_id: str | None
    workflow_id: str | None
    team_budget_usd: float | None
    team_spent_usd: float
    workflow_budget_usd: float | None
    workflow_spent_usd: float
    team_premium_cap_usd: float | None
    team_premium_spend_usd: float
    brownout_status: BrownoutStatus | None
    brownout_max_latency_ms: int | None
    brownout_block_premium: bool
    brownout_prefer_low_latency: bool
    blocked: set[str]
    matched: list[str]
    budget_actions: list[dict[str, str]]
    downgrade_reason_holder: list[str | None]
    config: PolicyEngineConfig
