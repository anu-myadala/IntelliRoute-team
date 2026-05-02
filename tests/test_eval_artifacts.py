from __future__ import annotations

import json
from pathlib import Path

from intelliroute.eval_harness.artifacts import build_run_id, write_json, write_metrics_csv, write_timeline_csv
from intelliroute.eval_harness.metrics import aggregate_summary
from intelliroute.eval_harness.runner import ReplayRunOutput, aggregate_matrix_runs
from intelliroute.eval_harness.types import ReplayResult
from intelliroute.eval_harness.workload import generate_workload


def _row(req_id: str, scenario: str, policy: str, success: bool, provider: str | None, seed: int) -> ReplayResult:
    return ReplayResult(
        request_id=req_id,
        scenario_name=scenario,
        policy_name=policy,
        success=success,
        status_code=200 if success else 503,
        provider=provider,
        latency_ms=50.0 if success else 100.0,
        estimated_cost_usd=0.001 if success else 0.0,
        fallback_used=not success,
        premium_used=provider == "mock-smart",
        reject=not success,
        reroute_or_downgrade=not success,
        brownout_degraded=False,
        budget_actions=[],
        seed=seed,
    )


def test_seed_drives_deterministic_workload() -> None:
    a = generate_workload("normal_mixed", 12, seed=123)
    b = generate_workload("normal_mixed", 12, seed=123)
    c = generate_workload("normal_mixed", 12, seed=124)
    assert [x.to_dict() for x in a] == [x.to_dict() for x in b]
    assert [x.to_dict() for x in a] != [x.to_dict() for x in c]


def test_metrics_and_timeline_and_json_written(tmp_path: Path) -> None:
    rows = [_row("1", "normal_mixed", "intelliroute", True, "mock-fast", 7)]
    metrics_path = tmp_path / "metrics.csv"
    timeline_path = tmp_path / "timeline.csv"
    summary_path = tmp_path / "summary.json"
    write_metrics_csv(metrics_path, rows)
    write_timeline_csv(
        timeline_path,
        [
            {
                "timestamp_sec": 1.0,
                "requests_completed": 1,
                "avg_latency_ms": 50.0,
                "p95_latency_ms": 50.0,
                "error_rate": 0.0,
                "queue_depth": 0,
                "brownout_active": 0,
                "active_breakers": 0,
                "total_cost": 0.001,
                "requests_shed": 0,
            }
        ],
    )
    write_json(summary_path, {"ok": True})
    assert "request_id" in metrics_path.read_text(encoding="utf-8")
    assert "timestamp_sec" in timeline_path.read_text(encoding="utf-8")
    assert json.loads(summary_path.read_text(encoding="utf-8"))["ok"] is True


def test_aggregate_matrix_summary_contains_expected_keys() -> None:
    run_a = ReplayRunOutput(
        rows=[_row("1", "normal_mixed", "intelliroute", True, "mock-fast", 11)],
        reset_report={"ok": True},
        setup_report={},
        run_log=[],
        duration_seconds=1.2,
        scenario_name="normal_mixed",
        policy_name="intelliroute",
        seed=11,
    )
    run_b = ReplayRunOutput(
        rows=[_row("2", "normal_mixed", "round_robin", False, None, 12)],
        reset_report={"ok": True},
        setup_report={},
        run_log=[],
        duration_seconds=1.1,
        scenario_name="normal_mixed",
        policy_name="round_robin",
        seed=12,
    )
    agg = aggregate_matrix_runs([run_a, run_b])
    assert "scenario_policy_summary" in agg
    assert "policy_averages" in agg
    assert "best_run" in agg
    assert "worst_run" in agg
    assert agg["run_counts"] == 2
    assert agg["seed_counts"] == 2


def test_build_run_id_contains_scenario_policy_and_seed() -> None:
    rid = build_run_id(scenario="budget_pressure", policy="intelliroute", seed=42)
    assert "budget_pressure" in rid
    assert "intelliroute" in rid
    assert "seed42" in rid


def test_aggregate_summary_has_p99_and_error_rate() -> None:
    rows = [
        _row("1", "normal_mixed", "intelliroute", True, "mock-fast", 1),
        _row("2", "normal_mixed", "intelliroute", False, None, 1),
    ]
    summary = aggregate_summary(rows)
    assert "p99_latency_ms" in summary[0]
    assert "error_rate" in summary[0]
