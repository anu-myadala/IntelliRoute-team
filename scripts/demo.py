"""Demo client.

Hits the running gateway with a few representative requests, prints the
routing decisions, then prints the tenant cost summary at the end.

Usage:
    # Terminal 1: launch the stack (or use docker compose / start.sh)
    python scripts/start_stack.py
    # Terminal 2:
    python scripts/demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

GATEWAY = os.environ.get("INTELLIROUTE_DEMO_URL", "http://127.0.0.1:8000")
ROUTER = os.environ.get("INTELLIROUTE_ROUTER_URL", "http://127.0.0.1:8001")
RL_REPLICAS = [
    "http://127.0.0.1:8002",  # rl-0
    "http://127.0.0.1:8012",  # rl-1
    "http://127.0.0.1:8022",  # rl-2
]
KEY = os.environ.get("INTELLIROUTE_DEMO_KEY", "demo-key-123")


def _post(path: str, body: dict, url: str = GATEWAY) -> dict:
    r = httpx.post(
        f"{url}{path}", json=body,
        headers={"X-API-Key": KEY, "Content-Type": "application/json"},
        timeout=10.0,
    )
    r.raise_for_status()
    return r.json()


def _get(path: str, url: str = GATEWAY) -> dict:
    r = httpx.get(
        f"{url}{path}",
        headers={"X-API-Key": KEY, "Content-Type": "application/json"},
        timeout=10.0,
    )
    r.raise_for_status()
    return r.json()


def demo_feedback() -> None:
    """Demo: Feedback metrics collection."""
    print("\n=== Feedback Metrics Demo ===")
    try:
        # Send some requests
        for i in range(3):
            body = {
                "tenant_id": "test-tenant",
                "messages": [{"role": "user", "content": f"Hello {i}"}],
                "max_tokens": 50,
            }
            try:
                _post("/v1/complete", body)
            except Exception:
                pass

        time.sleep(0.5)

        # Check feedback metrics
        metrics = _get("/feedback", url=ROUTER)
        print("Feedback metrics:")
        print(json.dumps(metrics, indent=2))
    except Exception as exc:
        print(f"Feedback demo failed: {exc}")


def demo_backpressure() -> None:
    """Demo: Queue and backpressure."""
    print("\n=== Queue & Backpressure Demo ===")
    try:
        # Check queue stats
        stats = _get("/queue/stats", url=ROUTER)
        print("Queue stats:")
        print(json.dumps(stats, indent=2))
    except Exception as exc:
        print(f"Backpressure demo failed: {exc}")


def demo_election() -> None:
    """Demo: Leader election across rate limiter replicas."""
    print("\n=== Leader Election Demo ===")
    try:
        for i, replica_url in enumerate(RL_REPLICAS):
            try:
                status = httpx.get(f"{replica_url}/election/status", timeout=2.0).json()
                print(
                    f"Replica {i} ({replica_url}): state={status.get('state')}, "
                    f"is_leader={status.get('is_leader')}, leader={status.get('current_leader')}"
                )
            except Exception as exc:
                print(f"Replica {i} unreachable: {exc}")
    except Exception as exc:
        print(f"Election demo failed: {exc}")


def main() -> int:
    prompts = [
        ("interactive small-talk", "Hi, what's the capital of France?"),
        ("reasoning", (
            "Explain step by step why the CAP theorem implies that a "
            "distributed system cannot simultaneously offer consistency, "
            "availability, and partition tolerance, and analyze how real "
            "systems trade off these properties."
        )),
        ("batch summarisation", "Summarize the following document into bullet points: ..."),
        ("code", "I got an exception in my Python loop, here is the traceback ..."),
    ]
    for label, text in prompts:
        body = {
            "tenant_id": "ignored",
            "messages": [{"role": "user", "content": text}],
            "max_tokens": 80,
        }
        try:
            resp = _post("/v1/complete", body)
        except Exception as exc:
            print(f"[{label}] FAILED: {exc}")
            return 1
        print(f"[{label}] -> {resp['provider']} ({resp['model']}) "
              f"latency={resp['latency_ms']}ms cost=${resp['estimated_cost_usd']:.6f}"
              f" fallback={resp['fallback_used']}")

    time.sleep(0.3)  # let async cost events flush
    summary = httpx.get(
        f"{GATEWAY}/v1/cost/summary", headers={"X-API-Key": KEY}, timeout=5.0
    ).json()
    print("\nCost summary:")
    print(json.dumps(summary, indent=2))

    # Run the new demos
    demo_feedback()
    demo_backpressure()
    demo_election()

    return 0


if __name__ == "__main__":
    sys.exit(main())
