from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .statistics import mean_value, median_value, p50, p95, p99, std_dev
from .types import ReplayResult


def aggregate_summary(rows: list[ReplayResult]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[ReplayResult]] = defaultdict(list)
    for row in rows:
        grouped[(row.scenario_name, row.policy_name)].append(row)
    out: list[dict[str, Any]] = []
    for (scenario, policy), bucket in sorted(grouped.items()):
        latencies = [r.latency_ms for r in bucket if r.latency_ms > 0]
        costs = [r.estimated_cost_usd for r in bucket]
        total = len(bucket)
        success = sum(1 for r in bucket if r.success)
        premium = sum(1 for r in bucket if r.premium_used)
        fallback = sum(1 for r in bucket if r.fallback_used)
        reroute = sum(1 for r in bucket if r.reroute_or_downgrade)
        reject = sum(1 for r in bucket if r.reject)
        provider_count: dict[str, int] = {}
        for r in bucket:
            if r.provider:
                provider_count[r.provider] = provider_count.get(r.provider, 0) + 1
        out.append(
            {
                "scenario_name": scenario,
                "policy_name": policy,
                "total_requests": total,
                "successful_requests": success,
                "failed_requests": total - success,
                "success_rate": round(success / total, 4) if total else 0.0,
                "error_rate": round((total - success) / total, 4) if total else 0.0,
                "avg_latency_ms": round(mean_value(latencies), 3),
                "median_latency_ms": round(median_value(latencies), 3),
                "p50_latency_ms": round(p50(latencies), 3),
                "p95_latency_ms": round(p95(latencies), 3),
                "p99_latency_ms": round(p99(latencies), 3),
                "latency_std_dev_ms": round(std_dev(latencies), 3),
                "total_cost_usd": round(sum(costs), 6),
                "avg_cost_usd": round(sum(costs) / total, 6) if total else 0.0,
                "premium_usage_rate": round(premium / total, 4) if total else 0.0,
                "fallback_count": fallback,
                "reroute_or_downgrade_count": reroute,
                "reject_count": reject,
                "provider_distribution": {
                    provider: round(count / success, 4) if success else 0.0
                    for provider, count in sorted(provider_count.items())
                },
            }
        )
    return out


def write_results_jsonl(path: Path, rows: list[ReplayResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row.to_dict()) + "\n")


def write_summary_csv(path: Path, summary: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not summary:
        path.write_text("", encoding="utf-8")
        return
    keys = list(summary[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(summary)


def aggregate_by_policy(summary_rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in summary_rows:
        grouped[row["policy_name"]].append(row)
    out: dict[str, dict[str, float]] = {}
    for policy, rows in grouped.items():
        out[policy] = {
            "avg_latency_ms": round(mean_value([float(r["avg_latency_ms"]) for r in rows]), 3),
            "avg_cost_usd": round(mean_value([float(r["avg_cost_usd"]) for r in rows]), 6),
            "avg_error_rate": round(mean_value([float(r["error_rate"]) for r in rows]), 4),
            "run_count": float(len(rows)),
        }
    return out
