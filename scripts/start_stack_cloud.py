"""Cloud launcher for the IntelliRoute backend stack.

Use this when deploying the backend to Render, Railway, Fly.io, or a VM-style
host. It starts only the Python backend services, not the static frontend.

Public behavior:
- Gateway binds to 0.0.0.0:$PORT so the cloud platform can route traffic to it.

Internal behavior:
- Router, rate limiter replicas, cost tracker, health monitor, and mock providers
  bind to 127.0.0.1 on their normal internal ports.
- Other services discover one another through localhost URLs.

Recommended Render start command:
    PYTHONPATH=. python scripts/start_stack_cloud.py
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from intelliroute.common.env import load_dotenv_if_present

ROOT = Path(__file__).resolve().parents[1]
load_dotenv_if_present()

INTERNAL_HOST = os.environ.get("INTELLIROUTE_INTERNAL_HOST", "127.0.0.1")
PUBLIC_HOST = os.environ.get("INTELLIROUTE_PUBLIC_HOST", "0.0.0.0")
PUBLIC_PORT = int(os.environ.get("PORT", os.environ.get("INTELLIROUTE_GATEWAY_PORT", "8000")))

RATE_LIMITER_PEERS = (
    "rl-0=http://127.0.0.1:8002,"
    "rl-1=http://127.0.0.1:8012,"
    "rl-2=http://127.0.0.1:8022"
)

# Services are intentionally ordered so dependencies are ready before the gateway
# starts accepting public traffic.
SERVICES: list[dict[str, Any]] = [
    {
        "name": "mock-fast",
        "module": "intelliroute.mock_provider.main:app",
        "port_env": "INTELLIROUTE_MOCK_FAST_PORT",
        "default_port": 9001,
        "host": INTERNAL_HOST,
        "env": {
            "MOCK_NAME": "mock-fast",
            "MOCK_MODEL": "fast-1",
            "MOCK_LATENCY_MS": "30",
            "MOCK_LATENCY_JITTER_MS": "10",
            "MOCK_FAILURE_RATE": "0.0",
            "MOCK_COST_PER_1K": "0.002",
        },
        "mock": True,
    },
    {
        "name": "mock-smart",
        "module": "intelliroute.mock_provider.main:app",
        "port_env": "INTELLIROUTE_MOCK_SMART_PORT",
        "default_port": 9002,
        "host": INTERNAL_HOST,
        "env": {
            "MOCK_NAME": "mock-smart",
            "MOCK_MODEL": "smart-1",
            "MOCK_LATENCY_MS": "120",
            "MOCK_LATENCY_JITTER_MS": "20",
            "MOCK_FAILURE_RATE": "0.0",
            "MOCK_COST_PER_1K": "0.02",
        },
        "mock": True,
    },
    {
        "name": "mock-cheap",
        "module": "intelliroute.mock_provider.main:app",
        "port_env": "INTELLIROUTE_MOCK_CHEAP_PORT",
        "default_port": 9003,
        "host": INTERNAL_HOST,
        "env": {
            "MOCK_NAME": "mock-cheap",
            "MOCK_MODEL": "cheap-1",
            "MOCK_LATENCY_MS": "80",
            "MOCK_LATENCY_JITTER_MS": "15",
            "MOCK_FAILURE_RATE": "0.0",
            "MOCK_COST_PER_1K": "0.0003",
        },
        "mock": True,
    },
    {
        "name": "rate-limiter-0",
        "module": "intelliroute.rate_limiter.main:app",
        "port_env": "INTELLIROUTE_RATE_LIMITER_PORT",
        "default_port": 8002,
        "host": INTERNAL_HOST,
        "env": {
            "RATE_LIMITER_REPLICA_ID": "rl-0",
            "RATE_LIMITER_PEERS": RATE_LIMITER_PEERS,
        },
    },
    {
        "name": "rate-limiter-1",
        "module": "intelliroute.rate_limiter.main:app",
        "port_env": "INTELLIROUTE_RATE_LIMITER_PORT_1",
        "default_port": 8012,
        "host": INTERNAL_HOST,
        "env": {
            "RATE_LIMITER_REPLICA_ID": "rl-1",
            "RATE_LIMITER_PEERS": RATE_LIMITER_PEERS,
        },
    },
    {
        "name": "rate-limiter-2",
        "module": "intelliroute.rate_limiter.main:app",
        "port_env": "INTELLIROUTE_RATE_LIMITER_PORT_2",
        "default_port": 8022,
        "host": INTERNAL_HOST,
        "env": {
            "RATE_LIMITER_REPLICA_ID": "rl-2",
            "RATE_LIMITER_PEERS": RATE_LIMITER_PEERS,
        },
    },
    {
        "name": "cost-tracker",
        "module": "intelliroute.cost_tracker.main:app",
        "port_env": "INTELLIROUTE_COST_TRACKER_PORT",
        "default_port": 8003,
        "host": INTERNAL_HOST,
        "env": {},
    },
    {
        "name": "health-monitor",
        "module": "intelliroute.health_monitor.main:app",
        "port_env": "INTELLIROUTE_HEALTH_MONITOR_PORT",
        "default_port": 8004,
        "host": INTERNAL_HOST,
        "env": {},
    },
    {
        "name": "router",
        "module": "intelliroute.router.main:app",
        "port_env": "INTELLIROUTE_ROUTER_PORT",
        "default_port": 8001,
        "host": INTERNAL_HOST,
        "env": {},
    },
    {
        "name": "gateway",
        "module": "intelliroute.gateway.main:app",
        "port_env": "INTELLIROUTE_GATEWAY_PORT",
        "default_port": PUBLIC_PORT,
        "host": PUBLIC_HOST,
        "env": {
            "INTELLIROUTE_GATEWAY_PORT": str(PUBLIC_PORT),
        },
    },
]


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _provider_mode() -> str:
    mode = os.environ.get("INTELLIROUTE_PROVIDER_MODE", "auto").strip().lower()
    if mode not in {"auto", "mock_only", "external_only", "hybrid"}:
        return "auto"
    if _truthy(os.environ.get("INTELLIROUTE_USE_MOCKS")):
        return "mock_only"
    return mode


def _should_start_mock_services() -> bool:
    mode = _provider_mode()
    if mode in {"mock_only", "hybrid"}:
        return True
    if mode == "external_only":
        return False

    # auto mode: if real API keys exist, let the router use external providers;
    # otherwise start mock providers so the demo still works.
    has_external_keys = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY"))
    return not has_external_keys


def _wait_ready(proc: subprocess.Popen[bytes], url: str, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not checked yet"

    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{url} exited early with code {proc.returncode}")
        try:
            response = httpx.get(url, timeout=1.5)
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001 - startup diagnostics should keep retrying
            last_error = str(exc)
        time.sleep(0.25)

    raise RuntimeError(f"{url} did not become ready within {timeout:.1f}s: {last_error}")


def _service_port(service: dict[str, Any]) -> int:
    if service["name"] == "gateway":
        return PUBLIC_PORT
    return int(os.environ.get(service["port_env"], service["default_port"]))


def _health_host(service: dict[str, Any]) -> str:
    # 0.0.0.0 is a bind address, not a reliable client destination.
    return INTERNAL_HOST if service["name"] == "gateway" else service["host"]


def _spawn_service(
    procs: list[subprocess.Popen[bytes]],
    base_env: dict[str, str],
    service: dict[str, Any],
) -> None:
    port = _service_port(service)
    host = service["host"]

    env = base_env.copy()
    env.update(service.get("env", {}))

    if service.get("mock"):
        env["INTELLIROUTE_MOCK_PUBLIC_PORT"] = str(port)

    command = [
        sys.executable,
        "-m",
        "uvicorn",
        service["module"],
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        os.environ.get("INTELLIROUTE_UVICORN_LOG_LEVEL", "info"),
    ]

    print(f"starting {service['name']} ({service['module']}) on {host}:{port}", flush=True)
    proc = subprocess.Popen(command, env=env, cwd=str(ROOT))
    procs.append(proc)

    health_url = f"http://{_health_host(service)}:{port}/health"
    _wait_ready(proc, health_url)


def _terminate(procs: list[subprocess.Popen[bytes]]) -> None:
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass

    for proc in procs:
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


def main() -> int:
    procs: list[subprocess.Popen[bytes]] = []

    base_env = os.environ.copy()
    base_env["PYTHONPATH"] = str(ROOT)

    # Keep app-level service discovery on localhost even though the gateway binds
    # publicly to 0.0.0.0. The current config.py derives service URLs from
    # INTELLIROUTE_HOST, so this must stay local inside the container/VM.
    base_env["INTELLIROUTE_HOST"] = INTERNAL_HOST
    base_env.setdefault("INTELLIROUTE_ROUTER_PORT", "8001")
    base_env.setdefault("INTELLIROUTE_RATE_LIMITER_PORT", "8002")
    base_env.setdefault("INTELLIROUTE_COST_TRACKER_PORT", "8003")
    base_env.setdefault("INTELLIROUTE_HEALTH_MONITOR_PORT", "8004")
    base_env["INTELLIROUTE_GATEWAY_PORT"] = str(PUBLIC_PORT)

    base_env.setdefault("INTELLIROUTE_MOCK_REGISTRATION", "hybrid")
    base_env.setdefault("INTELLIROUTE_PROVIDER_LEASE_TTL_SECONDS", "30")
    base_env.setdefault("INTELLIROUTE_PROVIDER_HEARTBEAT_INTERVAL_SECONDS", "8")

    start_mocks = _should_start_mock_services()
    services = [svc for svc in SERVICES if start_mocks or not svc.get("mock")]

    def _handle_shutdown(signum: int, _frame: object) -> None:
        print(f"received signal {signum}; shutting down...", flush=True)
        _terminate(procs)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    try:
        print("starting IntelliRoute cloud backend stack", flush=True)
        print(f"provider mode: {_provider_mode()} | mock subprocesses: {'on' if start_mocks else 'off'}", flush=True)
        print(f"public gateway bind: {PUBLIC_HOST}:{PUBLIC_PORT}", flush=True)
        print(f"internal service host: {INTERNAL_HOST}", flush=True)

        for service in services:
            _spawn_service(procs, base_env, service)

        print("\nIntelliRoute cloud backend stack is running.", flush=True)
        print(f"Gateway health: http://127.0.0.1:{PUBLIC_PORT}/health", flush=True)
        print("Use your cloud provider URL as the frontend API_BASE.", flush=True)

        while True:
            for proc in procs:
                if proc.poll() is not None:
                    print(f"child process exited unexpectedly with code {proc.returncode}", flush=True)
                    _terminate(procs)
                    return proc.returncode or 1
            time.sleep(1.0)

    except Exception as exc:  # noqa: BLE001 - launcher should surface startup failures clearly
        print(f"startup failed: {exc}", file=sys.stderr, flush=True)
        _terminate(procs)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
