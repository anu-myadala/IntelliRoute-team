"""Unit tests for the cost accountant."""
from __future__ import annotations

from intelliroute.common.models import CostEvent
from intelliroute.cost_tracker.accounting import CostAccountant


def _event(tenant: str, provider: str, cost: float, tokens: int = 100, ts: float = 0.0) -> CostEvent:
    return CostEvent(
        request_id=f"req-{ts}-{tenant}",
        tenant_id=tenant,
        provider=provider,
        model="m1",
        prompt_tokens=tokens // 2,
        completion_tokens=tokens // 2,
        estimated_cost_usd=cost,
        unix_ts=ts,
    )


def test_records_and_rolls_up_by_provider():
    acc = CostAccountant()
    acc.record(_event("t1", "fast", cost=0.01, tokens=100))
    acc.record(_event("t1", "smart", cost=0.05, tokens=200))
    acc.record(_event("t1", "fast", cost=0.02, tokens=100))

    s = acc.summary("t1")
    assert s.total_requests == 3
    assert s.total_tokens == 400
    assert round(s.total_cost_usd, 4) == 0.08
    assert round(s.by_provider["fast"], 4) == 0.03
    assert round(s.by_provider["smart"], 4) == 0.05


def test_unknown_tenant_returns_zero_summary():
    acc = CostAccountant()
    s = acc.summary("ghost")
    assert s.total_requests == 0
    assert s.total_cost_usd == 0.0
    assert s.by_provider == {}


def test_budget_alert_fires_once_on_crossing():
    acc = CostAccountant(budgets={"t1": 0.05})
    acc.record(_event("t1", "fast", cost=0.03))
    assert acc.alerts() == []
    acc.record(_event("t1", "smart", cost=0.03))  # total 0.06 > 0.05
    assert len(acc.alerts()) == 1
    acc.record(_event("t1", "smart", cost=0.01))
    # Alert string is deduplicated so we still only have one unique alert.
    assert len(acc.alerts()) == 1


def test_tenants_are_isolated():
    acc = CostAccountant()
    acc.record(_event("t1", "fast", cost=0.01))
    acc.record(_event("t2", "fast", cost=0.05))
    assert acc.summary("t1").total_cost_usd == 0.01
    assert acc.summary("t2").total_cost_usd == 0.05


def test_headroom_returns_none_when_no_budget():
    acc = CostAccountant()
    assert acc.headroom("t1") is None


def test_headroom_decreases_with_spend():
    acc = CostAccountant(budgets={"t1": 1.0})
    assert acc.headroom("t1") == 1.0
    acc.record(_event("t1", "fast", cost=0.3))
    assert round(acc.headroom("t1"), 4) == 0.7


def test_would_exceed_false_when_no_budget():
    acc = CostAccountant()
    assert acc.would_exceed("t1", projected_cost_usd=999.0) is False


def test_would_exceed_true_when_projected_overshoots():
    acc = CostAccountant(budgets={"t1": 1.0})
    acc.record(_event("t1", "fast", cost=0.8))
    # Headroom is now 0.2; a 0.5 projected cost would overshoot.
    assert acc.would_exceed("t1", projected_cost_usd=0.5) is True
    assert acc.would_exceed("t1", projected_cost_usd=0.1) is False


def test_would_exceed_after_budget_already_breached():
    acc = CostAccountant(budgets={"t1": 1.0})
    acc.record(_event("t1", "fast", cost=1.5))
    # Headroom is negative; any positive projected cost would still exceed.
    assert acc.would_exceed("t1", projected_cost_usd=0.001) is True


def test_team_and_workflow_rollups_are_recorded():
    acc = CostAccountant()
    e = _event("t1", "mock-fast", cost=0.02, tokens=120)
    e.team_id = "team-a"
    e.workflow_id = "wf-nightly"
    acc.record(e)
    team = acc.team_summary("team-a")
    workflow = acc.workflow_summary("wf-nightly")
    assert team["total_requests"] == 1
    assert workflow["total_requests"] == 1
    assert team["total_tokens"] == 120
    assert workflow["total_cost_usd"] == 0.02


def test_team_budget_and_premium_cap_status():
    acc = CostAccountant()
    acc.set_team_budget("team-a", 1.0)
    acc.set_team_premium_cap("team-a", 0.05)
    e = _event("t1", "mock-smart", cost=0.06, tokens=100)
    e.team_id = "team-a"
    acc.record(e)
    status = acc.team_budget_status("team-a")
    assert status["team_id"] == "team-a"
    assert status["budget_usd"] == 1.0
    assert status["premium_cap_hit"] is True


def test_workflow_budget_status_reports_pressure():
    acc = CostAccountant()
    acc.set_workflow_budget("wf-1", 0.1)
    e = _event("t1", "mock-cheap", cost=0.09, tokens=100)
    e.workflow_id = "wf-1"
    acc.record(e)
    status = acc.workflow_budget_status("wf-1")
    assert status["workflow_id"] == "wf-1"
    assert status["budget_pressure"] is True
