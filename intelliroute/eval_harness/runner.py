from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import random
import time
from typing import Iterable

import httpx

from .metrics import aggregate_summary
from .statistics import mean_value, p95
from .timeline import TimelineSampler
from .types import ReplayResult, WorkloadRequest


@dataclass
class ReplayRunOutput:
    rows: list[ReplayResult]
    reset_report: dict
    setup_report: dict
    run_log: list[str]
    duration_seconds: float
    scenario_name: str
    policy_name: str
    seed: int


class ResetFailure(RuntimeError):
    def __init__(self, message: str, report: dict, run_log: list[str]) -> None:
        super().__init__(message)
        self.report = report
        self.run_log = run_log


@dataclass(frozen=True)
class ScenarioConfig:
    tenant_budget_usd: float
    team_budget_usd: float
    team_premium_cap_usd: float
    workflow_budget_usd: float
    provider_capacity: float
    provider_refill_rate: float


_SCENARIO_CONFIGS: dict[str, ScenarioConfig] = {
    "normal_mixed": ScenarioConfig(2.0, 1.0, 1.0, 1.0, 120.0, 60.0),
    "degraded_provider": ScenarioConfig(2.0, 1.0, 1.0, 1.0, 120.0, 60.0),
    "budget_pressure": ScenarioConfig(0.18, 0.12, 0.05, 0.07, 120.0, 60.0),
    "overload_brownout": ScenarioConfig(1.0, 0.5, 0.3, 0.25, 25.0, 10.0),
}


def run_replay(
    *,
    scenario_name: str,
    policy_name: str,
    gateway_url: str,
    router_url: str,
    cost_tracker_url: str,
    health_monitor_url: str,
    rate_limiter_urls: list[str],
    api_key: str,
    requests: list[WorkloadRequest],
    seed: int,
    concurrency: int = 1,
    timeline_interval_s: float = 1.0,
    reset_before_run: bool = True,
) -> ReplayRunOutput:
    started = time.monotonic()
    run_log = [f"{_now_utc()} run_start scenario={scenario_name} policy={policy_name} seed={seed}"]
    reset_report: dict = {"attempted": False, "ok": True}
    setup_report: dict = {"scenario": scenario_name}
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    rng = random.Random(seed)

    with httpx.Client(timeout=20.0) as client:
        if reset_before_run:
            reset_report = _reset_for_run(
                client=client,
                router_url=router_url,
                cost_tracker_url=cost_tracker_url,
                health_monitor_url=health_monitor_url,
                rate_limiter_urls=rate_limiter_urls,
                run_log=run_log,
            )
        _set_mode(client, router_url, policy_name)
        setup_report = _prepare_scenario(
            client=client,
            scenario_name=scenario_name,
            gateway_url=gateway_url,
            cost_tracker_url=cost_tracker_url,
            rate_limiter_urls=rate_limiter_urls,
            run_log=run_log,
        )
        progress = {
            "requests_completed": 0.0,
            "avg_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
            "error_rate": 0.0,
            "total_cost": 0.0,
        }
        sampler = TimelineSampler(
            interval_s=timeline_interval_s,
            progress_fn=lambda: dict(progress),
            status_fn=_build_status_probe(client, router_url, health_monitor_url),
        )
        sampler.start()
        try:
            rows = _execute_requests(
                client=client,
                requests=requests,
                headers=headers,
                scenario_name=scenario_name,
                policy_name=policy_name,
                gateway_url=gateway_url,
                concurrency=max(1, int(concurrency)),
                seed=seed,
                rng=rng,
                progress_ref=progress,
            )
        finally:
            sampler.stop()
        _cleanup_scenario(client, scenario_name, gateway_url)

    duration = time.monotonic() - started
    run_log.append(f"{_now_utc()} run_complete requests={len(rows)} duration_s={duration:.3f}")
    return ReplayRunOutput(
        rows=rows,
        reset_report=reset_report,
        setup_report={**setup_report, "timeline": sampler.points()},
        run_log=run_log,
        duration_seconds=duration,
        scenario_name=scenario_name,
        policy_name=policy_name,
        seed=seed,
    )


