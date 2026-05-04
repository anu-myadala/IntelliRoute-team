"""Mock LLM provider service.

Each instance of this service simulates a single upstream LLM backend
(e.g. OpenAI, Anthropic, a local Llama). The behaviour is parameterised
at startup by environment variables so we can spin up three flavours
with different latency/cost/failure profiles for the demo and tests.

Environment variables
---------------------
MOCK_NAME          -- provider identifier, e.g. "mock-fast"
MOCK_MODEL         -- model identifier, e.g. "fast-1"
MOCK_LATENCY_MS    -- average simulated latency, milliseconds
MOCK_LATENCY_JITTER_MS -- random +/- jitter added to latency
MOCK_FAILURE_RATE  -- probability in [0, 1] that a request returns 503
MOCK_COST_PER_1K   -- cost per 1K tokens (used in logs only)

Router self-registration (hybrid / dynamic)
-------------------------------------------
INTELLIROUTE_MOCK_REGISTRATION   -- legacy | hybrid | dynamic (default: hybrid)
INTELLIROUTE_ROUTER_URL          -- optional; else http://INTELLIROUTE_HOST:INTELLIROUTE_ROUTER_PORT
INTELLIROUTE_MOCK_PUBLIC_PORT    -- this process's HTTP port (required for hybrid/dynamic)
INTELLIROUTE_PROVIDER_LEASE_TTL_SECONDS -- default 30
INTELLIROUTE_PROVIDER_HEARTBEAT_INTERVAL_SECONDS -- default 8
"""
from __future__ import annotations

import asyncio
import os
import random
import time
import uuid

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ..common.logging import get_logger, log_event
from ..common.mock_provider_catalog import mock_provider_info
from ..common.models import ProviderHeartbeatRequest, ProviderRegisterRequest


class MockChatRequest(BaseModel):
    messages: list[dict]
    max_tokens: int = 256


class MockChatResponse(BaseModel):
    id: str
    provider: str
    model: str
    content: str
    prompt_tokens: int
    completion_tokens: int


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


NAME = os.environ.get("MOCK_NAME", "mock-provider")
MODEL = os.environ.get("MOCK_MODEL", "mock-1")
LATENCY_MS = _env_float("MOCK_LATENCY_MS", 100.0)
JITTER_MS = _env_float("MOCK_LATENCY_JITTER_MS", 20.0)
FAILURE_RATE = _env_float("MOCK_FAILURE_RATE", 0.0)
COST_PER_1K = _env_float("MOCK_COST_PER_1K", 0.001)

log = get_logger(NAME)
app = FastAPI(title=f"IntelliRoute MockProvider {NAME}")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Fault-injection state dictionary.
# All flags default to False (normal operation). Each can be set independently
# via the /admin/* endpoints so tests can compose failure scenarios precisely.
#
# force_fail       — return HTTP 503 Service Unavailable on every request
# force_timeout    — sleep indefinitely, simulating a hung upstream connection
# force_rate_limit — return HTTP 429 Too Many Requests with a Retry-After header
# force_malformed  — return a syntactically broken JSON body (triggers parse errors)
# ---------------------------------------------------------------------------
_state = {
    "force_fail": False,
    "force_timeout": False,
    "force_rate_limit": False,
    "force_malformed": False,
}
_registration_task: asyncio.Task[None] | None = None


def _mock_registration_mode() -> str:
    raw = os.environ.get("INTELLIROUTE_MOCK_REGISTRATION", "hybrid").strip().lower()
    if raw in ("legacy", "hybrid", "dynamic"):
        return raw
    return "hybrid"


