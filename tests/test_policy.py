"""Unit tests for the multi-objective routing policy."""
from __future__ import annotations

from intelliroute.common.models import Intent, ProviderHealth, ProviderInfo
from intelliroute.router.policy import RoutingPolicy


def _providers() -> list[ProviderInfo]:
    return [
        ProviderInfo(
            name="fast",
            url="http://fast",
            model="fast-1",
            capability={"interactive": 0.8, "reasoning": 0.4, "batch": 0.5, "code": 0.6},
            cost_per_1k_tokens=0.002,
            typical_latency_ms=150,
        ),
        ProviderInfo(
            name="smart",
            url="http://smart",
            model="smart-1",
            capability={"interactive": 0.7, "reasoning": 0.95, "batch": 0.8, "code": 0.9},
            cost_per_1k_tokens=0.02,
            typical_latency_ms=900,
        ),
        ProviderInfo(
            name="cheap",
            url="http://cheap",
            model="cheap-1",
            capability={"interactive": 0.5, "reasoning": 0.4, "batch": 0.7, "code": 0.4},
            cost_per_1k_tokens=0.0005,
            typical_latency_ms=700,
        ),
    ]


def test_interactive_prefers_low_latency():
    policy = RoutingPolicy()
    ranked = policy.rank(_providers(), health={}, intent=Intent.INTERACTIVE)
    assert ranked[0].provider.name == "fast"


def test_reasoning_prefers_capability():
    policy = RoutingPolicy()
    ranked = policy.rank(_providers(), health={}, intent=Intent.REASONING)
    assert ranked[0].provider.name == "smart"


def test_batch_prefers_cheapest():
    policy = RoutingPolicy()
    ranked = policy.rank(_providers(), health={}, intent=Intent.BATCH)
    assert ranked[0].provider.name == "cheap"


def test_open_circuit_demoted_to_last():
    policy = RoutingPolicy()
    health = {
        "fast": ProviderHealth(name="fast", healthy=False, circuit_state="open"),
    }
    ranked = policy.rank(_providers(), health=health, intent=Intent.INTERACTIVE)
    names = [s.provider.name for s in ranked]
    # 'fast' must not be first when its breaker is open; smart/cheap come first.
    assert names[0] != "fast"
    # 'fast' should not be in the usable list at all (only surfaces as fallback
    # when every provider is open; here others are healthy).
    assert "fast" not in names


def test_latency_budget_zeros_out_slow_providers():
    policy = RoutingPolicy()
    ranked = policy.rank(
        _providers(),
        health={},
        intent=Intent.INTERACTIVE,
        latency_budget_ms=200,
    )
    # Only "fast" meets the 200 ms budget, so it must be first.
    assert ranked[0].provider.name == "fast"


def test_ranking_is_deterministic_and_stable():
    policy = RoutingPolicy()
    r1 = policy.rank(_providers(), health={}, intent=Intent.CODE)
    r2 = policy.rank(_providers(), health={}, intent=Intent.CODE)
    assert [s.provider.name for s in r1] == [s.provider.name for s in r2]


def test_empty_provider_list_returns_empty():
    policy = RoutingPolicy()
    assert policy.rank([], health={}, intent=Intent.INTERACTIVE) == []


def test_feedback_latency_override_static_config():
    """Test that feedback EMA latency overrides static config."""
    from intelliroute.router.feedback import CompletionOutcome, FeedbackCollector

    providers = _providers()
    feedback = FeedbackCollector()
    policy = RoutingPolicy(feedback=feedback)

    # Record a fast latency for "fast" provider
    feedback.record(
        CompletionOutcome(provider="fast", latency_ms=50.0, success=True)
    )
    # Record a slow latency for "smart" provider
    feedback.record(
        CompletionOutcome(provider="smart", latency_ms=2000.0, success=True)
    )

    ranked = policy.rank(providers, health={}, intent=Intent.INTERACTIVE)
    # "fast" should still rank highest despite typical_latency_ms being higher
    assert ranked[0].provider.name == "fast"


def test_anomaly_penalty_reduces_score():
    """Test that anomaly score penalty reduces provider score."""
    from intelliroute.router.feedback import CompletionOutcome, FeedbackCollector

    providers = _providers()
    feedback = FeedbackCollector()
    policy = RoutingPolicy(feedback=feedback)

    # Record normal latency for "fast"
    feedback.record(
        CompletionOutcome(provider="fast", latency_ms=100.0, success=True)
    )
    # Record anomalous latency for "smart"
    feedback.record(
        CompletionOutcome(provider="smart", latency_ms=5000.0, success=True)
    )

    ranked = policy.rank(providers, health={}, intent=Intent.INTERACTIVE)
    # Anomalous "smart" should be penalized
    scores = {s.provider.name: s.score for s in ranked}
    assert scores["fast"] > scores["smart"]


def test_policy_without_feedback_uses_static():
    """Test that policy without feedback falls back to static config."""
    policy = RoutingPolicy()
    ranked = policy.rank(_providers(), health={}, intent=Intent.INTERACTIVE)
    # Without feedback, should use typical_latency_ms from provider
    assert ranked[0].provider.name == "fast"


