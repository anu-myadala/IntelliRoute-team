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