def _router_base_url() -> str:
    explicit = (os.environ.get("INTELLIROUTE_ROUTER_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    host = os.environ.get("INTELLIROUTE_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("INTELLIROUTE_ROUTER_PORT", "8001"))
    except (TypeError, ValueError):
        port = 8001
    return f"http://{host}:{port}"


async def _registration_heartbeat_loop() -> None:
    mode = _mock_registration_mode()
    if mode == "legacy":
        log_event(log, "mock_router_registration_disabled", mode="legacy", provider=NAME)
        return

    try:
        public_port = int(os.environ.get("INTELLIROUTE_MOCK_PUBLIC_PORT", "0"))
    except (TypeError, ValueError):
        public_port = 0
    if public_port <= 0:
        log_event(
            log,
            "mock_router_registration_skipped",
            provider=NAME,
            reason="INTELLIROUTE_MOCK_PUBLIC_PORT_not_set",
        )
        return

    host = os.environ.get("INTELLIROUTE_HOST", "127.0.0.1")
    try:
        pinfo = mock_provider_info(NAME, host, public_port)
    except ValueError as exc:
        log_event(log, "mock_router_registration_skipped", provider=NAME, reason=str(exc))
        return

    lease_ttl = _env_float("INTELLIROUTE_PROVIDER_LEASE_TTL_SECONDS", 30.0)
    heartbeat_interval = _env_float("INTELLIROUTE_PROVIDER_HEARTBEAT_INTERVAL_SECONDS", 8.0)
    router = _router_base_url()
    provider_id = NAME.strip()
    register_payload = ProviderRegisterRequest(
        provider_id=provider_id,
        provider=pinfo,
        lease_ttl_seconds=lease_ttl,
        registration_source="mock_self",
        model_tier="mock",
    ).model_dump()
    heartbeat_payload = ProviderHeartbeatRequest(provider_id=provider_id).model_dump()

    backoff_s = 1.0
    registered = False
    next_heartbeat_at = 0.0

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                if not registered:
                    resp = await client.post(
                        f"{router}/providers/register",
                        json=register_payload,
                    )
                    if resp.status_code == 200:
                        log_event(
                            log,
                            "mock_router_register_ok",
                            provider=NAME,
                            provider_id=provider_id,
                            router=router,
                        )
                        registered = True
                        backoff_s = 1.0
                        next_heartbeat_at = time.monotonic() + heartbeat_interval
                    else:
                        log_event(
                            log,
                            "mock_router_register_retry",
                            provider=NAME,
                            status_code=resp.status_code,
                            detail=resp.text[:300],
                            backoff_s=backoff_s,
                        )
                        await asyncio.sleep(backoff_s)
                        backoff_s = min(backoff_s * 1.25, 10.0)
                    continue

                now = time.monotonic()
                sleep_for = next_heartbeat_at - now
                if sleep_for > 0:
                    await asyncio.sleep(min(sleep_for, 1.0))
                    continue

                hb = await client.post(
                    f"{router}/providers/heartbeat",
                    json=heartbeat_payload,
                )
                if hb.status_code == 200:
                    log_event(
                        log,
                        "mock_router_heartbeat_ok",
                        provider=NAME,
                        provider_id=provider_id,
                    )
                    next_heartbeat_at = time.monotonic() + heartbeat_interval
                    backoff_s = 1.0
                else:
                    log_event(
                        log,
                        "mock_router_heartbeat_failed",
                        provider=NAME,
                        status_code=hb.status_code,
                        detail=hb.text[:300],
                    )
                    registered = False
                    backoff_s = 1.0

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    log,
                    "mock_router_registration_error",
                    provider=NAME,
                    error=str(exc),
                    registered=registered,
                    backoff_s=backoff_s,
                )
                registered = False
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 1.25, 10.0)


@app.on_event("startup")
async def _startup_self_register() -> None:
    global _registration_task
    if _mock_registration_mode() != "legacy":
        _registration_task = asyncio.create_task(_registration_heartbeat_loop())


@app.on_event("shutdown")
async def _shutdown_self_register() -> None:
    global _registration_task
    if _registration_task is not None:
        _registration_task.cancel()
        try:
            await _registration_task
        except asyncio.CancelledError:
            pass
        _registration_task = None


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy" if not _state["force_fail"] else "degraded", "provider": NAME}


class ForceFailBody(BaseModel):
    fail: bool = True


class ForceTimeoutBody(BaseModel):
    timeout: bool = True


class ForceRateLimitBody(BaseModel):
    rate_limit: bool = True


class ForceMalformedBody(BaseModel):
    malformed: bool = True


@app.post("/admin/force_fail")
async def force_fail(body: ForceFailBody = ForceFailBody()) -> dict:
    """Test hook: flip the provider into hard-failure (503) mode."""
    _state["force_fail"] = body.fail
    return {"force_fail": body.fail}


@app.post("/admin/force_timeout")
async def admin_force_timeout(body: ForceTimeoutBody = ForceTimeoutBody()) -> dict:
    """Test hook: make every /v1/chat request hang indefinitely.

    The mock sleeps for 300 s so any real client timeout fires first, giving
    the same observable behaviour as a truly hung upstream without actually
    blocking the event loop for that long (the asyncio sleep is cancellable).
    """
    _state["force_timeout"] = body.timeout
    return {"force_timeout": body.timeout}


