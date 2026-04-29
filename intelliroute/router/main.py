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
import random
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
    BrownoutStatus,
    CompletionRequest,
    CompletionResponse,
    CostEvent,
    Intent,
    PolicyEvaluationResult,
    ProviderHealth,
    ProviderHeartbeatRequest,
    ProviderInfo,
    ProviderRegisterRequest,
    RateLimitCheck,
)
from .feedback import CompletionOutcome, FeedbackCollector, compute_hallucination_signal
from .brownout import BrownoutManager
from .intent import classify
from .policy import RoutingPolicy
from .policy_engine import PolicyEvaluator
from .provider_clients import ProviderCallError, call_provider
from .queue import INTENT_PRIORITY, Priority, RequestQueue
from .registry import ProviderRegistry
from .weight_tuner import WeightTuner

log = get_logger("router")

registry = ProviderRegistry()
feedback = FeedbackCollector()
policy = RoutingPolicy(feedback=feedback)
policy_evaluator = PolicyEvaluator()
request_queue = RequestQueue()
brownout_manager = BrownoutManager()
_tenant_brownout: dict[str, BrownoutManager] = {}
weight_tuner = WeightTuner(policy)

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
_discovery_task: Optional[asyncio.Task] = None


@app.on_event("startup")
async def _startup() -> None:
    global _http, _worker_tasks
    _http = httpx.AsyncClient(timeout=5.0)
    # Auto-register the three mock providers if the env vars are set.
    _bootstrap_registry()
    # Start queue worker tasks
    for i in range(_WORKER_COUNT):
        task = asyncio.create_task(_queue_worker(i))
        _worker_tasks.append(task)
    global _discovery_task
    _discovery_task = asyncio.create_task(_discovery_sweep_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _worker_tasks, _discovery_task
    if _discovery_task is not None:
        _discovery_task.cancel()
        await asyncio.gather(_discovery_task, return_exceptions=True)
        _discovery_task = None
    if _http is not None:
        await _http.aclose()
    # Cancel worker tasks
    for task in _worker_tasks:
        task.cancel()
    # Wait for cancellation
    await asyncio.gather(*_worker_tasks, return_exceptions=True)


def _mock_bootstrap() -> list[ProviderInfo]:
    return [
        ProviderInfo(
            name="mock-fast",
            url=f"http://{settings.host}:{settings.mock_fast_port}",
            model="fast-1",
            provider_type="mock",
            capability={"interactive": 0.85, "reasoning": 0.45, "batch": 0.5, "code": 0.6},
            cost_per_1k_tokens=0.002,
            typical_latency_ms=120,
            capability_tier=2,
        ),
        ProviderInfo(
            name="mock-smart",
            url=f"http://{settings.host}:{settings.mock_smart_port}",
            model="smart-1",
            provider_type="mock",
            capability={"interactive": 0.7, "reasoning": 0.95, "batch": 0.8, "code": 0.9},
            cost_per_1k_tokens=0.02,
            typical_latency_ms=900,
            capability_tier=3,
        ),
        ProviderInfo(
            name="mock-cheap",
            url=f"http://{settings.host}:{settings.mock_cheap_port}",
            model="cheap-1",
            provider_type="mock",
            capability={"interactive": 0.55, "reasoning": 0.4, "batch": 0.75, "code": 0.45},
            cost_per_1k_tokens=0.0003,
            typical_latency_ms=600,
            capability_tier=1,
        ),
    ]


def _external_bootstrap() -> list[ProviderInfo]:
    providers: list[ProviderInfo] = []
    if settings.groq_api_key:
        providers.append(
            ProviderInfo(
                name="groq",
                url="https://api.groq.com/openai/v1",
                model=settings.groq_model,
                provider_type="groq",
                capability={"interactive": 0.93, "reasoning": 0.76, "batch": 0.88, "code": 0.74},
                cost_per_1k_tokens=0.0007,
                typical_latency_ms=500,
                capability_tier=2,
            )
        )
    if settings.gemini_api_key:
        providers.append(
            ProviderInfo(
                name="gemini",
                url="https://generativelanguage.googleapis.com/v1beta",
                model=settings.gemini_model,
                provider_type="gemini",
                capability={"interactive": 0.72, "reasoning": 0.97, "batch": 0.68, "code": 0.91},
                cost_per_1k_tokens=0.0035,
                typical_latency_ms=1200,
                capability_tier=3,
            )
        )
    return providers


def _bootstrap_registry() -> None:
    if os.environ.get("INTELLIROUTE_SKIP_BOOTSTRAP") == "1":
        return
    external = _external_bootstrap()
    if external and not settings.use_mock_providers:
        registry.bulk_register(external)
        log_event(log, "bootstrap_registry", mode="external", providers=[p.name for p in external])
        return
    mocks = _mock_bootstrap()
    registry.bulk_register(mocks)
    log_event(log, "bootstrap_registry", mode="mock", providers=[p.name for p in mocks])


@app.get("/health")
async def health() -> dict:
    now = time.time()
    active = len(registry.all_active(now))
    total = len(registry.all_entries())
    return {
        "status": "healthy",
        "providers": active,
        "providers_active": active,
        "providers_total": total,
    }


@app.post("/providers")
async def register_provider(p: ProviderInfo) -> dict:
    registry.register_bootstrap(p)
    log_event(
        log,
        "provider_registered",
        mode="bootstrap",
        name=p.name,
        provider_id=p.name,
    )
    return {"registered": p.name}


@app.post("/providers/register")
async def register_provider_dynamic(req: ProviderRegisterRequest) -> dict:
    registry.register_api(req)
    pid = (req.provider_id or req.provider.name).strip()
    log_event(
        log,
        "provider_registered",
        mode="api",
        name=req.provider.name,
        provider_id=pid,
        lease_ttl_seconds=req.lease_ttl_seconds,
        source=req.registration_source,
    )
    return {"registered": req.provider.name, "provider_id": pid}


@app.post("/providers/heartbeat")
async def provider_heartbeat(req: ProviderHeartbeatRequest) -> dict:
    ok = registry.heartbeat(req.provider_id.strip())
    if not ok:
        raise HTTPException(
            status_code=404,
            detail="unknown provider_id or provider does not use heartbeats",
        )
    log_event(log, "provider_heartbeat", provider_id=req.provider_id.strip())
    return {"ok": True, "provider_id": req.provider_id.strip()}


@app.get("/providers/registry")
async def providers_registry() -> dict:
    """Debug / observability: all rows including stale (TTL expired) providers.

    Declared before ``/providers/{{name}}`` so ``registry`` is not captured as a name.
    """
    now = time.time()
    stale = registry.stale_names(now)
    return {
        "providers": registry.discovery_snapshot(now),
        "providers_active": len(registry.all_active(now)),
        "providers_total": len(registry.all_entries()),
        "stale_names": stale,
    }


@app.delete("/providers/{name}")
async def deregister_provider(name: str) -> dict:
    registry.deregister(name)
    log_event(log, "provider_deregistered", name=name)
    return {"deregistered": name}


@app.get("/providers")
async def list_providers() -> list[ProviderInfo]:
    """Routable providers only (same set used before ranking)."""
    return registry.all_active(time.time())


async def _discovery_sweep_loop() -> None:
    """Periodic observability for providers whose heartbeat lease has lapsed."""
    interval = float(os.environ.get("INTELLIROUTE_DISCOVERY_SWEEP_S", "15"))
    while True:
        try:
            await asyncio.sleep(interval)
            now = time.time()
            stale = registry.stale_names(now)
            if stale:
                log_event(log, "provider_heartbeat_expired", providers=stale)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log_event(log, "discovery_sweep_error", error=str(exc))


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


def _sla_backoff_ms(provider: ProviderInfo, intent: Intent, attempt: int) -> float:
    """Jittered exponential backoff bounded by the provider's declared SLA.

    The backoff floor doubles on each retry but is capped at one tenth of the
    provider's per-intent SLA so a slow provider with a 10s SLA cannot stall
    the fallback loop for a request whose budget is much tighter.
    """
    base_ms = 20.0 * (2 ** max(0, attempt - 1))
    sla_ms = provider.sla_p95_latency_ms.get(intent.value, 0.0)
    cap_ms = max(50.0, sla_ms / 10.0) if sla_ms > 0 else 200.0
    bounded = min(base_ms, cap_ms)
    return bounded * (1.0 + random.random() * 0.25)


async def _publish_cost(event: CostEvent) -> None:
    assert _http is not None
    try:
        await _http.post(
            f"{settings.cost_tracker_url}/events", json=event.model_dump()
        )
    except Exception:
        pass


async def _fetch_tenant_budget_context(tenant_id: str) -> tuple[float | None, float]:
    """Return (budget_usd or None, spent_usd). Fail-open on errors."""
    assert _http is not None
    spent = 0.0
    try:
        r = await _http.get(f"{settings.cost_tracker_url}/summary/{tenant_id}")
        if r.status_code == 200:
            spent = float(r.json().get("total_cost_usd", 0.0))
    except Exception:
        pass
    budget: float | None = None
    try:
        r = await _http.get(f"{settings.cost_tracker_url}/budget/{tenant_id}")
        if r.status_code == 200:
            raw = r.json().get("budget_usd")
            if raw is not None:
                budget = float(raw)
    except Exception:
        pass
    return budget, spent


def _tenant_key(tenant_id: str) -> str:
    return tenant_id.strip() or "__anonymous__"


def _tenant_brownout_manager(tenant_id: str) -> BrownoutManager:
    key = _tenant_key(tenant_id)
    mgr = _tenant_brownout.get(key)
    if mgr is None:
        mgr = BrownoutManager(config=brownout_manager.config)
        _tenant_brownout[key] = mgr
    return mgr


def _record_brownout_result(
    tenant_id: str, *, latency_ms: float, success: bool, timed_out: bool = False
) -> None:
    brownout_manager.record_request_result(
        latency_ms=latency_ms, success=success, timed_out=timed_out
    )
    _tenant_brownout_manager(tenant_id).record_request_result(
        latency_ms=latency_ms, success=success, timed_out=timed_out
    )


async def _prepare_routing(
    req: CompletionRequest,
) -> tuple[
    Intent,
    dict[str, ProviderHealth],
    list[ProviderInfo],
    PolicyEvaluationResult | None,
    BrownoutStatus,
    float | None,
    float,
]:
    intent = classify(req)
    health = await _fetch_health_snapshot()
    now = time.time()
    all_providers = registry.all_active(now)
    queue_stats = request_queue.stats()
    global_bs, transitioned = brownout_manager.evaluate(queue_stats.total_depth)
    tenant_mgr = _tenant_brownout_manager(req.tenant_id)
    tenant_bs, tenant_transitioned = tenant_mgr.evaluate(queue_stats.total_depth)
    if transitioned:
        log_event(
            log,
            "brownout_transition",
            scope="global",
            is_degraded=global_bs.is_degraded,
            reason=global_bs.reason,
            queue_depth=global_bs.queue_depth,
            p95_latency_ms=global_bs.p95_latency_ms,
            error_rate=global_bs.error_rate,
            timeout_rate=global_bs.timeout_rate,
        )
    if tenant_transitioned:
        log_event(
            log,
            "brownout_transition",
            scope=f"tenant:{_tenant_key(req.tenant_id)}",
            is_degraded=tenant_bs.is_degraded,
            reason=tenant_bs.reason,
            queue_depth=tenant_bs.queue_depth,
            p95_latency_ms=tenant_bs.p95_latency_ms,
            error_rate=tenant_bs.error_rate,
            timeout_rate=tenant_bs.timeout_rate,
        )
    effective_bs = tenant_bs if tenant_bs.is_degraded else global_bs
    bs_model = BrownoutStatus(
        is_degraded=effective_bs.is_degraded,
        reason=effective_bs.reason,
        entered_at_unix=effective_bs.entered_at_unix,
        queue_depth=effective_bs.queue_depth,
        p95_latency_ms=effective_bs.p95_latency_ms,
        error_rate=effective_bs.error_rate,
        timeout_rate=effective_bs.timeout_rate,
    )
    tenant_budget, tenant_spent = await _fetch_tenant_budget_context(req.tenant_id)
    candidates, policy_result = policy_evaluator.evaluate(
        all_providers,
        intent,
        req,
        tenant_budget_usd=tenant_budget,
        tenant_spent_usd=tenant_spent,
        brownout_status=bs_model,
        brownout_max_latency_ms=brownout_manager.config.low_latency_max_ms,
        brownout_block_premium=brownout_manager.config.block_premium_for_medium_and_low,
        brownout_prefer_low_latency=brownout_manager.config.prefer_low_latency_for_medium_and_low,
    )
    if not policy_evaluator._config.enabled:
        return intent, health, list(all_providers), None, bs_model, tenant_budget, tenant_spent
    return intent, health, candidates, policy_result, bs_model, tenant_budget, tenant_spent


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
                if queued.future is not None and not queued.future.done():
                    queued.future.set_exception(
                        TimeoutError(
                            f"Request {queued.request_id} timed out after {elapsed_ms:.0f}ms"
                        )
                    )
                continue

            # Execute the request
            try:
                response = await _execute_completion(queued.request_id, queued.request)
                if queued.future is not None and not queued.future.done():
                    queued.future.set_result(response)
            except Exception as exc:
                if queued.future is not None and not queued.future.done():
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
    started = time.monotonic()
    intent, health, candidates, pe, bs, tenant_budget, tenant_spent = await _prepare_routing(req)
    effective_req = req
    if (
        bs.is_degraded
        and brownout_manager.config.reduce_max_tokens_for_medium_and_low
        and intent in {Intent.REASONING, Intent.BATCH}
        and req.max_tokens > brownout_manager.config.degraded_max_tokens
    ):
        effective_req = req.model_copy(
            update={"max_tokens": brownout_manager.config.degraded_max_tokens}
        )
        log_event(
            log,
            "brownout_max_tokens_clamped",
            request_id=request_id,
            old_max_tokens=req.max_tokens,
            new_max_tokens=effective_req.max_tokens,
            intent=intent.value,
        )

    ranked = policy.rank(
        candidates,
        health=health,
        intent=intent,
        latency_budget_ms=effective_req.latency_budget_ms,
        confidence_hint=effective_req.confidence_hint,
    )
    if not ranked:
        _record_brownout_result(
            effective_req.tenant_id,
            latency_ms=(time.monotonic() - started) * 1000,
            success=False,
        )
        raise HTTPException(status_code=503, detail="no providers registered")

    log_event(
        log,
        "route_decided",
        request_id=request_id,
        intent=intent.value,
        primary=ranked[0].provider.name,
        policy_matched_rules=list(pe.matched_rules) if pe else [],
        policy_blocked=list(pe.blocked_providers) if pe else [],
        policy_complexity=pe.complexity_score if pe else None,
        brownout_active=bs.is_degraded,
        brownout_reason=bs.reason,
    )

    fallback_used = False
    last_error: Optional[str] = None

    pending: list = list(ranked)
    i = 0
    attempts = 0
    while pending:
        if attempts > 0 and pending:
            backoff_ms = _sla_backoff_ms(pending[0].provider, intent, attempts)
            await asyncio.sleep(backoff_ms / 1000.0)
        # Budget-aware pre-call gate: if the next-up provider would push the
        # tenant past its budget, demote to the cheapest still-pending option.
        # This implements the spec's "fallback if marginal gain < cost delta"
        # rule without requiring the policy_engine to know about live spend.
        if tenant_budget is not None and len(pending) >= 1:
            head = pending[0]
            projected = (effective_req.max_tokens / 1000.0) * head.provider.cost_per_1k_tokens
            if tenant_spent + projected > tenant_budget:
                cheaper = min(pending, key=lambda s: s.provider.cost_per_1k_tokens)
                if cheaper is not head:
                    log_event(
                        log,
                        "budget_gate_demoted",
                        from_provider=head.provider.name,
                        to_provider=cheaper.provider.name,
                        projected_cost=round(projected, 6),
                        tenant_budget=tenant_budget,
                        tenant_spent=tenant_spent,
                    )
                    pending.remove(cheaper)
                    pending.insert(0, cheaper)
                    fallback_used = True
        scored = pending.pop(0)
        info = scored.provider
        allowed, retry_ms = await _check_rate_limit(effective_req.tenant_id, info.name)
        if not allowed:
            log_event(
                log, "rate_limited", provider=info.name, retry_after_ms=retry_ms
            )
            last_error = f"rate_limited:{info.name}"
            fallback_used = True
            pending = policy.reorder_after_failure(pending, info.capability_tier)
            i += 1
            attempts += 1
            continue

        ok, latency_ms, data = await _call_provider(info, effective_req)
        asyncio.create_task(_report_health(info.name, ok, latency_ms))
        weight_tuner.observe(intent, scored.sub_scores, ok)

        # Record feedback outcome
        prompt_chars = len(effective_req.messages[0].content) if effective_req.messages else 1
        response_text = data.get("content", "") if data else ""
        hallucination_signal = (
            compute_hallucination_signal(response_text, prompt_char_count=prompt_chars)
            if ok
            else 0.0
        )
        outcome = CompletionOutcome(
            provider=info.name,
            latency_ms=latency_ms,
            success=ok,
            prompt_tokens=int(data.get("prompt_tokens", 0)) if data else 0,
            completion_tokens=int(data.get("completion_tokens", 0)) if data else 0,
            prompt_char_count=prompt_chars,
            response_char_count=len(response_text),
            hallucination_signal=hallucination_signal,
        )
        feedback.record(outcome)

        if not ok:
            log_event(
                log, "provider_failed", provider=info.name, latency_ms=latency_ms
            )
            last_error = f"provider_failed:{info.name}"
            fallback_used = True
            pending = policy.reorder_after_failure(pending, info.capability_tier)
            i += 1
            attempts += 1
            continue

        prompt_tokens = int(data.get("prompt_tokens", 0))
        completion_tokens = int(data.get("completion_tokens", 0))
        total_tokens = prompt_tokens + completion_tokens
        estimated_cost = (total_tokens / 1000.0) * info.cost_per_1k_tokens

        asyncio.create_task(
            _publish_cost(
                CostEvent(
                    request_id=request_id,
                    tenant_id=effective_req.tenant_id,
                    provider=info.name,
                    model=info.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost_usd=estimated_cost,
                    unix_ts=time.time(),
                )
            )
        )

        _record_brownout_result(
            effective_req.tenant_id,
            latency_ms=(time.monotonic() - started) * 1000,
            success=True,
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
            policy_evaluation=pe,
            brownout_status=bs,
        )

    _record_brownout_result(
        effective_req.tenant_id,
        latency_ms=(time.monotonic() - started) * 1000,
        success=False,
    )
    raise HTTPException(status_code=503, detail=f"all providers failed: {last_error}")


async def _call_provider(
    info: ProviderInfo, req: CompletionRequest
) -> tuple[bool, float, dict | None]:
    assert _http is not None
    start = time.monotonic()
    try:
        ok, data = await call_provider(_http, info, req)
        return ok, (time.monotonic() - start) * 1000, data
    except ProviderCallError as exc:
        log_event(log, "provider_call_config_error", provider=info.name, error=str(exc))
        return False, (time.monotonic() - start) * 1000, None
    except Exception as exc:
        log_event(log, "provider_call_error", provider=info.name, error=str(exc))
        return False, (time.monotonic() - start) * 1000, None


class RouteDecision(BaseModel):
    intent: str
    ranked: list[str]
    scores: dict[str, float]
    policy_evaluation: Optional[PolicyEvaluationResult] = None
    brownout_status: Optional[BrownoutStatus] = None


@app.get("/weights")
async def get_weights() -> dict:
    """Current per-intent multi-objective weights and tuner samples."""
    out: dict[str, dict] = {}
    for intent, weights in policy._weights.items():
        snap = weight_tuner.snapshot(intent)
        out[intent.value] = {
            "latency": round(weights.latency, 4),
            "cost": round(weights.cost, 4),
            "capability": round(weights.capability, 4),
            "success": round(weights.success, 4),
            "tuner_samples": snap.samples,
            "tuner_net_credit": {k: round(v, 4) for k, v in snap.net_credit.items()},
        }
    return out


@app.post("/weights/rebalance/{intent}")
async def rebalance_weights(intent: str) -> dict:
    """Manually trigger a tuner rebalance for a given intent."""
    try:
        intent_enum = Intent(intent)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown intent: {intent}")
    applied = weight_tuner.maybe_rebalance(intent_enum)
    return {"intent": intent, "rebalanced": applied}


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


@app.get("/brownout", response_model=BrownoutStatus)
async def brownout_status() -> BrownoutStatus:
    bs = brownout_manager.snapshot()
    return BrownoutStatus(
        is_degraded=bs.is_degraded,
        reason=bs.reason,
        entered_at_unix=bs.entered_at_unix,
        queue_depth=bs.queue_depth,
        p95_latency_ms=bs.p95_latency_ms,
        error_rate=bs.error_rate,
        timeout_rate=bs.timeout_rate,
    )


@app.get("/brownout/metrics")
async def brownout_metrics() -> dict:
    return {
        "global": brownout_manager.metrics(),
        "tenants": {
            key: mgr.metrics()
            for key, mgr in _tenant_brownout.items()
        },
    }


@app.get("/brownout/{tenant_id}", response_model=BrownoutStatus)
async def brownout_status_for_tenant(tenant_id: str) -> BrownoutStatus:
    bs = _tenant_brownout_manager(tenant_id).snapshot()
    return BrownoutStatus(
        is_degraded=bs.is_degraded,
        reason=bs.reason,
        entered_at_unix=bs.entered_at_unix,
        queue_depth=bs.queue_depth,
        p95_latency_ms=bs.p95_latency_ms,
        error_rate=bs.error_rate,
        timeout_rate=bs.timeout_rate,
    )


@app.post("/decide", response_model=RouteDecision)
async def decide(req: CompletionRequest) -> RouteDecision:
    """Introspection endpoint: return the routing decision without executing it."""
    intent, health, candidates, pe, bs, _budget, _spent = await _prepare_routing(req)
    ranked = policy.rank(
        candidates,
        health=health,
        intent=intent,
        latency_budget_ms=req.latency_budget_ms,
        confidence_hint=req.confidence_hint,
    )
    return RouteDecision(
        intent=intent.value,
        ranked=[s.provider.name for s in ranked],
        scores={s.provider.name: round(s.score, 4) for s in ranked},
        policy_evaluation=pe,
        brownout_status=BrownoutStatus(
            is_degraded=bs.is_degraded,
            reason=bs.reason,
            entered_at_unix=bs.entered_at_unix,
            queue_depth=bs.queue_depth,
            p95_latency_ms=bs.p95_latency_ms,
            error_rate=bs.error_rate,
            timeout_rate=bs.timeout_rate,
        ),
    )


@app.post("/complete", response_model=CompletionResponse)
async def complete(req: CompletionRequest) -> CompletionResponse:
    request_id = str(uuid.uuid4())
    intent = classify(req)

    # Determine priority
    priority = INTENT_PRIORITY.get(intent, Priority.MEDIUM)
    bs = brownout_manager.snapshot()

    # Brownout override for lowest-priority traffic.
    if (
        bs.is_degraded
        and priority == Priority.LOW
        and brownout_manager.config.drop_low_priority_when_degraded
    ):
        log_event(
            log,
            "brownout_low_priority_drop",
            request_id=request_id,
            reason=bs.reason,
            intent=intent.value,
        )
        raise HTTPException(status_code=503, detail="brownout active: low-priority traffic deferred")

    if (
        bs.is_degraded
        and priority == Priority.LOW
        and brownout_manager.config.delay_low_priority_ms > 0
    ):
        await asyncio.sleep(brownout_manager.config.delay_low_priority_ms / 1000.0)

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

    if queued_req is None:
        raise HTTPException(status_code=500, detail="queue enqueue failed")

    if queued_req.future is None:
        queued_req.future = asyncio.get_running_loop().create_future()

    # Wait for the queued request to be processed with timeout
    try:
        timeout_s = request_queue._config.timeout_ms / 1000.0
        response = await asyncio.wait_for(queued_req.future, timeout=timeout_s)
        return response
    except asyncio.TimeoutError:
        request_queue.record_timeout(request_id)
        _record_brownout_result(
            req.tenant_id, latency_ms=timeout_s * 1000.0, success=False, timed_out=True
        )
        raise HTTPException(
            status_code=504, detail=f"request processing timed out after {timeout_s}s"
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