def run_matrix(
    *,
    scenarios: Iterable[str],
    policies: Iterable[str],
    gateway_url: str,
    router_url: str,
    cost_tracker_url: str,
    health_monitor_url: str,
    rate_limiter_urls: list[str],
    api_key: str,
    workload_by_scenario: dict[str, list[WorkloadRequest]],
    seeds: list[int],
    concurrency: int = 1,
    timeline_interval_s: float = 1.0,
    reset_before_run: bool = True,
) -> list[ReplayRunOutput]:
    runs: list[ReplayRunOutput] = []
    for scenario in scenarios:
        for seed in seeds:
            for policy in policies:
                runs.append(
                    run_replay(
                        scenario_name=scenario,
                        policy_name=policy,
                        gateway_url=gateway_url,
                        router_url=router_url,
                        cost_tracker_url=cost_tracker_url,
                        health_monitor_url=health_monitor_url,
                        rate_limiter_urls=rate_limiter_urls,
                        api_key=api_key,
                        requests=workload_by_scenario[scenario],
                        seed=seed,
                        concurrency=concurrency,
                        timeline_interval_s=timeline_interval_s,
                        reset_before_run=reset_before_run,
                    )
                )
    return runs


def aggregate_matrix_runs(runs: list[ReplayRunOutput]) -> dict:
    all_rows: list[ReplayResult] = []
    for r in runs:
        all_rows.extend(r.rows)
    summary = aggregate_summary(all_rows)
    best = max(summary, key=lambda x: x["success_rate"], default={})
    worst = min(summary, key=lambda x: x["success_rate"], default={})
    policy_rollup: dict[str, dict[str, float]] = {}
    for row in summary:
        p = row["policy_name"]
        if p not in policy_rollup:
            policy_rollup[p] = {"latency": 0.0, "cost": 0.0, "error": 0.0, "count": 0.0}
        policy_rollup[p]["latency"] += float(row["avg_latency_ms"])
        policy_rollup[p]["cost"] += float(row["avg_cost_usd"])
        policy_rollup[p]["error"] += float(row["error_rate"])
        policy_rollup[p]["count"] += 1.0
    for vals in policy_rollup.values():
        c = max(1.0, vals["count"])
        vals["avg_latency_ms"] = round(vals["latency"] / c, 3)
        vals["avg_cost_usd"] = round(vals["cost"] / c, 6)
        vals["avg_error_rate"] = round(vals["error"] / c, 4)
    return {
        "scenario_policy_summary": summary,
        "policy_averages": policy_rollup,
        "best_run": best,
        "worst_run": worst,
        "run_counts": len(runs),
        "seed_counts": len({r.seed for r in runs}),
        "total_experiment_duration_s": round(sum(r.duration_seconds for r in runs), 3),
    }


def _reset_for_run(
    *,
    client: httpx.Client,
    router_url: str,
    cost_tracker_url: str,
    health_monitor_url: str,
    rate_limiter_urls: list[str],
    run_log: list[str],
) -> dict:
    report = {
        "timestamp_utc": _now_utc(),
        "router_reset": "pending",
        "cost_tracker_reset": "pending",
        "health_monitor_reset": "pending",
        "rate_limiter_resets": {},
        "provider_recovery": {},
        "ok": True,
        "errors": [],
    }
    run_log.append(f"{_now_utc()} reset_start")
    for admin_url in _mock_provider_admin_urls():
        try:
            r = client.post(admin_url, json={"fail": False}, timeout=2.0)
            report["provider_recovery"][admin_url] = "ok" if r.status_code == 200 else f"status_{r.status_code}"
        except Exception as exc:
            report["provider_recovery"][admin_url] = f"error:{exc}"
            report["errors"].append(f"provider_recovery:{admin_url}:{exc}")

    report["router_reset"] = _post_reset(
        client,
        f"{router_url}/reset",
        {"reset_feedback": True, "reset_brownout": True, "reset_queue": True, "reset_tuner": True, "reset_routing_mode": True},
        "router",
        report,
    )
    report["cost_tracker_reset"] = _post_reset(
        client, f"{cost_tracker_url}/reset", {"clear_budgets": True}, "cost_tracker", report
    )
    report["health_monitor_reset"] = _post_reset(
        client, f"{health_monitor_url}/reset", {}, "health_monitor", report
    )
    for rl_url in rate_limiter_urls:
        report["rate_limiter_resets"][rl_url] = _post_reset(
            client,
            f"{rl_url}/reset",
            {"clear_configs": True, "clear_log": True},
            f"rate_limiter:{rl_url}",
            report,
        )
    report["ok"] = len(report["errors"]) == 0
    run_log.append(f"{_now_utc()} reset_done ok={report['ok']}")
    if not report["ok"]:
        raise ResetFailure("reset failed", report=report, run_log=run_log)
    return report


