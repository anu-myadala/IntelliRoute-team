"""Multi-objective routing policy.

Given a set of candidate providers, the current health snapshot, and the
inferred intent of a request, ``RoutingPolicy.rank`` returns the providers
in the order they should be tried.

Scoring model
-------------
For every candidate provider we compute four normalised sub-scores in
``[0, 1]`` (higher is better) and combine them into a weighted score. The
weights depend on the intent:

* INTERACTIVE: latency dominates, then capability.
* REASONING:   capability and historical success dominate; cost matters
                least because users paying for reasoning expect quality.
* BATCH:       cost dominates; latency barely matters.
* CODE:        capability first, then latency.

Providers that are currently circuit-broken are filtered out unless the
policy is explicitly asked for a fallback list, in which case they are
demoted to the end.

The policy is deterministic and side-effect free, which makes it
straightforward to unit test against a fixed fixture.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..common.models import Intent, ProviderHealth, ProviderInfo
from .feedback import FeedbackCollector


@dataclass
class Weights:
    latency: float
    cost: float
    capability: float
    success: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.latency, self.cost, self.capability, self.success)


INTENT_WEIGHTS: dict[Intent, Weights] = {
    Intent.INTERACTIVE: Weights(latency=0.55, cost=0.15, capability=0.20, success=0.10),
    Intent.REASONING:   Weights(latency=0.10, cost=0.10, capability=0.50, success=0.30),
    Intent.BATCH:       Weights(latency=0.05, cost=0.65, capability=0.15, success=0.15),
    Intent.CODE:        Weights(latency=0.25, cost=0.10, capability=0.45, success=0.20),
}


def _normalize_latency(latency_ms: float, worst_ms: float) -> float:
    # Lower is better; invert and clamp.
    if worst_ms <= 0:
        return 1.0
    return max(0.0, 1.0 - min(latency_ms, worst_ms) / worst_ms)


def _normalize_cost(cost: float, worst_cost: float) -> float:
    if worst_cost <= 0:
        return 1.0
    return max(0.0, 1.0 - min(cost, worst_cost) / worst_cost)


@dataclass
class ScoredProvider:
    provider: ProviderInfo
    score: float
    sub_scores: dict[str, float]


class RoutingPolicy:
    def __init__(
        self,
        intent_weights: dict[Intent, Weights] | None = None,
        feedback: Optional[FeedbackCollector] = None,
    ) -> None:
        self._weights = intent_weights or INTENT_WEIGHTS
        self._feedback = feedback

    def rank(
        self,
        providers: list[ProviderInfo],
        health: dict[str, ProviderHealth],
        intent: Intent,
        latency_budget_ms: int | None = None,
<<<<<<< HEAD
=======
        confidence_hint: float | None = None,
        premium_threshold: float = 0.7,
>>>>>>> 2b788c2948bcc409fd824497816e061092d81ec0
    ) -> list[ScoredProvider]:
        if not providers:
            return []

        # Filter out providers whose circuit is currently OPEN.
        usable: list[ProviderInfo] = []
        fallback: list[ProviderInfo] = []
        for p in providers:
            h = health.get(p.name)
            if h is not None and h.circuit_state == "open":
                fallback.append(p)
            else:
                usable.append(p)

        if not usable:
            # Every provider is broken; degrade to fallback list (will likely fail
            # but the router can still surface a structured error to the client).
            usable = fallback

        weights = self._weights.get(intent, INTENT_WEIGHTS[Intent.INTERACTIVE])

        # Compute worst values for min-max normalisation.
        worst_latency = max((p.typical_latency_ms for p in usable), default=1.0)
        worst_cost = max((p.cost_per_1k_tokens for p in usable), default=1.0)

        scored: list[ScoredProvider] = []
        for p in usable:
            h = health.get(p.name)

            # Determine latency estimate: prefer feedback EMA, then health, then static
            latency_est = p.typical_latency_ms
            if self._feedback:
                metrics = self._feedback.get_metrics(p.name)
                if metrics and metrics.latency_ema > 0:
                    latency_est = metrics.latency_ema
                elif h and h.avg_latency_ms > 0:
                    latency_est = h.avg_latency_ms
            elif h and h.avg_latency_ms > 0:
                latency_est = h.avg_latency_ms

            # Determine success score: prefer feedback EMA, then health
            success_score = 1.0
            if self._feedback:
                metrics = self._feedback.get_metrics(p.name)
                if metrics and metrics.success_rate_ema < 1.0:
                    success_score = metrics.success_rate_ema
                elif h:
                    success_score = 1.0 - h.error_rate
            elif h:
                success_score = 1.0 - h.error_rate

            # Apply anomaly penalty if feedback available
            anomaly_penalty = 0.0
            if self._feedback:
                metrics = self._feedback.get_metrics(p.name)
                if metrics:
                    anomaly_penalty = 0.1 * metrics.anomaly_score

            capability_score = p.capability.get(intent.value, 0.5)

<<<<<<< HEAD
=======
            # Confidence-gated premium demotion: when the caller signalled
            # low confidence that this request actually needs premium
            # quality, zero out the capability sub-score for tier-3
            # providers so cheaper siblings outrank them.
            if (
                confidence_hint is not None
                and confidence_hint < premium_threshold
                and p.capability_tier >= 3
            ):
                capability_score = 0.0

>>>>>>> 2b788c2948bcc409fd824497816e061092d81ec0
            latency_score = _normalize_latency(latency_est, worst_latency)
            cost_score = _normalize_cost(p.cost_per_1k_tokens, worst_cost)

            # Hard latency budget: if the provider's estimated latency already
            # exceeds the caller's budget, zero-out its latency sub-score so
            # it is only picked if nothing else is available.
            if latency_budget_ms is not None and latency_est > latency_budget_ms:
                latency_score = 0.0

<<<<<<< HEAD
=======
            # SLA breach demotion: if the provider declares an SLA for this
            # intent and the observed EMA latency already exceeds it, treat
            # the provider as out-of-spec and zero its latency sub-score so
            # it is deprioritised behind any in-spec sibling.
            sla_ms = p.sla_p95_latency_ms.get(intent.value)
            if sla_ms is not None and sla_ms > 0 and latency_est > sla_ms:
                latency_score = 0.0

>>>>>>> 2b788c2948bcc409fd824497816e061092d81ec0
            score = (
                weights.latency * latency_score
                + weights.cost * cost_score
                + weights.capability * capability_score
                + weights.success * success_score
                - anomaly_penalty
            )
            scored.append(
                ScoredProvider(
                    provider=p,
                    score=score,
                    sub_scores={
                        "latency": round(latency_score, 3),
                        "cost": round(cost_score, 3),
                        "capability": round(capability_score, 3),
                        "success": round(success_score, 3),
                    },
                )
            )

        scored.sort(key=lambda s: s.score, reverse=True)
        return scored
<<<<<<< HEAD
=======

    @staticmethod
    def reorder_after_failure(
        remaining: list[ScoredProvider], failed_tier: int
    ) -> list[ScoredProvider]:
        """Bias the next-attempt order toward graceful capability degradation.

        After a primary fails for a non-capability reason (overload, timeout,
        rate limit), prefer same-or-lower-tier siblings before reaching for
        another premium model. Stable within each band so the underlying
        multi-objective ranking is preserved as the tiebreaker.
        """
        if not remaining:
            return remaining
        same_or_lower = [s for s in remaining if s.provider.capability_tier <= failed_tier]
        higher = [s for s in remaining if s.provider.capability_tier > failed_tier]
        return same_or_lower + higher
>>>>>>> 2b788c2948bcc409fd824497816e061092d81ec0
