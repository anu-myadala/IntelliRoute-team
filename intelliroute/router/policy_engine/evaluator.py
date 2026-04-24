"""Composable control-plane rules applied before multi-objective ranking."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ...common.models import (
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
        downgrade_reason: str | None = None

        ctx = _RuleContext(
            providers=tuple(providers),
            intent=intent,
            request=request,
            complexity=complexity,
            tenant_budget_usd=tenant_budget_usd,
            tenant_spent_usd=tenant_spent_usd,
            blocked=blocked,
            matched=matched,
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


@dataclass
class _RuleContext:
    providers: tuple[ProviderInfo, ...]
    intent: Intent
    request: CompletionRequest
    complexity: ComplexityResult
    tenant_budget_usd: float | None
    tenant_spent_usd: float
    blocked: set[str]
    matched: list[str]
    downgrade_reason_holder: list[str | None]
    config: PolicyEngineConfig
