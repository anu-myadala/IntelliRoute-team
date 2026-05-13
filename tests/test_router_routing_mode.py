from __future__ import annotations

import pytest

from intelliroute.common.models import CompletionRequest, Intent, ProviderInfo
from intelliroute.router.main import _rank_candidates, _set_routing_mode


def _providers() -> list[ProviderInfo]:
    return [
        ProviderInfo(
            name="fast",
            url="http://fast",
            model="fast-1",
            capability={"interactive": 0.6},
            cost_per_1k_tokens=0.003,
            typical_latency_ms=100,
            capability_tier=2,
        ),
        ProviderInfo(
            name="cheap",
            url="http://cheap",
            model="cheap-1",
            capability={"interactive": 0.5},
            cost_per_1k_tokens=0.001,
            typical_latency_ms=300,
            capability_tier=1,
        ),
        ProviderInfo(
            name="premium",
            url="http://premium",
            model="premium-1",
            capability={"interactive": 0.95},
            cost_per_1k_tokens=0.02,
            typical_latency_ms=200,
            capability_tier=3,
        ),
    ]


def _req() -> CompletionRequest:
    return CompletionRequest(tenant_id="t1", messages=[{"role": "user", "content": "hello"}], max_tokens=64)


def test_set_routing_mode_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        _set_routing_mode("unknown")


def test_cheapest_first_orders_by_cost() -> None:
    ranked = _rank_candidates("cheapest_first", _providers(), {}, Intent.INTERACTIVE, _req())
    assert [s.provider.name for s in ranked][:2] == ["cheap", "fast"]


def test_latency_first_orders_by_static_latency() -> None:
    ranked = _rank_candidates("latency_first", _providers(), {}, Intent.INTERACTIVE, _req())
    assert [s.provider.name for s in ranked][0] == "fast"


def test_default_intelliroute_still_works() -> None:
    ranked = _rank_candidates("intelliroute", _providers(), {}, Intent.INTERACTIVE, _req())
    assert ranked
<<<<<<< HEAD
=======


def test_reasoning_code_ladder_prefers_gemini_then_groq_then_mocks() -> None:
    providers = [
        ProviderInfo(
            name="mock-smart",
            url="http://mock-smart",
            model="smart-1",
            provider_type="mock",
            capability={"reasoning": 0.99, "code": 0.99},
            cost_per_1k_tokens=0.02,
            typical_latency_ms=100,
            capability_tier=3,
        ),
        ProviderInfo(
            name="groq",
            url="https://api.groq.com/openai/v1",
            model="llama-3.3-70b-versatile",
            provider_type="groq",
            capability={"reasoning": 0.75, "code": 0.75},
            cost_per_1k_tokens=0.0007,
            typical_latency_ms=500,
            capability_tier=2,
        ),
        ProviderInfo(
            name="mock-fast",
            url="http://mock-fast",
            model="fast-1",
            provider_type="mock",
            capability={"reasoning": 0.45, "code": 0.6},
            cost_per_1k_tokens=0.002,
            typical_latency_ms=30,
            capability_tier=2,
        ),
        ProviderInfo(
            name="gemini",
            url="https://generativelanguage.googleapis.com/v1beta",
            model="gemini-2.5-flash",
            provider_type="gemini",
            capability={"reasoning": 0.97, "code": 0.91},
            cost_per_1k_tokens=0.0035,
            typical_latency_ms=1200,
            capability_tier=3,
        ),
    ]

    reasoning = _rank_candidates("intelliroute", providers, {}, Intent.REASONING, _req())
    code = _rank_candidates("intelliroute", providers, {}, Intent.CODE, _req())

    assert [s.provider.name for s in reasoning[:3]] == ["gemini", "groq", "mock-smart"]
    assert [s.provider.name for s in code[:3]] == ["gemini", "groq", "mock-smart"]
>>>>>>> gemini-groq-failover-demo
