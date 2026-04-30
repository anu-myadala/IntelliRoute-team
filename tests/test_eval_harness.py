from __future__ import annotations

from intelliroute.eval_harness.metrics import aggregate_summary
from intelliroute.eval_harness.types import ReplayResult
from intelliroute.eval_harness.workload import generate_workload


def test_workload_generation_is_deterministic() -> None:
    a = generate_workload("normal_mixed", size=8, seed=11)
    b = generate_workload("normal_mixed", size=8, seed=11)
    assert [x.to_dict() for x in a] == [x.to_dict() for x in b]
    assert all(r.request_id for r in a)
    assert all(r.prompt for r in a)


def test_budget_pressure_workload_contains_scope_fields() -> None:
    rows = generate_workload("budget_pressure", size=6, seed=1)
    assert any(r.team_id for r in rows)
    assert any(r.workflow_id for r in rows)
    assert all(r.intent_hint in {"reasoning", "batch"} for r in rows)


def test_overload_brownout_workload_has_heavy_mix() -> None:
    rows = generate_workload("overload_brownout", size=9, seed=3)
    assert len(rows) == 9
    assert any(r.intent_hint == "batch" and r.max_tokens >= 512 for r in rows)
    assert any(r.intent_hint == "reasoning" and r.priority == "medium" for r in rows)
    assert any(r.intent_hint == "interactive" and r.priority == "high" for r in rows)


def test_aggregate_summary_counts_and_rates() -> None:
    rows = [
        ReplayResult(
            request_id="1",
            scenario_name="normal_mixed",
            policy_name="intelliroute",
            success=True,
            status_code=200,
            provider="mock-fast",
            latency_ms=100.0,
            estimated_cost_usd=0.001,
            fallback_used=False,
            premium_used=False,
            reject=False,
            reroute_or_downgrade=False,
            brownout_degraded=False,
            budget_actions=[],
        ),
        ReplayResult(
            request_id="2",
            scenario_name="normal_mixed",
            policy_name="intelliroute",
            success=False,
            status_code=503,
            provider=None,
            latency_ms=250.0,
            estimated_cost_usd=0.0,
            fallback_used=True,
            premium_used=False,
            reject=True,
            reroute_or_downgrade=True,
            brownout_degraded=False,
            budget_actions=[],
        ),
    ]
    summary = aggregate_summary(rows)
    assert len(summary) == 1
    s = summary[0]
    assert s["total_requests"] == 2
    assert s["success_rate"] == 0.5
    assert s["fallback_count"] == 1
    assert s["reject_count"] == 1
