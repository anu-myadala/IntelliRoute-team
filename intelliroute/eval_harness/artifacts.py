from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .types import ReplayResult


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def build_run_id(*, scenario: str, policy: str, seed: int) -> str:
    return f"{utc_timestamp()}_{scenario}_{policy}_seed{seed}"


def build_matrix_id() -> str:
    return utc_timestamp()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_log(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_metrics_csv(path: Path, rows: list[ReplayResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "request_id",
        "scenario_name",
        "policy_name",
        "seed",
        "timestamp_utc",
        "tenant_id",
        "team_id",
        "workflow_id",
        "provider",
        "latency_ms",
        "success",
        "detail",
        "estimated_cost_usd",
        "fallback_used",
        "premium_used",
        "reroute_or_downgrade",
        "reject",
        "brownout_degraded",
        "budget_actions",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            d = row.to_dict()
            d["budget_actions"] = json.dumps(d.get("budget_actions", []))
            writer.writerow({k: d.get(k, "") for k in fields})


def write_timeline_csv(path: Path, points: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "timestamp_sec",
        "requests_completed",
        "avg_latency_ms",
        "p95_latency_ms",
        "error_rate",
        "queue_depth",
        "brownout_active",
        "active_breakers",
        "total_cost",
        "requests_shed",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in points:
            writer.writerow({k: row.get(k, 0) for k in fields})
