"""Router service.

Responsibilities
----------------
1. Classify incoming requests into an :class:`Intent`.
2. Consult the provider registry (service discovery) and the health
   monitor (circuit breaker state) to build a ranked list of candidates.
3. Check the rate limiter before attempting a provider.
4. Call the chosen provider over HTTP; on failure, fall back to the next
   provider in the ranked list ("adaptive degradation").
5. Report success/failure to the health monitor and publish a cost
   event to the cost tracker (fire-and-forget).
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..common.config import settings
from ..common.logging import get_logger, log_event
from ..common.models import (
    CompletionRequest,
    CompletionResponse,
    CostEvent,
    Intent,
    ProviderHealth,
    ProviderInfo,
    RateLimitCheck,
)
from .feedback import CompletionOutcome, FeedbackCollector
from .intent import classify
from .policy import RoutingPolicy
from .queue import INTENT_PRIORITY, Priority, RequestQueue
from .registry import ProviderRegistry

log = get_logger("router")

registry = ProviderRegistry()
feedback = FeedbackCollector()
policy = RoutingPolicy(feedback=feedback)
request_queue = RequestQueue()

app = FastAPI(title="IntelliRoute Router")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_http: Optional[httpx.AsyncClient] = None
_WORKER_COUNT = 4
_worker_tasks: list[asyncio.Task] = []


@app.on_event("startup")
async def _startup() -> None:
    global _http, _worker_tasks
    _http = httpx.AsyncClient(timeout=5.0)
    # Auto-register the three mock providers if the env vars are set.
    _bootstrap_mock_registry()
    # Start queue worker tasks
    for i in range(_WORKER_COUNT):
        task = asyncio.create_task(_queue_worker(i))
        _worker_tasks.append(task)


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _worker_tasks
    if _http is not None:
        await _http.aclose()
    # Cancel worker tasks
    for task in _worker_tasks:
        task.cancel()
    # Wait for cancellation
    await asyncio.gather(*_worker_tasks, return_exceptions=True)


def _bootstrap_mock_registry() -> None:
    """Register the three canonical mock providers from env config."""
    bootstrap = [
        ProviderInfo(
            name="mock-fast",
            url=f"http://{settings.host}:{settings.mock_fast_port}",
            model="fast-1",
            capability={"interactive": 0.85, "reasoning": 0.45, "batch": 0.5, "code": 0.6},
            cost_per_1k_tokens=0.002,
            typical_latency_ms=120,
        ),
        ProviderInfo(
            name="mock-smart",
            url=f"http://{settings.host}:{settings.mock_smart_port}",
            model="smart-1",
            capability={"interactive": 0.7, "reasoning": 0.95, "batch": 0.8, "code": 0.9},
            cost_per_1k_tokens=0.02,
            typical_latency_ms=900,
        ),
        ProviderInfo(
            name="mock-cheap",
            url=f"http://{settings.host}:{settings.mock_cheap_port}",
            model="cheap-1",
            capability={"interactive": 0.55, "reasoning": 0.4, "batch": 0.75, "code": 0.45},
            cost_per_1k_tokens=0.0003,
            typical_latency_ms=600,
        ),
    ]
    if os.environ.get("INTELLIROUTE_SKIP_BOOTSTRAP") != "1":
        registry.bulk_register(bootstrap)


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "providers": len(registry.all())}


@app.post("/providers")
async def register_provider(p: ProviderInfo) -> dict:
    registry.register(p)
    return {"registered": p.name}


@app.delete("/providers/{name}")
async def deregister_provider(name: str) -> dict:
    registry.deregister(name)
    return {"deregistered": name}


@app.get("/providers")
async def list_providers() -> list[ProviderInfo]:
    return registry.all()


async def _fetch_health_snapshot() -> dict[str, ProviderHealth]:
    assert _http is not None
    try:
        r = await _http.get(f"{settings.health_monitor_url}/snapshot")
        if r.status_code != 200:
            return {}
        data = r.json()
        return {name: ProviderHealth(**h) for name, h in data.items()}
    except Exception as exc:
        log_event(log, "health_snapshot_failed", error=str(exc))
        return {}


async def _check_rate_limit(tenant: str, provider: str) -> tuple[bool, int]:
    assert _http is not None
    try:
        r = await _http.post(
            f"{settings.rate_limiter_url}/check",
            json=RateLimitCheck(tenant_id=tenant, provider=provider).model_dump(),
        )
        if r.status_code != 200:
            return True, 0  # fail-open if the limiter is down
        data = r.json()
        return bool(data.get("allowed", True)), int(data.get("retry_after_ms", 0))
    except Exception:
        return True, 0


async def _report_health(provider: str, success: bool, latency_ms: float) -> None:
    assert _http is not None
    try:
        await _http.post(
            f"{settings.health_monitor_url}/report/{provider}",
            params={"success": str(success).lower(), "latency_ms": latency_ms},
        )
    except Exception:
        pass


async def _publish_cost(event: CostEvent) -> None:
    assert _http is not None
    try:
        await _http.post(
            f"{settings.cost_tracker_url}/events", json=event.model_dump()
        )
    except Exception:
        pass


async def _queue_worker(worker_id: int) -> None:
    """Worker coroutine that processes queued requests."""
    while True:
        try:
            queued = await request_queue.dequeue()
            if queued is None:
                await asyncio.sleep(0.1)
                continue

            # Check timeout
            elapsed_ms = (time.monotonic() - queued.enqueued_at) * 1000
            if elapsed_ms > request_queue._config.timeout_ms:
                request_queue.record_timeout(queued.request_id)
                queued.future.set_exception(
                    TimeoutError(
                        f"Request {queued.request_id} timed out after {elapsed_ms:.0f}ms"
                    )
                )
                continue

            # Execute the request
            try:
                response = await _execute_completion(queued.request_id, queued.request)
                queued.future.set_result(response)
            except Exception as exc:
                queued.future.set_exception(exc)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log_event(log, "queue_worker_error", worker_id=worker_id, error=str(exc))
            await asyncio.sleep(0.1)


async def _execute_completion(
    request_id: str, req: CompletionRequest
) -> CompletionResponse:
    """Core completion logic: ranking, provider tries, and feedback recording."""
    intent = classify(req)
    health = await _fetch_health_snapshot()
    ranked = policy.rank(
        registry.all(), health=health, intent=intent, latency_budget_ms=req.latency_budget_ms
    )
    if not ranked:
        raise HTTPException(status_code=503, detail="no providers registered")

    log_event(
        log,
        "route_decided",
        request_id=request_id,
        intent=intent.value,
        primary=ranked[0].provider.name,
    )

    fallback_used = False
    last_error: Optional[str] = None

    for i, scored in enumerate(ranked):
        info = scored.provider
        allowed, retry_ms = await _check_rate_limit(req.tenant_id, info.name)
        if not allowed:
            log_event(
                log, "rate_limited", provider=info.name, retry_after_ms=retry_ms
            )
            last_error = f"rate_limited:{info.name}"
            fallback_used = True
            continue

        ok, latency_ms, data = await _call_provider(info, req)
        asyncio.create_task(_report_health(info.name, ok, latency_ms))

        # Record feedback outcome
        outcome = CompletionOutcome(
            provider=info.name,
            latency_ms=latency_ms,
            success=ok,
            prompt_tokens=int(data.get("prompt_tokens", 0)) if data else 0,
            completion_tokens=int(data.get("completion_tokens", 0)) if data else 0,
            prompt_char_count=len(req.messages[0].content) if req.messages else 1,
            response_char_count=len(data.get("content", "")) if data else 0,
        )
        feedback.record(outcome)

        if not ok:
            log_event(
                log, "provider_failed", provider=info.name, latency_ms=latency_ms
            )
            last_error = f"provider_failed:{info.name}"
            fallback_used = True
            continue

        prompt_tokens = int(data.get("prompt_tokens", 0))
        completion_tokens = int(data.get("completion_tokens", 0))
        total_tokens = prompt_tokens + completion_tokens
        estimated_cost = (total_tokens / 1000.0) * info.cost_per_1k_tokens

        asyncio.create_task(
            _publish_cost(
                CostEvent(
                    request_id=request_id,
                    tenant_id=req.tenant_id,
                    provider=info.name,
                    model=info.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost_usd=estimated_cost,
                    unix_ts=time.time(),
                )
            )
        )

        return CompletionResponse(
            request_id=request_id,
            provider=info.name,
            model=info.model,
            content=data.get("content", ""),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=round(latency_ms, 2),
            estimated_cost_usd=round(estimated_cost, 6),
            fallback_used=fallback_used or i > 0,
            degraded=i > 0,
        )

    raise HTTPException(status_code=503, detail=f"all providers failed: {last_error}")


async def _call_provider(
    info: ProviderInfo, req: CompletionRequest
) -> tuple[bool, float, dict | None]:
    assert _http is not None
    start = time.monotonic()
    try:
        payload = {
            "messages": [m.model_dump() for m in req.messages],
            "max_tokens": req.max_tokens,
        }
        r = await _http.post(f"{info.url}/v1/chat", json=payload, timeout=5.0)
        elapsed_ms = (time.monotonic() - start) * 1000
        if r.status_code != 200:
            return False, elapsed_ms, None
        return True, elapsed_ms, r.json()
    except Exception:
        return False, (time.monotonic() - start) * 1000, None


class RouteDecision(BaseModel):
    intent: str
    ranked: list[str]
    scores: dict[str, float]


@app.get("/feedback")
async def get_feedback() -> dict:
    """Return all feedback metrics collected so far."""
    metrics = feedback.all_metrics()
    return {
        name: {
            "latency_ema": round(m.latency_ema, 2),
            "success_rate_ema": round(m.success_rate_ema, 4),
            "token_efficiency_ema": round(m.token_efficiency_ema, 4),
            "anomaly_score": round(m.anomaly_score, 4),
            "sample_count": m.sample_count,
        }
        for name, m in metrics.items()
    }


@app.get("/queue/stats")
async def queue_stats() -> dict:
    """Return current queue statistics."""
    stats = request_queue.stats()
    return {
        "total_depth": stats.total_depth,
        "by_priority": stats.by_priority,
        "shed_count": stats.shed_count,
        "timeout_count": stats.timeout_count,
    }


@app.post("/decide", response_model=RouteDecision)
async def decide(req: CompletionRequest) -> RouteDecision:
    """Introspection endpoint: return the routing decision without executing it."""
    intent = classify(req)
    health = await _fetch_health_snapshot()
    ranked = policy.rank(
        registry.all(), health=health, intent=intent, latency_budget_ms=req.latency_budget_ms
    )
    return RouteDecision(
        intent=intent.value,
        ranked=[s.provider.name for s in ranked],
        scores={s.provider.name: round(s.score, 4) for s in ranked},
    )


@app.post("/complete", response_model=CompletionResponse)
async def complete(req: CompletionRequest) -> CompletionResponse:
    request_id = str(uuid.uuid4())
    intent = classify(req)

    # Determine priority
    priority = INTENT_PRIORITY.get(intent, Priority.MEDIUM)

    # HIGH priority requests bypass the queue
    if priority == Priority.HIGH:
        return await _execute_completion(request_id, req)

    # MEDIUM/LOW priority requests go through the queue with timeout
    enqueued, queued_req, error_msg = request_queue.try_enqueue(
        request_id, req, priority
    )
    if not enqueued:
        log_event(log, "request_shed", request_id=request_id, reason=error_msg)
        raise HTTPException(status_code=503, detail=f"queue full: {error_msg}")

    # Wait for the queued request to be processed with timeout
    try:
        timeout_s = request_queue._config.timeout_ms / 1000.0
        response = await asyncio.wait_for(queued_req.future, timeout=timeout_s)
        return response
    except asyncio.TimeoutError:
        request_queue.record_timeout(request_id)
        raise HTTPException(
            status_code=504, detail=f"request processing timed out after {timeout_s}s"
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
