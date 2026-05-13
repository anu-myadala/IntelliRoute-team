"""Dynamic-only registration: router starts empty, mock self-registers, TTL after stop."""
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

# Hard cap so a hung subprocess does not block CI indefinitely.
_MAX_WALL_S = 55.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_dynamic_register_then_stale_when_mock_stops():
    _t0 = time.monotonic()
    router_p = _free_port()
    mock_p = _free_port()
    env = os.environ.copy()
    env["INTELLIROUTE_SKIP_DOTENV"] = "1"
    for _k in ("GEMINI_API_KEY", "GROQ_API_KEY"):
        env.pop(_k, None)
    env["PYTHONPATH"] = str(ROOT)
    env["INTELLIROUTE_HOST"] = "127.0.0.1"
    env["INTELLIROUTE_ROUTER_PORT"] = str(router_p)
    env["INTELLIROUTE_ROUTER_URL"] = f"http://127.0.0.1:{router_p}"
    env["INTELLIROUTE_MOCK_REGISTRATION"] = "dynamic"
    env["INTELLIROUTE_PROVIDER_LEASE_TTL_SECONDS"] = "3"
    env["INTELLIROUTE_PROVIDER_HEARTBEAT_INTERVAL_SECONDS"] = "1"

    procs: list[subprocess.Popen] = []

    def spawn(module: str, port: int, extra: dict[str, str] | None = None) -> subprocess.Popen:
        e = env.copy()
        if extra:
            e.update(extra)
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            module,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]
        p = subprocess.Popen(cmd, env=e, cwd=str(ROOT))
        procs.append(p)
        return p

    try:
        spawn("intelliroute.router.main:app", router_p)
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            if time.monotonic() - _t0 > _MAX_WALL_S:
                pytest.fail("wall time exceeded waiting for router")
            try:
                r = httpx.get(f"http://127.0.0.1:{router_p}/health", timeout=1.0)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("router did not become ready")

        empty = httpx.get(f"http://127.0.0.1:{router_p}/providers", timeout=5.0)
        assert empty.status_code == 200
        assert empty.json() == []

        spawn(
            "intelliroute.mock_provider.main:app",
            mock_p,
            {
                "MOCK_NAME": "mock-fast",
                "MOCK_MODEL": "fast-1",
                "MOCK_LATENCY_MS": "1",
                "MOCK_FAILURE_RATE": "0",
                "MOCK_COST_PER_1K": "0.002",
                "INTELLIROUTE_MOCK_PUBLIC_PORT": str(mock_p),
            },
        )

        saw = False
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if time.monotonic() - _t0 > _MAX_WALL_S:
                pytest.fail("wall time exceeded waiting for mock registration")
            try:
                httpx.get(f"http://127.0.0.1:{mock_p}/health", timeout=1.0)
            except Exception:
                time.sleep(0.15)
                continue
            pr = httpx.get(f"http://127.0.0.1:{router_p}/providers", timeout=2.0)
            if pr.status_code == 200 and len(pr.json()) >= 1:
                saw = True
                break
            time.sleep(0.15)
        assert saw, "mock-fast did not become routable"

        mock_proc = procs.pop()
        mock_proc.terminate()
        try:
            mock_proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            mock_proc.kill()

        time.sleep(4.5)
        final = httpx.get(f"http://127.0.0.1:{router_p}/providers", timeout=5.0)
        assert final.status_code == 200
        assert final.json() == []

        reg = httpx.get(f"http://127.0.0.1:{router_p}/providers/registry", timeout=5.0).json()
        assert reg["providers_active"] == 0
        assert "mock-fast" in reg["stale_names"]
    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
