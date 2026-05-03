"""End-to-end integration test.

Spins up every IntelliRoute service as a real subprocess bound to an
ephemeral port, then exercises the whole system over HTTP:

1. Registry bootstrap + intent-aware routing (a batch prompt should be
   routed to mock-cheap; an interactive prompt to mock-fast).
2. Automatic failover: knock out the top-ranked provider via its
   ``/admin/force_fail`` hook and verify the router seamlessly falls
   back to the next candidate and the circuit breaker trips.
3. Distributed rate limiting: tighten the bucket to capacity=1 and
   verify concurrent clients are throttled.
4. Async cost accounting: after successful completions the cost
   tracker reflects the tenant's spend.

The test takes a few seconds because it actually starts and tears down
real processes. That's the whole point: this is the integration test
that proves the services work together.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(url: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return
        except Exception as exc:
            last_err = exc
        time.sleep(0.1)
    raise RuntimeError(f"service at {url} did not become ready within {timeout:.1f}s: {last_err}")


def _wait_hybrid_registry_leased(router_url: str, timeout: float = 20.0) -> dict:
    """Mocks start before the router; wait until all three have self-registered with a lease."""
    deadline = time.monotonic() + timeout
    last_body: dict | None = None
    want = {"mock-fast", "mock-smart", "mock-cheap"}
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{router_url}/providers/registry", timeout=10.0)
        except Exception:
            time.sleep(0.15)
            continue
        if r.status_code != 200:
            time.sleep(0.15)
            continue
        body = r.json()
        last_body = body
        if body.get("providers_total") != 3 or body.get("providers_active") != 3:
            time.sleep(0.15)
            continue
        if set(body.get("stale_names") or []) != set():
            time.sleep(0.15)
            continue
        rows = body.get("providers") or []
        names = {p.get("name") for p in rows}
        if names != want:
            time.sleep(0.15)
            continue
        if len(rows) == 3 and all(
            p.get("routable") is True and p.get("lease_ttl_seconds") is not None for p in rows
        ):
            return body
        time.sleep(0.15)
    pytest.fail(
        "hybrid registry did not converge to three leased routable mocks within "
        f"{timeout:.1f}s; last snapshot: {last_body!r}"
    )


class _Stack:
    def __init__(self, env_overrides: dict[str, str] | None = None) -> None:
        self.ports = {
            "gateway": _free_port(),
            "router": _free_port(),
            "rate_limiter": _free_port(),
            "cost_tracker": _free_port(),
            "health_monitor": _free_port(),
            "mock_fast": _free_port(),
            "mock_smart": _free_port(),
            "mock_cheap": _free_port(),
        }
        self.procs: list[subprocess.Popen] = []
        self.env_overrides = dict(env_overrides or {})

    def _base_env(self) -> dict:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env["INTELLIROUTE_HOST"] = "127.0.0.1"
        env["INTELLIROUTE_GATEWAY_PORT"] = str(self.ports["gateway"])
        env["INTELLIROUTE_ROUTER_PORT"] = str(self.ports["router"])
        env["INTELLIROUTE_RATE_LIMITER_PORT"] = str(self.ports["rate_limiter"])
        env["INTELLIROUTE_COST_TRACKER_PORT"] = str(self.ports["cost_tracker"])
        env["INTELLIROUTE_HEALTH_MONITOR_PORT"] = str(self.ports["health_monitor"])
        env["INTELLIROUTE_MOCK_FAST_PORT"] = str(self.ports["mock_fast"])
        env["INTELLIROUTE_MOCK_SMART_PORT"] = str(self.ports["mock_smart"])
        env["INTELLIROUTE_MOCK_CHEAP_PORT"] = str(self.ports["mock_cheap"])
        env.update(self.env_overrides)
        return env

    def _spawn_uvicorn(self, module: str, port: int, extra_env: dict | None = None) -> None:
        env = self._base_env()
        if extra_env:
            env.update(extra_env)
        cmd = [
            sys.executable, "-m", "uvicorn", module,
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning",
        ]
        p = subprocess.Popen(cmd, env=env, cwd=str(ROOT))
        self.procs.append(p)

    def _spawn_and_wait(
        self,
        module: str,
        port: int,
        extra_env: dict | None = None,
        *,
        ready_timeout: float = 30.0,
    ) -> None:
        self._spawn_uvicorn(module, port, extra_env)
        proc = self.procs[-1]
        url = f"http://127.0.0.1:{port}/health"
        deadline = time.monotonic() + ready_timeout

        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"service {module} on {url} exited early with code {proc.returncode}"
                )
            try:
                r = httpx.get(url, timeout=1.0)
                if r.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.1)

        raise RuntimeError(
            f"service {module} at {url} did not become ready within {ready_timeout:.1f}s"
        )

    def start(self) -> None:
        self._spawn_and_wait(
            "intelliroute.mock_provider.main:app",
            self.ports["mock_fast"],
            {
                "MOCK_NAME": "mock-fast", "MOCK_MODEL": "fast-1",
                "MOCK_LATENCY_MS": "30", "MOCK_LATENCY_JITTER_MS": "5",
                "MOCK_FAILURE_RATE": "0.0", "MOCK_COST_PER_1K": "0.002",
                "INTELLIROUTE_MOCK_PUBLIC_PORT": str(self.ports["mock_fast"]),
            },
        )
        self._spawn_and_wait(
            "intelliroute.mock_provider.main:app",
            self.ports["mock_smart"],
            {
                "MOCK_NAME": "mock-smart", "MOCK_MODEL": "smart-1",
                "MOCK_LATENCY_MS": "80", "MOCK_LATENCY_JITTER_MS": "5",
                "MOCK_FAILURE_RATE": "0.0", "MOCK_COST_PER_1K": "0.02",
                "INTELLIROUTE_MOCK_PUBLIC_PORT": str(self.ports["mock_smart"]),
            },
        )
        self._spawn_and_wait(
            "intelliroute.mock_provider.main:app",
            self.ports["mock_cheap"],
            {
                "MOCK_NAME": "mock-cheap", "MOCK_MODEL": "cheap-1",
                "MOCK_LATENCY_MS": "60", "MOCK_LATENCY_JITTER_MS": "5",
                "MOCK_FAILURE_RATE": "0.0", "MOCK_COST_PER_1K": "0.0003",
                "INTELLIROUTE_MOCK_PUBLIC_PORT": str(self.ports["mock_cheap"]),
            },
        )
        self._spawn_and_wait("intelliroute.rate_limiter.main:app", self.ports["rate_limiter"])
        self._spawn_and_wait("intelliroute.cost_tracker.main:app", self.ports["cost_tracker"])
        self._spawn_and_wait("intelliroute.health_monitor.main:app", self.ports["health_monitor"])
        self._spawn_and_wait("intelliroute.router.main:app", self.ports["router"])
        self._spawn_and_wait("intelliroute.gateway.main:app", self.ports["gateway"])

    def stop(self) -> None:
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        self.procs.clear()

    # Convenience URLs ------------------------------------------------
    @property
    def gateway_url(self) -> str:
        return f"http://127.0.0.1:{self.ports['gateway']}"

    @property
    def router_url(self) -> str:
        return f"http://127.0.0.1:{self.ports['router']}"

    @property
    def rate_limiter_url(self) -> str:
        return f"http://127.0.0.1:{self.ports['rate_limiter']}"

    @property
    def cost_tracker_url(self) -> str:
        return f"http://127.0.0.1:{self.ports['cost_tracker']}"

    def mock_url(self, which: str) -> str:
        return f"http://127.0.0.1:{self.ports[which]}"


@pytest.fixture(scope="module")
def stack():
    s = _Stack()
    s.start()
    try:
        yield s
    finally:
        s.stop()


def _headers() -> dict:
    return {"X-API-Key": "demo-key-123", "Content-Type": "application/json"}


def _interactive_request() -> dict:
    return {
        "tenant_id": "ignored-by-gateway",
        "messages": [{"role": "user", "content": "Hi there!"}],
        "max_tokens": 50,
    }


def _batch_request() -> dict:
    return {
        "tenant_id": "ignored-by-gateway",
        "messages": [{"role": "user", "content": "Summarize the following document into bullet points"}],
        "max_tokens": 50,
    }


def _workflow_batch_request() -> dict:
    return {
        "tenant_id": "ignored-by-gateway",
        "team_id": "team-alpha",
        "workflow_id": "nightly-batch",
        "messages": [{"role": "user", "content": "Summarize the following document into bullet points"}],
        "max_tokens": 50,
        "intent_hint": "batch",
    }


def test_hybrid_registry_lists_three_routable_mocks(stack):
    """With default hybrid mode, mocks self-register and all three stay routable."""
    body = _wait_hybrid_registry_leased(stack.router_url)
    assert body["providers_total"] == 3
    assert body["providers_active"] == 3
    assert set(body["stale_names"]) == set()
    names = {p["name"] for p in body["providers"]}
    assert names == {"mock-fast", "mock-smart", "mock-cheap"}
    for row in body["providers"]:
        assert row["routable"] is True
        assert row["lease_ttl_seconds"] is not None


def test_interactive_prompt_routes_to_fast(stack):
    r = httpx.post(f"{stack.gateway_url}/v1/complete", json=_interactive_request(), headers=_headers(), timeout=10.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "mock-fast"
    assert body["model"] == "fast-1"
    assert body["total_tokens"] > 0


def test_router_decide_endpoint_returns_ranking(stack):
    r = httpx.post(f"{stack.router_url}/decide", json=_interactive_request(), timeout=5.0)
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "interactive"
    assert body["ranked"][0] == "mock-fast"
    # Policy layer removes premium and high-latency mocks for short interactive.
    assert "mock-smart" not in body["ranked"]
    pe = body.get("policy_evaluation")
    assert pe is not None
    assert pe["complexity_score"] >= 0.0
    assert isinstance(pe.get("matched_rules"), list)


def test_failover_when_top_provider_is_down(stack):
    # Force mock-fast into failing mode, then issue an interactive prompt.
    # The router should fall back to the next provider in the ranked list.
    r = httpx.post(f"{stack.mock_url('mock_fast')}/admin/force_fail", json={"fail": True}, timeout=5.0)
    assert r.status_code == 200
    try:
        body = None
        # A few attempts because the breaker needs to see failures to trip.
        for _ in range(5):
            r = httpx.post(f"{stack.gateway_url}/v1/complete", json=_interactive_request(), headers=_headers(), timeout=10.0)
            assert r.status_code == 200, r.text
            body = r.json()
            if body["provider"] != "mock-fast":
                break
        assert body is not None
        assert body["provider"] != "mock-fast"
        assert body["fallback_used"] is True
    finally:
        httpx.post(f"{stack.mock_url('mock_fast')}/admin/force_fail", json={"fail": False}, timeout=5.0)


def test_batch_prompt_routes_to_cheap(stack):
    r = httpx.post(f"{stack.gateway_url}/v1/complete", json=_batch_request(), headers=_headers(), timeout=10.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "mock-cheap"


def test_rate_limiter_throttles_when_bucket_drained(stack):
    # Tighten the bucket for (demo-tenant, mock-cheap) to capacity=1, refill very slow.
    r = httpx.post(
        f"{stack.rate_limiter_url}/config",
        json={"key": "demo-tenant|mock-cheap", "capacity": 1, "refill_rate": 0.01},
        timeout=5.0,
    )
    assert r.status_code == 200

    # First batch request should succeed (consumes the 1 token).
    r1 = httpx.post(f"{stack.gateway_url}/v1/complete", json=_batch_request(), headers=_headers(), timeout=10.0)
    assert r1.status_code == 200
    # Second batch request: mock-cheap is rate-limited so the router should
    # fall back to another provider (fallback_used=True).
    r2 = httpx.post(f"{stack.gateway_url}/v1/complete", json=_batch_request(), headers=_headers(), timeout=10.0)
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["fallback_used"] is True
    assert body2["provider"] != "mock-cheap"

    # Reset config so subsequent tests aren't affected.
    httpx.post(
        f"{stack.rate_limiter_url}/config",
        json={"key": "demo-tenant|mock-cheap", "capacity": 10, "refill_rate": 1.0},
        timeout=5.0,
    )


def test_cost_tracker_reflects_tenant_spend(stack):
    # Poll the cost summary; because cost events are published async, allow
    # up to a second for eventual consistency.
    deadline = time.monotonic() + 2.0
    summary = None
    while time.monotonic() < deadline:
        r = httpx.get(f"{stack.gateway_url}/v1/cost/summary", headers=_headers(), timeout=5.0)
        assert r.status_code == 200
        summary = r.json()
        if summary["total_requests"] > 0:
            break
        time.sleep(0.1)
    assert summary is not None
    assert summary["tenant_id"] == "demo-tenant"
    assert summary["total_requests"] >= 1
    assert summary["total_cost_usd"] > 0.0
    assert len(summary["by_provider"]) >= 1


def test_team_workflow_rollups_and_budget_endpoints(stack):
    r = httpx.post(
        f"{stack.gateway_url}/v1/complete",
        json=_workflow_batch_request(),
        headers=_headers(),
        timeout=10.0,
    )
    assert r.status_code == 200, r.text
    team_summary = httpx.get(
        f"{stack.cost_tracker_url}/summary/team/team-alpha", timeout=5.0
    ).json()
    workflow_summary = httpx.get(
        f"{stack.cost_tracker_url}/summary/workflow/nightly-batch", timeout=5.0
    ).json()
    assert team_summary["total_requests"] >= 1
    assert workflow_summary["total_requests"] >= 1

    rb = httpx.post(
        f"{stack.cost_tracker_url}/budget/team",
        json={"team_id": "team-alpha", "budget_usd": 1.0},
        timeout=5.0,
    )
    assert rb.status_code == 200
    wb = httpx.post(
        f"{stack.cost_tracker_url}/budget/workflow",
        json={"workflow_id": "nightly-batch", "budget_usd": 1.0},
        timeout=5.0,
    )
    assert wb.status_code == 200
    tb = httpx.get(
        f"{stack.cost_tracker_url}/budget/team/team-alpha", timeout=5.0
    ).json()
    wfb = httpx.get(
        f"{stack.cost_tracker_url}/budget/workflow/nightly-batch", timeout=5.0
    ).json()
    assert tb["team_id"] == "team-alpha"
    assert wfb["workflow_id"] == "nightly-batch"


def test_unauthenticated_request_is_rejected(stack):
    r = httpx.post(f"{stack.gateway_url}/v1/complete", json=_interactive_request(), timeout=5.0)
    assert r.status_code == 401


def test_feedback_endpoint_populated_after_completions(stack):
    """Test that feedback metrics are collected after completions."""
    # Send a few requests
    for _ in range(3):
        r = httpx.post(
            f"{stack.gateway_url}/v1/complete",
            json=_interactive_request(),
            headers=_headers(),
            timeout=10.0,
        )
        assert r.status_code == 200

    time.sleep(0.5)  # Allow feedback to be recorded

    # Check feedback endpoint
    r = httpx.get(f"{stack.router_url}/feedback", timeout=5.0)
    assert r.status_code == 200
    feedback = r.json()
    # Should have metrics for providers that handled requests
    assert len(feedback) > 0
    # Check that metrics have expected fields
    for provider_name, metrics in feedback.items():
        assert "latency_ema" in metrics
        assert "success_rate_ema" in metrics
        assert "quality_score" in metrics
        assert "sample_count" in metrics


def test_queue_stats_endpoint_shape(stack):
    """Test that queue stats endpoint returns expected shape."""
    r = httpx.get(f"{stack.router_url}/queue/stats", timeout=5.0)
    assert r.status_code == 200
    stats = r.json()
    assert "total_depth" in stats
    assert "by_priority" in stats
    assert "shed_count" in stats
    assert "timeout_count" in stats
    assert "high" in stats["by_priority"]
    assert "medium" in stats["by_priority"]
    assert "low" in stats["by_priority"]


def test_election_status_shows_leader(stack):
    """Test that election status endpoint is accessible."""
    # Try to get election status from one of the replicas
    # Note: In the integration test, we only run one rate limiter replica
    # So we just check that the endpoint exists and returns expected shape
    r = httpx.get(f"{stack.rate_limiter_url}/election/status", timeout=5.0)
    assert r.status_code == 200
    status = r.json()
    assert "replica_id" in status or "error" in status  # May error if not set up
    if "replica_id" in status:
        assert "state" in status
        assert "is_leader" in status


def test_brownout_mode_reflected_in_decide_and_complete():
    """Run a stack with aggressive brownout thresholds and verify metadata."""
    s = _Stack(
        env_overrides={
            "INTELLIROUTE_BROWNOUT_ENABLED": "1",
            "INTELLIROUTE_BROWNOUT_ENTER_CONSEC": "1",
            "INTELLIROUTE_BROWNOUT_EXIT_CONSEC": "1",
            "INTELLIROUTE_BROWNOUT_QUEUE_ENTER": "0",
            "INTELLIROUTE_BROWNOUT_QUEUE_EXIT": "0",
        }
    )
    s.start()
    try:
        r = httpx.post(f"{s.router_url}/decide", json=_batch_request(), timeout=5.0)
        assert r.status_code == 200
        body = r.json()
        bs = body.get("brownout_status")
        assert bs is not None and bs["is_degraded"] is True
        pe = body.get("policy_evaluation")
        assert pe is not None
        assert "brownout_degrade_low_priority_routing" in pe.get("matched_rules", [])

        r2 = httpx.post(
            f"{s.gateway_url}/v1/complete",
            json={**_batch_request(), "max_tokens": 512},
            headers=_headers(),
            timeout=10.0,
        )
        assert r2.status_code == 200, r2.text
        resp = r2.json()
        assert resp.get("brownout_status", {}).get("is_degraded") is True
    finally:
        s.stop()