def _prepare_scenario(
    *,
    client: httpx.Client,
    scenario_name: str,
    gateway_url: str,
    cost_tracker_url: str,
    rate_limiter_urls: list[str],
    run_log: list[str],
) -> dict:
    cfg = _SCENARIO_CONFIGS[scenario_name]
    run_log.append(f"{_now_utc()} scenario_setup_start scenario={scenario_name}")
    for rl_url in rate_limiter_urls:
        for provider in ("mock-fast", "mock-cheap", "mock-smart"):
            client.post(
                f"{rl_url}/config/provider",
                json={"provider": provider, "capacity": cfg.provider_capacity, "refill_rate": cfg.provider_refill_rate},
            )
    if scenario_name == "degraded_provider":
        degraded_admin_url = os.environ.get(
            "INTELLIROUTE_DEGRADED_PROVIDER_ADMIN_URL",
            "http://127.0.0.1:9002/admin/force_fail",
        )
        client.post(degraded_admin_url, json={"fail": True})
    if scenario_name == "budget_pressure":
        client.post(f"{cost_tracker_url}/budget", json={"tenant_id": "demo-tenant", "budget_usd": cfg.tenant_budget_usd})
        client.post(f"{cost_tracker_url}/budget/team", json={"team_id": "team-alpha", "budget_usd": cfg.team_budget_usd})
        client.post(
            f"{cost_tracker_url}/budget/team/premium-cap",
            json={"team_id": "team-alpha", "premium_cap_usd": cfg.team_premium_cap_usd},
        )
        client.post(
            f"{cost_tracker_url}/budget/workflow",
            json={"workflow_id": "nightly-batch", "budget_usd": cfg.workflow_budget_usd},
        )
    client.get(f"{gateway_url}/health")
    run_log.append(f"{_now_utc()} scenario_setup_done")
    return {
        "scenario": scenario_name,
        "provider_capacity": cfg.provider_capacity,
        "provider_refill_rate": cfg.provider_refill_rate,
        "tenant_budget_usd": cfg.tenant_budget_usd,
        "team_budget_usd": cfg.team_budget_usd,
        "team_premium_cap_usd": cfg.team_premium_cap_usd,
        "workflow_budget_usd": cfg.workflow_budget_usd,
    }


def _cleanup_scenario(client: httpx.Client, scenario: str, gateway_url: str) -> None:
    if scenario == "degraded_provider":
        try:
            degraded_admin_url = os.environ.get(
                "INTELLIROUTE_DEGRADED_PROVIDER_ADMIN_URL",
                "http://127.0.0.1:9002/admin/force_fail",
            )
            client.post(degraded_admin_url, json={"fail": False})
        except Exception:
            pass
    client.get(f"{gateway_url}/health")


def _set_mode(client: httpx.Client, router_url: str, mode: str) -> None:
    r = client.post(f"{router_url}/routing/mode", json={"mode": mode})
    r.raise_for_status()


def _is_json(resp: httpx.Response) -> bool:
    return "application/json" in resp.headers.get("content-type", "")


def _post_reset(client: httpx.Client, url: str, body: dict, label: str, report: dict) -> str:
    try:
        r = client.post(url, json=body, timeout=4.0)
        if r.status_code != 200:
            report["errors"].append(f"{label}:status_{r.status_code}")
            return f"status_{r.status_code}"
        return "ok"
    except Exception as exc:
        report["errors"].append(f"{label}:{exc}")
        return f"error:{exc}"


def _mock_provider_admin_urls() -> list[str]:
    configured = os.environ.get("INTELLIROUTE_MOCK_PROVIDER_ADMIN_URLS", "").strip()
    if configured:
        return [u.strip() for u in configured.split(",") if u.strip()]
    return [
        "http://127.0.0.1:9001/admin/force_fail",
        "http://127.0.0.1:9002/admin/force_fail",
        "http://127.0.0.1:9003/admin/force_fail",
    ]


