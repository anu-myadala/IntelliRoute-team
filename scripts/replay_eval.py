from __future__ import annotations

import argparse
import os
from pathlib import Path
import random
import subprocess
import time

from intelliroute.eval_harness import (
    POLICIES,
    SCENARIOS,
    aggregate_matrix_runs,
    aggregate_summary,
    build_matrix_id,
    build_run_id,
    generate_workload,
    run_matrix,
    run_replay,
    write_json,
    write_log,
    write_metrics_csv,
    write_results_jsonl,
    write_summary_csv,
    write_timeline_csv,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay workloads against IntelliRoute and baseline routing modes.")
    p.add_argument("--gateway-url", default="http://127.0.0.1:8000")
    p.add_argument("--router-url", default="http://127.0.0.1:8001")
    p.add_argument("--cost-tracker-url", default="http://127.0.0.1:8003")
    p.add_argument("--health-monitor-url", default="http://127.0.0.1:8004")
    p.add_argument(
        "--rate-limiter-urls",
        default="http://127.0.0.1:8002,http://127.0.0.1:8012,http://127.0.0.1:8022",
        help="Comma-separated limiter replica URLs to reset before each run.",
    )
    p.add_argument("--api-key", default="demo-key-123")
    p.add_argument("--scenario", choices=["all", *SCENARIOS], default="all")
    p.add_argument("--policy", choices=["all", *POLICIES], default="all")
    p.add_argument("--size", type=int, default=60)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--seeds", default="", help="Comma-separated seeds for matrix runs.")
    p.add_argument("--seed-count", type=int, default=1, help="When --seeds is unset, generate this many seeds.")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--timeline-interval-s", type=float, default=1.0)
    p.add_argument("--no-reset", action="store_true", help="Disable per-run reset/isolation.")
    p.add_argument("--out-dir", type=Path, default=Path("eval_results"))
    p.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    scenarios = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    policies = list(POLICIES) if args.policy == "all" else [args.policy]
    rate_limiter_urls = [u.strip() for u in args.rate_limiter_urls.split(",") if u.strip()]
    seeds = _resolve_seeds(args.seed, args.seeds, args.seed_count)
    workload_by_scenario = {
        scenario: generate_workload(scenario, args.size, seed=seeds[0]) for scenario in scenarios
    }
    run_started = time.monotonic()
    git_sha = _git_sha()
    if len(scenarios) == 1 and len(policies) == 1 and len(seeds) == 1:
        run = run_replay(
            scenario_name=scenarios[0],
            policy_name=policies[0],
            gateway_url=args.gateway_url,
            router_url=args.router_url,
            cost_tracker_url=args.cost_tracker_url,
            health_monitor_url=args.health_monitor_url,
            rate_limiter_urls=rate_limiter_urls,
            api_key=args.api_key,
            requests=workload_by_scenario[scenarios[0]],
            seed=seeds[0],
            concurrency=max(1, int(args.concurrency)),
            timeline_interval_s=args.timeline_interval_s,
            reset_before_run=not args.no_reset,
        )
        rows = run.rows
        summary = aggregate_summary(rows)
        _write_single_run_artifacts(
            args=args,
            run=run,
            summary_row=summary[0] if summary else {},
            git_sha=git_sha,
        )
    else:
        runs = run_matrix(
            scenarios=scenarios,
            policies=policies,
            gateway_url=args.gateway_url,
            router_url=args.router_url,
            cost_tracker_url=args.cost_tracker_url,
            health_monitor_url=args.health_monitor_url,
            rate_limiter_urls=rate_limiter_urls,
            api_key=args.api_key,
            workload_by_scenario={
                scenario: generate_workload(scenario, args.size, seed=seeds[0]) for scenario in scenarios
            },
            seeds=seeds,
            concurrency=args.concurrency,
            timeline_interval_s=args.timeline_interval_s,
            reset_before_run=not args.no_reset,
        )
        rows = [row for run in runs for row in run.rows]
        summary = aggregate_summary(rows)
        _write_matrix_artifacts(
            args=args,
            runs=runs,
            aggregate=aggregate_matrix_runs(runs),
            git_sha=git_sha,
        )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    results_path = args.out_dir / "results.jsonl"
    summary_path = args.out_dir / "summary.csv"
    write_results_jsonl(results_path, rows)
    write_summary_csv(summary_path, summary)
    elapsed = time.monotonic() - run_started
    print(f"wrote {len(rows)} rows -> {results_path}")
    print(f"wrote summary ({len(summary)} groups) -> {summary_path}")
    print(f"elapsed {elapsed:.2f}s")
    return 0


def _resolve_seeds(seed: int | None, seeds_arg: str, seed_count: int) -> list[int]:
    if seeds_arg.strip():
        return [int(s.strip()) for s in seeds_arg.split(",") if s.strip()]
    if seed is not None:
        return [int(seed)]
    rng = random.Random()
    return [rng.randint(1, 10_000_000) for _ in range(max(1, seed_count))]


def _git_sha() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        return out
    except Exception:
        return ""


def _jsonable_args(args: argparse.Namespace) -> dict:
    out: dict = {}
    for key, value in vars(args).items():
        out[key] = str(value) if isinstance(value, Path) else value
    return out


def _write_single_run_artifacts(*, args: argparse.Namespace, run, summary_row: dict, git_sha: str) -> None:
    run_name = build_run_id(scenario=run.scenario_name, policy=run.policy_name, seed=run.seed)
    run_dir = args.artifacts_dir / "replay_runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "summary.json", {"run_id": run_name, **summary_row, "duration_seconds": run.duration_seconds})
    write_metrics_csv(run_dir / "metrics.csv", run.rows)
    write_timeline_csv(run_dir / "timeline.csv", run.setup_report.get("timeline", []))
    write_json(run_dir / "reset_report.json", run.reset_report)
    write_json(
        run_dir / "config.json",
        {
            "scenario": run.scenario_name,
            "policy": run.policy_name,
            "seed": run.seed,
            "cli_args": _jsonable_args(args),
            "endpoints": {
                "gateway_url": args.gateway_url,
                "router_url": args.router_url,
                "cost_tracker_url": args.cost_tracker_url,
                "health_monitor_url": args.health_monitor_url,
                "rate_limiter_urls": args.rate_limiter_urls,
            },
            "reset_before_run": not args.no_reset,
            "git_commit_hash": git_sha,
            "scenario_setup": run.setup_report,
        },
    )
    write_log(run_dir / "run.log", run.run_log)


def _write_matrix_artifacts(*, args: argparse.Namespace, runs: list, aggregate: dict, git_sha: str) -> None:
    matrix_name = build_matrix_id()
    matrix_dir = args.artifacts_dir / "matrix_runs" / matrix_name
    individual = matrix_dir / "individual_runs"
    individual.mkdir(parents=True, exist_ok=True)
    run_index: list[dict] = []
    for run in runs:
        run_name = build_run_id(scenario=run.scenario_name, policy=run.policy_name, seed=run.seed)
        run_dir = individual / run_name
        run_dir.mkdir(parents=True, exist_ok=True)
        summary = aggregate_summary(run.rows)
        write_json(run_dir / "summary.json", {"run_id": run_name, **(summary[0] if summary else {}), "duration_seconds": run.duration_seconds})
        write_metrics_csv(run_dir / "metrics.csv", run.rows)
        write_timeline_csv(run_dir / "timeline.csv", run.setup_report.get("timeline", []))
        write_json(run_dir / "reset_report.json", run.reset_report)
        write_json(
            run_dir / "config.json",
            {
                "scenario": run.scenario_name,
                "policy": run.policy_name,
                "seed": run.seed,
                "cli_args": _jsonable_args(args),
                "reset_before_run": not args.no_reset,
                "git_commit_hash": git_sha,
                "scenario_setup": run.setup_report,
            },
        )
        write_log(run_dir / "run.log", run.run_log)
        run_index.append(
            {
                "run_id": run_name,
                "scenario": run.scenario_name,
                "policy": run.policy_name,
                "seed": run.seed,
                "path": str(run_dir),
            }
        )
    write_json(matrix_dir / "aggregate_summary.json", aggregate)
    write_json(matrix_dir / "run_index.json", {"runs": run_index, "git_commit_hash": git_sha})
    write_log(
        matrix_dir / "report.md",
        [
            "# Replay Matrix Report",
            "",
            f"- runs: {len(run_index)}",
            f"- scenarios: {', '.join(sorted({r['scenario'] for r in run_index}))}",
            f"- policies: {', '.join(sorted({r['policy'] for r in run_index}))}",
            f"- seeds: {', '.join(str(s) for s in sorted({r['seed'] for r in run_index}))}",
            "",
            "See `aggregate_summary.json` and `individual_runs/`.",
        ],
    )


if __name__ == "__main__":
    raise SystemExit(main())