@app.post("/admin/force_rate_limit")
async def admin_force_rate_limit(body: ForceRateLimitBody = ForceRateLimitBody()) -> dict:
    """Test hook: return HTTP 429 with a Retry-After header on every request.

    Exercises the router's SLA-aware retry back-off logic and the rate-limiter's
    provider-level circuit breaker without needing a real rate-limited API.
    """
    _state["force_rate_limit"] = body.rate_limit
    return {"force_rate_limit": body.rate_limit}


@app.post("/admin/force_malformed")
async def admin_force_malformed(body: ForceMalformedBody = ForceMalformedBody()) -> dict:
    """Test hook: return a syntactically broken JSON body on every request.

    The response has HTTP 200 so the router does not treat it as a transport
    failure, but the provider-client will fail to parse it and the feedback
    collector's JSON-parse signal will flag the response as anomalous.
    """
    _state["force_malformed"] = body.malformed
    return {"force_malformed": body.malformed}


@app.post("/admin/reset")
async def admin_reset() -> dict:
    """Clear all fault-injection flags, returning the provider to normal operation.

    Useful at the start of a new test scenario to guarantee a clean state
    without restarting the process.
    """
    _state["force_fail"] = False
    _state["force_timeout"] = False
    _state["force_rate_limit"] = False
    _state["force_malformed"] = False
    log_event(log, "admin_reset", provider=NAME)
    return {"reset": True, "provider": NAME, "all_faults_cleared": True}


@app.get("/admin/state")
async def admin_state() -> dict:
    """Return the current fault-injection state for observability.

    Lets test harnesses verify which faults are active without needing to
    track the state themselves across multiple admin calls.
    """
    return {"provider": NAME, "fault_state": dict(_state)}


@app.post("/v1/chat", response_model=MockChatResponse)
async def chat(req: MockChatRequest) -> MockChatResponse | Response:
    # Simulate latency.
    latency = max(0.0, (LATENCY_MS + random.uniform(-JITTER_MS, JITTER_MS)) / 1000.0)
    await asyncio.sleep(latency)

    # ------------------------------------------------------------------
    # Fault injection — checked in priority order before normal logic.
    # ------------------------------------------------------------------

    # 503: hard failure (provider down / overloaded)
    if _state["force_fail"] or random.random() < FAILURE_RATE:
        log_event(log, "simulated_failure", provider=NAME)
        raise HTTPException(status_code=503, detail=f"{NAME} temporarily unavailable")

    # 504: timeout simulation — sleep far longer than any real client timeout.
    # The router's httpx client will raise a ReadTimeout before this wakes up,
    # which exercises the same code path as a real hung upstream.
    if _state["force_timeout"]:
        log_event(log, "simulated_timeout", provider=NAME)
        await asyncio.sleep(300.0)
        raise HTTPException(status_code=504, detail=f"{NAME} gateway timeout (simulated)")

    # 429: rate-limit simulation — includes a Retry-After header so the router's
    # SLA-aware retry logic can respect the back-off window.
    if _state["force_rate_limit"]:
        log_event(log, "simulated_rate_limit", provider=NAME)
        raise HTTPException(
            status_code=429,
            detail=f"{NAME} rate limited (simulated) — retry after 5s",
            headers={"Retry-After": "5"},
        )

    # 200 with broken body: deliberately truncated JSON to exercise the
    # provider-client's parse-error handling and hallucination signal path.
    if _state["force_malformed"]:
        log_event(log, "simulated_malformed_response", provider=NAME)
        return Response(
            content='{"provider": "' + NAME + '", "broken_json":',
            media_type="application/json",
            status_code=200,
        )

    prompt_text = " ".join(m.get("content", "") for m in req.messages)
    prompt_tokens = max(1, len(prompt_text) // 4)
    completion_tokens = min(req.max_tokens, max(1, prompt_tokens // 2))
    content = (
        f"[{NAME}:{MODEL}] synthesized reply to: "
        f"{prompt_text[:80]!r} ({completion_tokens} tokens)"
    )

    resp = MockChatResponse(
        id=str(uuid.uuid4()),
        provider=NAME,
        model=MODEL,
        content=content,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    log_event(
        log,
        "completion_ok",
        provider=NAME,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=round(latency * 1000, 1),
    )
    return resp