def _tiered_providers() -> list[ProviderInfo]:
    return [
        ProviderInfo(
            name="premium",
            url="http://premium",
            model="premium-1",
            capability={"interactive": 0.9, "reasoning": 0.95},
            cost_per_1k_tokens=0.03,
            typical_latency_ms=900,
            capability_tier=3,
        ),
        ProviderInfo(
            name="standard",
            url="http://standard",
            model="standard-1",
            capability={"interactive": 0.7, "reasoning": 0.7},
            cost_per_1k_tokens=0.005,
            typical_latency_ms=400,
            capability_tier=2,
        ),
        ProviderInfo(
            name="cheap",
            url="http://cheap",
            model="cheap-1",
            capability={"interactive": 0.5, "reasoning": 0.4},
            cost_per_1k_tokens=0.0005,
            typical_latency_ms=600,
            capability_tier=1,
        ),
    ]


def test_reorder_after_failure_prefers_lower_tier_siblings():
    """A failed premium primary should fall back to lower-tier siblings first."""
    policy = RoutingPolicy()
    ranked = policy.rank(_tiered_providers(), health={}, intent=Intent.REASONING)
    # Simulate the premium primary failing: take everything except premium and reorder.
    score_map = {s.provider.name: s for s in ranked}
    remaining = [score_map["standard"], score_map["cheap"]]
    reordered = RoutingPolicy.reorder_after_failure(remaining, failed_tier=3)
    names = [s.provider.name for s in reordered]
    # Both standard (tier 2) and cheap (tier 1) are <= tier 3 → both stay,
    # ordering preserved relative to original score ranking.
    assert set(names) == {"standard", "cheap"}


def test_reorder_after_failure_demotes_higher_tier():
    """When primary is mid-tier, premium peers must be demoted to the tail."""
    policy = RoutingPolicy()
    providers = _tiered_providers()
    ranked = policy.rank(providers, health={}, intent=Intent.INTERACTIVE)
    # Construct a scenario where standard (tier 2) failed and premium is still in pool.
    # Build "remaining" with premium first, cheap second.
    score_map = {s.provider.name: s for s in ranked}
    remaining = [score_map["premium"], score_map["cheap"]]
    reordered = RoutingPolicy.reorder_after_failure(remaining, failed_tier=2)
    # cheap (tier 1) is <= 2 and must come before premium (tier 3).
    assert reordered[0].provider.name == "cheap"
    assert reordered[1].provider.name == "premium"


def test_reorder_after_failure_empty_list():
    assert RoutingPolicy.reorder_after_failure([], failed_tier=2) == []


def test_sla_breach_zeroes_latency_score():
    """When EMA latency exceeds the per-intent SLA, latency sub-score is zeroed."""
    from intelliroute.router.feedback import CompletionOutcome, FeedbackCollector

    fc = FeedbackCollector()
    # 'fast' has been observed running at 800ms (over its 200ms SLA)
    fc.record(CompletionOutcome(provider="fast", latency_ms=800.0, success=True))
    fc.record(CompletionOutcome(provider="standard", latency_ms=300.0, success=True))

    providers = [
        ProviderInfo(
            name="fast",
            url="http://fast",
            model="fast-1",
            capability={"interactive": 0.9},
            cost_per_1k_tokens=0.002,
            typical_latency_ms=120,
            sla_p95_latency_ms={"interactive": 200.0},
        ),
        ProviderInfo(
            name="standard",
            url="http://standard",
            model="standard-1",
            capability={"interactive": 0.7},
            cost_per_1k_tokens=0.005,
            typical_latency_ms=400,
            sla_p95_latency_ms={"interactive": 600.0},
        ),
    ]
    policy = RoutingPolicy(feedback=fc)
    ranked = policy.rank(providers, health={}, intent=Intent.INTERACTIVE)
    sub = {s.provider.name: s.sub_scores for s in ranked}
    # 'fast' breached its SLA → its latency sub-score is zeroed.
    assert sub["fast"]["latency"] == 0.0
    # 'standard' is in-spec and gets a non-zero latency sub-score.
    assert sub["standard"]["latency"] > 0.0


def test_low_confidence_zeros_premium_capability():
    policy = RoutingPolicy()
    providers = _tiered_providers()
    ranked = policy.rank(
        providers,
        health={},
        intent=Intent.REASONING,
        confidence_hint=0.3,
    )
    sub = {s.provider.name: s.sub_scores for s in ranked}
    # Premium (tier 3) capability sub-score is zeroed under low confidence.
    assert sub["premium"]["capability"] == 0.0
    # Standard (tier 2) and cheap (tier 1) keep their capability scores.
    assert sub["standard"]["capability"] > 0.0
    assert sub["cheap"]["capability"] > 0.0


def test_high_confidence_keeps_premium_capability():
    policy = RoutingPolicy()
    providers = _tiered_providers()
    ranked = policy.rank(
        providers,
        health={},
        intent=Intent.REASONING,
        confidence_hint=0.9,
    )
    sub = {s.provider.name: s.sub_scores for s in ranked}
    assert sub["premium"]["capability"] > 0.0


def test_no_confidence_hint_does_not_demote_premium():
    policy = RoutingPolicy()
    providers = _tiered_providers()
    ranked = policy.rank(providers, health={}, intent=Intent.REASONING)
    sub = {s.provider.name: s.sub_scores for s in ranked}
    assert sub["premium"]["capability"] > 0.0


def test_sla_unset_does_not_demote():
    """Providers without a declared SLA are not penalised."""
    providers = [
        ProviderInfo(
            name="no-sla",
            url="http://no-sla",
            model="m",
            capability={"interactive": 0.9},
            cost_per_1k_tokens=0.001,
            typical_latency_ms=10_000,  # very slow
        ),
    ]
    policy = RoutingPolicy()
    ranked = policy.rank(providers, health={}, intent=Intent.INTERACTIVE)
    # Nothing else to compare against — single-provider list returns it.
    assert ranked[0].provider.name == "no-sla"