def _build_status_probe(client: httpx.Client, router_url: str, health_monitor_url: str):
    def _probe() -> dict[str, float]:
        queue_depth = 0
        requests_shed = 0
        brownout_active = 0
        active_breakers = 0
        try:
            r = client.get(f"{router_url}/queue/stats", timeout=2.0)
            if r.status_code == 200:
                b = r.json()
                queue_depth = int(b.get("total_depth", 0))
                requests_shed = int(b.get("shed_count", 0))
        except Exception:
            pass
        try:
            r = client.get(f"{router_url}/brownout", timeout=2.0)
            if r.status_code == 200:
                brownout_active = int(bool(r.json().get("is_degraded", False)))
        except Exception:
            pass
        try:
            r = client.get(f"{health_monitor_url}/snapshot", timeout=2.0)
            if r.status_code == 200:
                snap = r.json()
                active_breakers = sum(1 for _, v in snap.items() if v.get("circuit_state") == "open")
        except Exception:
            pass
        return {
            "queue_depth": float(queue_depth),
            "requests_shed": float(requests_shed),
            "brownout_active": float(brownout_active),
            "active_breakers": float(active_breakers),
        }

    return _probe


def _execute_requests(
    *,
    client: httpx.Client,
    requests: list[WorkloadRequest],
    headers: dict[str, str],
    scenario_name: str,
    policy_name: str,
    gateway_url: str,
    concurrency: int,
    seed: int,
    rng: random.Random,
    progress_ref: dict[str, float],
) -> list[ReplayResult]:
    if concurrency <= 1:
        rows: list[ReplayResult] = []
        for req in requests:
            time.sleep(rng.uniform(0.0, 8.0) / 1000.0)
            rows.append(
                _single_request(
                    client=client,
                    req=req,
                    headers=headers,
                    scenario_name=scenario_name,
                    policy_name=policy_name,
                    gateway_url=gateway_url,
                    seed=seed,
                )
            )
            _update_progress(progress_ref, rows)
        return rows
    rows = asyncio.run(
        _run_async_batch(
            requests=requests,
            headers=headers,
            scenario_name=scenario_name,
            policy_name=policy_name,
            gateway_url=gateway_url,
            concurrency=concurrency,
            seed=seed,
        )
    )
    _update_progress(progress_ref, rows)
    return rows


def _update_progress(progress_ref: dict[str, float], rows: list[ReplayResult]) -> None:
    completed = len(rows)
    latencies = [r.latency_ms for r in rows if r.latency_ms > 0]
    failures = sum(1 for r in rows if not r.success)
    progress_ref["requests_completed"] = float(completed)
    progress_ref["avg_latency_ms"] = mean_value(latencies)
    progress_ref["p95_latency_ms"] = p95(latencies)
    progress_ref["error_rate"] = (failures / completed) if completed else 0.0
    progress_ref["total_cost"] = sum(r.estimated_cost_usd for r in rows)


def _single_request(
    *,
    client: httpx.Client,
    req: WorkloadRequest,
    headers: dict[str, str],
    scenario_name: str,
    policy_name: str,
    gateway_url: str,
    seed: int,
) -> ReplayResult:
    start = time.monotonic()
    status = 0
    ts = _now_utc()
    try:
        resp = client.post(f"{gateway_url}/v1/complete", json=req.to_payload(), headers=headers)
        latency_ms = (time.monotonic() - start) * 1000.0
        status = resp.status_code
        body = resp.json() if _is_json(resp) else {}
        ok = status == 200
        policy_eval = body.get("policy_evaluation", {}) if isinstance(body, dict) else {}
        provider = body.get("provider") if isinstance(body, dict) else None
        fallback = bool(body.get("fallback_used", False)) if isinstance(body, dict) else False
        budget_actions = policy_eval.get("budget_actions", []) if isinstance(policy_eval, dict) else []
        return ReplayResult(
            request_id=req.request_id,
            scenario_name=scenario_name,
            policy_name=policy_name,
            success=ok,
            status_code=status,
            provider=provider,
            latency_ms=round(float(body.get("latency_ms", latency_ms)) if ok else latency_ms, 2),
            estimated_cost_usd=float(body.get("estimated_cost_usd", 0.0)) if ok else 0.0,
            fallback_used=fallback,
            premium_used=bool(provider and ("smart" in provider or "gemini" in provider)),
            reject=(status in {429, 503, 504}),
            reroute_or_downgrade=fallback or bool(budget_actions),
            brownout_degraded=bool((body.get("brownout_status") or {}).get("is_degraded", False))
            if isinstance(body, dict)
            else False,
            budget_actions=budget_actions if isinstance(budget_actions, list) else [],
            detail=str(body.get("detail", "")) if isinstance(body, dict) else "",
            timestamp_utc=ts,
            tenant_id=req.tenant_id,
            team_id=req.team_id or "",
            workflow_id=req.workflow_id or "",
            seed=seed,
        )
    except Exception as exc:
        return ReplayResult(
            request_id=req.request_id,
            scenario_name=scenario_name,
            policy_name=policy_name,
            success=False,
            status_code=status,
            provider=None,
            latency_ms=round((time.monotonic() - start) * 1000.0, 2),
            estimated_cost_usd=0.0,
            fallback_used=False,
            premium_used=False,
            reject=True,
            reroute_or_downgrade=False,
            brownout_degraded=False,
            budget_actions=[],
            detail=str(exc),
            timestamp_utc=ts,
            tenant_id=req.tenant_id,
            team_id=req.team_id or "",
            workflow_id=req.workflow_id or "",
            seed=seed,
        )


