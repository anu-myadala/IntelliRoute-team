"""Unit tests for control-plane policy evaluation."""
from __future__ import annotations

from intelliroute.common.models import CompletionRequest, Intent, ProviderInfo
from intelliroute.router.policy_engine import PolicyEngineConfig, PolicyEvaluator


def _providers() -> list[ProviderInfo]:
    return [
        ProviderInfo(
            name="mock-fast",
            url="http://127.0.0.1:9001",
            model="fast-1",
            provider_type="mock",
            capability={"interactive": 0.85, "reasoning": 0.45, "batch": 0.5, "code": 0.6},
            cost_per_1k_tokens=0.002,
            typical_latency_ms=120,
        ),
        ProviderInfo(
            name="mock-smart",
            url="http://127.0.0.1:9002",
            model="smart-1",
            provider_type="mock",
            capability={"interactive": 0.7, "reasoning": 0.95, "batch": 0.8, "code": 0.9},
            cost_per_1k_tokens=0.02,
            typical_latency_ms=900,
        ),
        ProviderInfo(
            name="mock-cheap",
            url="http://127.0.0.1:9003",
            model="cheap-1",
            provider_type="mock",
            capability={"interactive": 0.55, "reasoning": 0.4, "batch": 0.75, "code": 0.45},
            cost_per_1k_tokens=0.0003,
            typical_latency_ms=600,
        ),
    ]


def _req(content: str, intent_hint: Intent | None = None) -> CompletionRequest:
    return CompletionRequest(
        tenant_id="t1",
        messages=[{"role": "user", "content": content}],
        intent_hint=intent_hint,
    )


def test_batch_blocks_premium():
    cfg = PolicyEngineConfig(
        enabled=True,
        premium_provider_names=frozenset({"mock-smart"}),
        complexity_threshold_premium=0.5,
        budget_utilization_downgrade=0.85,
        interactive_max_latency_ms=350,
        apply_interactive_latency_gate=True,
    )
    ev = PolicyEvaluator(cfg)
    req = _req("Summarize the following long document into bullets")
    out, pe = ev.evaluate(_providers(), Intent.BATCH, req, tenant_budget_usd=None, tenant_spent_usd=0.0)
    assert "mock-smart" not in {p.name for p in out}
    assert "batch_avoids_premium" in pe.matched_rules


def test_interactive_simple_blocks_premium_and_slow():
    cfg = PolicyEngineConfig(
        enabled=True,
        premium_provider_names=frozenset({"mock-smart"}),
        complexity_threshold_premium=0.5,
        budget_utilization_downgrade=0.85,
        interactive_max_latency_ms=650,
        apply_interactive_latency_gate=True,
    )
    ev = PolicyEvaluator(cfg)
    req = _req("Hi")
    out, pe = ev.evaluate(_providers(), Intent.INTERACTIVE, req, tenant_budget_usd=None, tenant_spent_usd=0.0)
    names = {p.name for p in out}
    assert names == {"mock-fast", "mock-cheap"}
    assert "premium_requires_reasoning_or_high_complexity" in pe.matched_rules


def test_budget_pressure_blocks_premium():
    cfg = PolicyEngineConfig(
        enabled=True,
        premium_provider_names=frozenset({"mock-smart"}),
        complexity_threshold_premium=0.1,
        budget_utilization_downgrade=0.5,
        interactive_max_latency_ms=350,
        apply_interactive_latency_gate=False,
    )
    ev = PolicyEvaluator(cfg)
    req = _req("x" * 3000 + " explain step by step the CAP theorem " * 5)
    out, pe = ev.evaluate(
        _providers(),
        Intent.REASONING,
        req,
        tenant_budget_usd=1.0,
        tenant_spent_usd=0.9,
    )
    assert "mock-smart" not in {p.name for p in out}
    assert "tenant_budget_pressure_blocks_premium" in pe.matched_rules
    assert pe.downgrade_reason is not None


def test_disabled_passes_through_all_providers():
    cfg = PolicyEngineConfig(
        enabled=False,
        premium_provider_names=frozenset({"mock-smart"}),
        complexity_threshold_premium=0.99,
        budget_utilization_downgrade=0.1,
        interactive_max_latency_ms=1,
        apply_interactive_latency_gate=True,
    )
    ev = PolicyEvaluator(cfg)
    req = _req("Hi")
    out, pe = ev.evaluate(_providers(), Intent.INTERACTIVE, req, tenant_budget_usd=None, tenant_spent_usd=0.0)
    assert len(out) == 3
    assert pe.matched_rules == []


def test_fail_open_when_all_blocked(monkeypatch):
    cfg = PolicyEngineConfig(
        enabled=True,
        premium_provider_names=frozenset({"mock-fast", "mock-smart", "mock-cheap"}),
        complexity_threshold_premium=0.99,
        budget_utilization_downgrade=0.85,
        interactive_max_latency_ms=350,
        apply_interactive_latency_gate=True,
    )
    ev = PolicyEvaluator(cfg)
    req = _req("Hi")
    out, pe = ev.evaluate(_providers(), Intent.INTERACTIVE, req, tenant_budget_usd=None, tenant_spent_usd=0.0)
    assert len(out) == 3
    assert pe.fail_open is True
    assert "fail_open_restore_full_provider_set" in pe.matched_rules
