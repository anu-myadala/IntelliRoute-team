"""Launch the entire IntelliRoute stack as subprocesses."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

from intelliroute.common.config import settings
from intelliroute.common.env import load_dotenv_if_present
from intelliroute.common.provider_mode import should_skip_mock_uvicorn_subprocesses

ROOT = Path(__file__).resolve().parents[1]
load_dotenv_if_present()

SERVICES = [
    {
        "module": "intelliroute.mock_provider.main:app",
        "port_env": "INTELLIROUTE_MOCK_FAST_PORT",
        "default_port": 9001,
        "env": {
            "MOCK_NAME": "mock-fast", "MOCK_MODEL": "fast-1",
            "MOCK_LATENCY_MS": "30", "MOCK_LATENCY_JITTER_MS": "10",
            "MOCK_FAILURE_RATE": "0.0", "MOCK_COST_PER_1K": "0.002",
        },
    },
    {
        "module": "intelliroute.mock_provider.main:app",
        "port_env": "INTELLIROUTE_MOCK_SMART_PORT",
        "default_port": 9002,
        "env": {
            "MOCK_NAME": "mock-smart", "MOCK_MODEL": "smart-1",
            "MOCK_LATENCY_MS": "120", "MOCK_LATENCY_JITTER_MS": "20",
            "MOCK_FAILURE_RATE": "0.0", "MOCK_COST_PER_1K": "0.02",
        },
    },
    {
        "module": "intelliroute.mock_provider.main:app",
        "port_env": "INTELLIROUTE_MOCK_CHEAP_PORT",
        "default_port": 9003,
        "env": {
            "MOCK_NAME": "mock-cheap", "MOCK_MODEL": "cheap-1",
            "MOCK_LATENCY_MS": "80", "MOCK_LATENCY_JITTER_MS": "15",
            "MOCK_FAILURE_RATE": "0.0", "MOCK_COST_PER_1K": "0.0003",
        },
    },
    {
        "module": "intelliroute.rate_limiter.main:app",
        "port_env": "INTELLIROUTE_RATE_LIMITER_PORT",
        "default_port": 8002,
        "env": {
            "RATE_LIMITER_REPLICA_ID": "rl-0",
            "RATE_LIMITER_PEERS": "rl-0=http://127.0.0.1:8002,rl-1=http://127.0.0.1:8012,rl-2=http://127.0.0.1:8022",
        },
    },
    {
        "module": "intelliroute.rate_limiter.main:app",
        "port_env": "INTELLIROUTE_RATE_LIMITER_PORT_1",
        "default_port": 8012,
        "env": {
            "RATE_LIMITER_REPLICA_ID": "rl-1",
            "RATE_LIMITER_PEERS": "rl-0=http://127.0.0.1:8002,rl-1=http://127.0.0.1:8012,rl-2=http://127.0.0.1:8022",
        },
    },
    {
        "module": "intelliroute.rate_limiter.main:app",
        "port_env": "INTELLIROUTE_RATE_LIMITER_PORT_2",
        "default_port": 8022,
        "env": {
            "RATE_LIMITER_REPLICA_ID": "rl-2",
            "RATE_LIMITER_PEERS": "rl-0=http://127.0.0.1:8002,rl-1=http://127.0.0.1:8012,rl-2=http://127.0.0.1:8022",
        },
    },
    {"module": "intelliroute.cost_tracker.main:app", "port_env": "INTELLIROUTE_COST_TRACKER_PORT", "default_port": 8003, "env": {}},
    {"module": "intelliroute.health_monitor.main:app", "port_env": "INTELLIROUTE_HEALTH_MONITOR_PORT", "default_port": 8004, "env": {}},
    {"module": "intelliroute.router.main:app", "port_env": "INTELLIROUTE_ROUTER_PORT", "default_port": 8001, "env": {}},
    {"module": "intelliroute.gateway.main:app", "port_env": "INTELLIROUTE_GATEWAY_PORT", "default_port": 8000, "env": {}},
]


def _wait_ready(proc: subprocess.Popen, url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"service for {url} exited early with code {proc.returncode}")
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"service at {url} did not become ready within {timeout:.1f}s: {last_error}")


def _spawn_service(
    procs: list[subprocess.Popen],
    base_env: dict[str, str],
    svc: dict,
    host: str,
) -> None:
    port = int(os.environ.get(svc["port_env"], svc["default_port"]))
    env = base_env.copy()
    env.update(svc["env"])
    if "mock_provider" in svc["module"]:
        env["INTELLIROUTE_MOCK_PUBLIC_PORT"] = str(port)
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        svc["module"],
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    print(f"starting {svc['module']} on :{port}")
    proc = subprocess.Popen(cmd, env=env, cwd=str(ROOT))
    procs.append(proc)
    _wait_ready(proc, f"http://{host}:{port}/health")


def main() -> int:
    procs: list[subprocess.Popen] = []
    base_env = os.environ.copy()
    base_env["PYTHONPATH"] = str(ROOT)
    host = os.environ.get("INTELLIROUTE_HOST", "127.0.0.1")
    router_port = os.environ.get("INTELLIROUTE_ROUTER_PORT", "8001")
    base_env.setdefault(
        "INTELLIROUTE_ROUTER_URL",
        os.environ.get("INTELLIROUTE_ROUTER_URL", f"http://{host}:{router_port}"),
    )
    base_env.setdefault(
        "INTELLIROUTE_MOCK_REGISTRATION",
        os.environ.get("INTELLIROUTE_MOCK_REGISTRATION", "hybrid"),
    )
    base_env.setdefault(
        "INTELLIROUTE_PROVIDER_LEASE_TTL_SECONDS",
        os.environ.get("INTELLIROUTE_PROVIDER_LEASE_TTL_SECONDS", "30"),
    )
    base_env.setdefault(
        "INTELLIROUTE_PROVIDER_HEARTBEAT_INTERVAL_SECONDS",
        os.environ.get("INTELLIROUTE_PROVIDER_HEARTBEAT_INTERVAL_SECONDS", "8"),
    )
    skip_mocks = should_skip_mock_uvicorn_subprocesses(settings)
    services = SERVICES[3:] if skip_mocks else SERVICES
    frontend_port = int(os.environ.get("INTELLIROUTE_FRONTEND_PORT", "3000"))
    frontend_dir = str(ROOT / "frontend")
    try:
        for svc in services:
            _spawn_service(procs, base_env, svc, host)
        if os.path.isdir(frontend_dir):
            frontend_cmd = [
                sys.executable,
                "-m",
                "http.server",
                str(frontend_port),
                "--directory",
                frontend_dir,
                "--bind",
                host,
            ]
            print(f"starting frontend on :{frontend_port}")
            fe_proc = subprocess.Popen(frontend_cmd, env=base_env, cwd=str(ROOT))
            procs.append(fe_proc)
            time.sleep(0.6)
            if fe_proc.poll() is not None:
                print(
                    f"\nERROR: Static frontend exited immediately (exit {fe_proc.returncode}). "
                    f"Port {frontend_port} is probably already in use (Errno 48).\n"
                    f"  Fix: stop the other stack (Ctrl-C in that terminal) or free the port, e.g.:\n"
                    f"    lsof -nP -iTCP:{frontend_port} -sTCP:LISTEN\n"
                    f"  Or use another port, then open that URL in the browser:\n"
                    f"    INTELLIROUTE_FRONTEND_PORT=3005 PYTHONPATH=. python3 scripts/start_stack.py\n"
                )
                return 1
        print("\nIntelliRoute stack running.")
        _prov_label = (
            "external subprocesses skipped; router uses live APIs only"
            if skip_mocks
            else "mock demo provider processes + router bootstrap (see INTELLIROUTE_PROVIDER_MODE)"
        )
        print(f"  Providers:  {_prov_label}")
        print(f"  Gateway:    http://{host}:8000")
        print(f"  Frontend:   http://{host}:{frontend_port}")
        print("  Press Ctrl-C to stop.\n")
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nshutting down...")
    finally:
        for p in procs:
            try:
                p.send_signal(signal.SIGINT)
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