async def _run_async_batch(
    *,
    requests: list[WorkloadRequest],
    headers: dict[str, str],
    scenario_name: str,
    policy_name: str,
    gateway_url: str,
    concurrency: int,
    seed: int,
) -> list[ReplayResult]:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=20.0) as client:

        async def _call(req: WorkloadRequest) -> ReplayResult:
            async with sem:
                start = time.monotonic()
                status = 0
                ts = _now_utc()
                try:
                    resp = await client.post(f"{gateway_url}/v1/complete", json=req.to_payload(), headers=headers)
                    latency_ms = (time.monotonic() - start) * 1000.0
                    status = resp.status_code
                    body = resp.json() if _is_json(resp) else {}
                    ok = status == 200
                    policy_eval = body.get("policy_evaluation", {}) if isinstance(body, dict) else {}
                    provider = body.get("provider") if isinstance(body, dict) else None
                    fallback = bool(body.get("fallback_used", False)) if isinstance(body, dict) else False
                    budget_actions = (
                        policy_eval.get("budget_actions", []) if isinstance(policy_eval, dict) else []
                    )
                    return ReplayResult(
                        request_id=req.request_id,
                        scenario_name=scenario_name,
                        policy_name=policy_name,
                        success=ok,
                        status_code=status,
                        provider=provider,
                        latency_ms=round(float(body.get("latency_ms", latency_ms)) if ok else latency_ms, 2),
                        estimated_cost_usd=float(body.get("estimated_cost_usd", 0.0)) if ok else 0.0,
                        fallback_used=fallback,
                        premium_used=bool(provider and ("smart" in provider or "gemini" in provider)),
                        reject=(status in {429, 503, 504}),
                        reroute_or_downgrade=fallback or bool(budget_actions),
                        brownout_degraded=bool((body.get("brownout_status") or {}).get("is_degraded", False))
                        if isinstance(body, dict)
                        else False,
                        budget_actions=budget_actions if isinstance(budget_actions, list) else [],
                        detail=str(body.get("detail", "")) if isinstance(body, dict) else "",
                        timestamp_utc=ts,
                        tenant_id=req.tenant_id,
                        team_id=req.team_id or "",
                        workflow_id=req.workflow_id or "",
                        seed=seed,
                    )
                except Exception as exc:
                    return ReplayResult(
                        request_id=req.request_id,
                        scenario_name=scenario_name,
                        policy_name=policy_name,
                        success=False,
                        status_code=status,
                        provider=None,
                        latency_ms=round((time.monotonic() - start) * 1000.0, 2),
                        estimated_cost_usd=0.0,
                        fallback_used=False,
                        premium_used=False,
                        reject=True,
                        reroute_or_downgrade=False,
                        brownout_degraded=False,
                        budget_actions=[],
                        detail=str(exc),
                        timestamp_utc=ts,
                        tenant_id=req.tenant_id,
                        team_id=req.team_id or "",
                        workflow_id=req.workflow_id or "",
                        seed=seed,
                    )

        return await asyncio.gather(*[_call(req) for req in requests])


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
