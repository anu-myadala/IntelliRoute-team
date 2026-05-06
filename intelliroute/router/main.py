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
import json
import os
import time
import uuid
from typing import Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..common.config import settings
from ..common.logging import get_logger, log_event
from ..common.mock_provider_catalog import list_mock_provider_infos_from_settings
from ..common.provider_mode import build_bootstrap_result
from ..common.models import (
    BrownoutStatus,
    ChatMessage,
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
from .policy import RoutingPolicy, ScoredProvider
from .policy_engine import PolicyEvaluator
from .provider_clients import ProviderCallError, call_provider
from .provider_daily_quota import (
    QUOTA_EXHAUSTED_DETAIL,
    apply_daily_quota_to_ranked,
    daily_quota_limit,
    daily_quota_tracker,
)
from .queue import INTENT_PRIORITY, Priority, RequestQueue
from .registry import ProviderRegistry
from .user_feedback_store import CompletionMeta, UserFeedbackStore, format_provider_by_intent_for_prompt
from .weight_tuner import WeightTuner

log = get_logger("router")

registry = ProviderRegistry()
feedback = FeedbackCollector()
policy = RoutingPolicy(feedback=feedback)
request_queue = RequestQueue()
policy_evaluator = PolicyEvaluator()
request_queue = RequestQueue()
brownout_manager = BrownoutManager()
_tenant_brownout: dict[str, BrownoutManager] = {}
weight_tuner = WeightTuner(policy)
user_feedback = UserFeedbackStore()

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
_routing_mode = os.environ.get("INTELLIROUTE_ROUTING_MODE", "intelliroute").strip().lower()
_rr_cursor = 0


def _get_routing_mode() -> str:
    mode = (_routing_mode or "intelliroute").strip().lower()
    if mode in {"intelliroute", "round_robin", "cheapest_first", "latency_first", "premium_first"}:
        return mode
    return "intelliroute"


def _set_routing_mode(mode: str) -> str:
    global _routing_mode
    candidate = (mode or "intelliroute").strip().lower()
    if candidate not in {"intelliroute", "round_robin", "cheapest_first", "latency_first", "premium_first"}:
        raise ValueError(f"unsupported routing mode: {mode}")
    _routing_mode = candidate
    return _routing_mode


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


def _mock_registration_mode() -> str:
    """legacy = bootstrap only; hybrid = bootstrap + mock self-register/heartbeat; dynamic = self-register only."""
    raw = os.environ.get("INTELLIROUTE_MOCK_REGISTRATION", "hybrid").strip().lower()
    if raw in ("legacy", "hybrid", "dynamic"):
        return raw
    return "hybrid"


def _mock_bootstrap() -> list[ProviderInfo]:
    return list_mock_provider_infos_from_settings(settings)


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
    mocks = _mock_bootstrap()
    mock_mode = _mock_registration_mode()
    result = build_bootstrap_result(settings, external, mocks, mock_mode)

    if result.external_only_no_keys:
        log_event(
            log,
            "bootstrap_registry",
            mode="external_only",
            error="no GEMINI_API_KEY or GROQ_API_KEY; no providers registered",
            providers=[],
        )
        return

    if result.providers:
        registry.bulk_register(result.providers)

    if result.skipped_mock_bootstrap:
        log_event(
            log,
            "bootstrap_registry",
            mode=result.log_mode,
            skipped_mock_bootstrap=True,
            note="mocks must self-register",
            providers=[p.name for p in result.providers],
        )
        return

    if result.providers:
        log_event(
            log,
            "bootstrap_registry",
            mode=result.log_mode,
            providers=[p.name for p in result.providers],
        )


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


def _provider_timeout_s(provider: ProviderInfo, intent: Intent) -> float:
    """Per-provider timeout policy derived from declared SLA and defaults."""
    sla_ms = provider.sla_p95_latency_ms.get(intent.value, 0.0)
    if sla_ms <= 0:
        return settings.provider_timeout_s
    # Allow modest headroom over declared p95 while staying bounded.
    derived = max(0.5, (sla_ms / 1000.0) * 1.5)
    return min(settings.provider_timeout_s, derived)


def _error_backoff_ms(
    provider: ProviderInfo,
    intent: Intent,
    local_attempt: int,
    error_kind: str | None,
    retry_after_ms: int = 0,
) -> float:
    base = _sla_backoff_ms(provider, intent, local_attempt)
    if error_kind == "rate_limited":
        return max(base, float(retry_after_ms or 0))
    if error_kind == "timeout":
        return base * 1.5
    if error_kind == "server_error":
        return base * 1.2
    if error_kind == "transport_error":
        return base * 1.1
    return base


async def _publish_cost(event: CostEvent) -> None:
    assert _http is not None
    try:
        await _http.post(
            f"{settings.cost_tracker_url}/events", json=event.model_dump()
        )
    except Exception:
        pass


async def _fetch_budget_context(req: CompletionRequest) -> dict:
    """Return tenant/team/workflow budget context. Fail-open on errors."""
    assert _http is not None
    tenant_spent = 0.0
    try:
        r = await _http.get(f"{settings.cost_tracker_url}/summary/{req.tenant_id}")
        if r.status_code == 200:
            tenant_spent = float(r.json().get("total_cost_usd", 0.0))
    except Exception:
        pass
    tenant_budget: float | None = None
    try:
        r = await _http.get(f"{settings.cost_tracker_url}/budget/{req.tenant_id}")
        if r.status_code == 200:
            raw = r.json().get("budget_usd")
            if raw is not None:
                tenant_budget = float(raw)
    except Exception:
        pass
    team_spent = 0.0
    team_budget = None
    team_premium_spend = 0.0
    team_premium_cap = None
    if req.team_id:
        try:
            r = await _http.get(f"{settings.cost_tracker_url}/summary/team/{req.team_id}")
            if r.status_code == 200:
                team_spent = float(r.json().get("total_cost_usd", 0.0))
        except Exception:
            pass
        try:
            r = await _http.get(f"{settings.cost_tracker_url}/budget/team/{req.team_id}")
            if r.status_code == 200:
                body = r.json()
                team_budget = body.get("budget_usd")
                if team_budget is not None:
                    team_budget = float(team_budget)
                team_premium_spend = float(body.get("premium_spend_usd", 0.0))
                cap = body.get("premium_cap_usd")
                if cap is not None:
                    team_premium_cap = float(cap)
        except Exception:
            pass
    workflow_spent = 0.0
    workflow_budget = None
    if req.workflow_id:
        try:
            r = await _http.get(f"{settings.cost_tracker_url}/summary/workflow/{req.workflow_id}")
            if r.status_code == 200:
                workflow_spent = float(r.json().get("total_cost_usd", 0.0))
        except Exception:
            pass
        try:
            r = await _http.get(f"{settings.cost_tracker_url}/budget/workflow/{req.workflow_id}")
            if r.status_code == 200:
                b = r.json().get("budget_usd")
                if b is not None:
                    workflow_budget = float(b)
        except Exception:
            pass
    return {
        "tenant_budget_usd": tenant_budget,
        "tenant_spent_usd": tenant_spent,
        "team_budget_usd": team_budget,
        "team_spent_usd": team_spent,
        "workflow_budget_usd": workflow_budget,
        "workflow_spent_usd": workflow_spent,
        "team_premium_cap_usd": team_premium_cap,
        "team_premium_spend_usd": team_premium_spend,
    }


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
    dict,
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
    budget_ctx = await _fetch_budget_context(req)
    candidates, policy_result = policy_evaluator.evaluate(
        all_providers,
        intent,
        req,
        tenant_budget_usd=budget_ctx["tenant_budget_usd"],
        tenant_spent_usd=budget_ctx["tenant_spent_usd"],
        team_id=req.team_id,
        workflow_id=req.workflow_id,
        team_budget_usd=budget_ctx["team_budget_usd"],
        team_spent_usd=budget_ctx["team_spent_usd"],
        workflow_budget_usd=budget_ctx["workflow_budget_usd"],
        workflow_spent_usd=budget_ctx["workflow_spent_usd"],
        team_premium_cap_usd=budget_ctx["team_premium_cap_usd"],
        team_premium_spend_usd=budget_ctx["team_premium_spend_usd"],
        brownout_status=bs_model,
        brownout_max_latency_ms=brownout_manager.config.low_latency_max_ms,
        brownout_block_premium=brownout_manager.config.block_premium_for_medium_and_low,
        brownout_prefer_low_latency=brownout_manager.config.prefer_low_latency_for_medium_and_low,
    )
    if not policy_evaluator._config.enabled:
        return intent, health, list(all_providers), None, bs_model, budget_ctx
    return intent, health, candidates, policy_result, bs_model, budget_ctx


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


def _last_user_message_text(messages: list[ChatMessage]) -> str:
    for m in reversed(messages):
        if m.role.lower() == "user" and (m.content or "").strip():
            return str(m.content).strip()
    return ""


def _truncate_preview_text(text: str, max_len: int) -> str:
    raw = " ".join((text or "").split())
    if max_len <= 0:
        return ""
    if len(raw) <= max_len:
        return raw
    return raw[: max_len - 1] + "…"


def _completion_feedback_previews(req: CompletionRequest, response_text: str) -> tuple[str, str]:
    pl = settings.feedback_prompt_preview_chars
    rl = settings.feedback_response_preview_chars
    return (
        _truncate_preview_text(_last_user_message_text(req.messages), pl),
        _truncate_preview_text(response_text or "", rl),
    )


async def _execute_completion(
    request_id: str, req: CompletionRequest
) -> CompletionResponse:
    """Core completion logic: ranking, provider tries, and feedback recording."""
    started = time.monotonic()
    intent, health, candidates, pe, bs, budget_ctx = await _prepare_routing(req)
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

    routing_mode = _get_routing_mode()
    ranked = _rank_candidates(routing_mode, candidates, health, intent, effective_req)
    if not ranked:
        _record_brownout_result(
            effective_req.tenant_id,
            latency_ms=(time.monotonic() - started) * 1000,
            success=False,
        )
        raise HTTPException(status_code=503, detail="no providers registered")

    _ranked_before_quota = ranked
    ranked = apply_daily_quota_to_ranked(ranked, settings)
    if not ranked:
        _record_brownout_result(
            effective_req.tenant_id,
            latency_ms=(time.monotonic() - started) * 1000,
            success=False,
        )
        log_event(
            log,
            "provider_daily_quota_all_exhausted",
            candidates=[s.provider.name for s in _ranked_before_quota],
        )
        raise HTTPException(status_code=429, detail=QUOTA_EXHAUSTED_DETAIL)

    log_event(
        log,
        "route_decided",
        request_id=request_id,
        intent=intent.value,
        routing_mode=routing_mode,
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
    _provider_attempts: dict[str, int] = {}
    while pending:
        # Budget-aware pre-call gate: if the next-up provider would push the
        # tenant past its budget, demote to the cheapest still-pending option.
        # This implements the spec's "fallback if marginal gain < cost delta"
        # rule without requiring the policy_engine to know about live spend.
        tenant_budget = budget_ctx.get("tenant_budget_usd")
        tenant_spent = budget_ctx.get("tenant_spent_usd", 0.0)
        team_budget = budget_ctx.get("team_budget_usd")
        team_spent = budget_ctx.get("team_spent_usd", 0.0)
        workflow_budget = budget_ctx.get("workflow_budget_usd")
        workflow_spent = budget_ctx.get("workflow_spent_usd", 0.0)
        if (tenant_budget is not None or team_budget is not None or workflow_budget is not None) and len(pending) >= 1:
            head = pending[0]
            projected = (effective_req.max_tokens / 1000.0) * head.provider.cost_per_1k_tokens
            tenant_exceed = tenant_budget is not None and (tenant_spent + projected > tenant_budget)
            team_exceed = team_budget is not None and (team_spent + projected > team_budget)
            workflow_exceed = workflow_budget is not None and (workflow_spent + projected > workflow_budget)
            if tenant_exceed or team_exceed or workflow_exceed:
                cheaper = min(pending, key=lambda s: s.provider.cost_per_1k_tokens)
                if cheaper is not head:
                    scope = "tenant" if tenant_exceed else ("team" if team_exceed else "workflow")
                    log_event(
                        log,
                        "budget_gate_demoted",
                        scope=scope,
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
        local_attempt = _provider_attempts.get(info.name, 0) + 1
        _provider_attempts[info.name] = local_attempt

        lim_q = daily_quota_limit(info.name, settings)
        if lim_q is not None and daily_quota_tracker.usage(info.name) >= lim_q:
            log_event(
                log,
                "provider_daily_quota_skip",
                provider=info.name,
                used=daily_quota_tracker.usage(info.name),
                limit=lim_q,
                reason="daily_quota_reached_mid_route",
            )
            last_error = f"daily_quota_exhausted:{info.name}"
            fallback_used = True
            pending = policy.reorder_after_failure(pending, info.capability_tier)
            attempts += 1
            continue

        # Per-provider retry budget. Keep at least one attempt per provider.
        if local_attempt > max(1, int(info.max_retries)):
            log_event(
                log,
                "provider_retry_budget_exhausted",
                provider=info.name,
                attempts=local_attempt - 1,
                max_retries=info.max_retries,
            )
            fallback_used = True
            pending = policy.reorder_after_failure(pending, info.capability_tier)
            attempts += 1
            continue

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

        ok, latency_ms, data, error_kind, error_retry_after_ms, status_code, retryable = await _call_provider(
            info, effective_req
        )
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
                log,
                "provider_failed",
                provider=info.name,
                latency_ms=latency_ms,
                error_kind=error_kind,
                status_code=status_code,
            )
            last_error = f"provider_failed:{info.name}:{error_kind or 'unknown'}"
            fallback_used = True
            same_provider_retry_kinds = {"rate_limited", "timeout", "transport_error"}
            if (
                retryable
                and error_kind in same_provider_retry_kinds
                and local_attempt < max(1, int(info.max_retries))
            ):
                backoff_ms = _error_backoff_ms(
                    info,
                    intent,
                    local_attempt,
                    error_kind,
                    retry_after_ms=error_retry_after_ms,
                )
                log_event(
                    log,
                    "provider_retry_scheduled",
                    provider=info.name,
                    local_attempt=local_attempt,
                    max_retries=info.max_retries,
                    error_kind=error_kind,
                    backoff_ms=round(backoff_ms, 2),
                )
                await asyncio.sleep(backoff_ms / 1000.0)
                pending.insert(0, scored)
            else:
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
                    team_id=effective_req.team_id,
                    workflow_id=effective_req.workflow_id,
                    provider=info.name,
                    model=info.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost_usd=estimated_cost,
                    unix_ts=time.time(),
                )
            )
        )

<<<<<<< HEAD
=======
        _record_brownout_result(
            effective_req.tenant_id,
            latency_ms=(time.monotonic() - started) * 1000,
            success=True,
        )
        pp, rp = _completion_feedback_previews(effective_req, str(data.get("content") or ""))
        user_feedback.record_completion(
            CompletionMeta(
                request_id=request_id,
                tenant_id=effective_req.tenant_id,
                provider=info.name,
                model=info.model,
                intent=intent.value,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                latency_ms=round(latency_ms, 2),
                unix_ts=time.time(),
                prompt_preview=pp,
                response_preview=rp,
            )
        )
        if daily_quota_limit(info.name, settings) is not None:
            daily_quota_tracker.record_successful_completion(info.name)
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
<<<<<<< HEAD
        )

=======
            policy_evaluation=pe,
            brownout_status=bs,
        )

    _record_brownout_result(
        effective_req.tenant_id,
        latency_ms=(time.monotonic() - started) * 1000,
        success=False,
    )
>>>>>>> 2b788c2948bcc409fd824497816e061092d81ec0
    raise HTTPException(status_code=503, detail=f"all providers failed: {last_error}")


async def _call_provider(
    info: ProviderInfo, req: CompletionRequest
<<<<<<< HEAD
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
=======
) -> tuple[bool, float, dict | None, str | None, int, int | None, bool]:
    assert _http is not None
    start = time.monotonic()
    intent = classify(req)
    timeout_s = _provider_timeout_s(info, intent)
    try:
        ok, data = await call_provider(_http, info, req, timeout_s=timeout_s)
        return ok, (time.monotonic() - start) * 1000, data, None, 0, None, True
    except ProviderCallError as exc:
        log_event(
            log,
            "provider_call_error",
            provider=info.name,
            error=str(exc),
            error_kind=getattr(exc, "kind", "unknown"),
            status_code=getattr(exc, "status_code", None),
        )
        return (
            False,
            (time.monotonic() - start) * 1000,
            None,
            getattr(exc, "kind", "unknown"),
            int(getattr(exc, "retry_after_ms", 0) or 0),
            getattr(exc, "status_code", None),
            bool(getattr(exc, "retryable", False)),
        )
    except Exception as exc:
        log_event(log, "provider_call_error", provider=info.name, error=str(exc))
        return False, (time.monotonic() - start) * 1000, None, "unknown", 0, None, False


def _rank_candidates(
    mode: str,
    candidates: list[ProviderInfo],
    health: dict[str, ProviderHealth],
    intent: Intent,
    req: CompletionRequest,
 ) -> list[ScoredProvider]:
    global _rr_cursor

    def _naive_scored(items: list[ProviderInfo]) -> list[ScoredProvider]:
        n = max(1, len(items))
        return [
            ScoredProvider(
                provider=p,
                score=round(1.0 - (i / n), 6),
                sub_scores={"latency": 0.0, "cost": 0.0, "capability": 0.0, "success": 0.0},
            )
            for i, p in enumerate(items)
        ]

    if mode == "round_robin":
        if not candidates:
            return []
        ordered = sorted(candidates, key=lambda p: p.name)
        start = _rr_cursor % len(ordered)
        _rr_cursor += 1
        rotated = ordered[start:] + ordered[:start]
        return _naive_scored(rotated)
    if mode == "cheapest_first":
        ordered = sorted(candidates, key=lambda p: (p.cost_per_1k_tokens, p.name))
        return _naive_scored(ordered)
    if mode == "latency_first":
        ordered = sorted(candidates, key=lambda p: (p.typical_latency_ms, p.name))
        return _naive_scored(ordered)
    if mode == "premium_first":
        ordered = sorted(
            candidates,
            key=lambda p: (-p.capability_tier, p.typical_latency_ms, p.cost_per_1k_tokens, p.name),
        )
        return _naive_scored(ordered)
    return policy.rank(
        candidates,
        health=health,
        intent=intent,
        latency_budget_ms=req.latency_budget_ms,
        confidence_hint=req.confidence_hint,
    )


class RouteDecision(BaseModel):
    intent: str
    ranked: list[str]
    scores: dict[str, float]
    routing_mode: str = "intelliroute"
    policy_evaluation: Optional[PolicyEvaluationResult] = None
    brownout_status: Optional[BrownoutStatus] = None


class RoutingModeBody(BaseModel):
    mode: str


class UserFeedbackSubmit(BaseModel):
    request_id: str
    rating: Literal["positive", "negative", "helpful", "not_helpful"]
    comment: str = ""
    tenant_id: str = ""


class UserFeedbackAnalyzeRequest(BaseModel):
    tenant_id: str
    limit: Optional[int] = None
    force_refresh: bool = False


class ResetPayload(BaseModel):
    reset_feedback: bool = True
    reset_brownout: bool = True
    reset_queue: bool = True
    reset_tuner: bool = True
    reset_routing_mode: bool = True
    reset_provider_daily_quotas: bool = True
    reset_user_feedback: bool = True


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


@app.post("/reset")
async def reset_runtime_state(body: ResetPayload = ResetPayload()) -> dict:
    global _rr_cursor
    if body.reset_feedback:
        feedback.reset()
    if body.reset_brownout:
        brownout_manager.reset()
        for mgr in _tenant_brownout.values():
            mgr.reset()
        _tenant_brownout.clear()
    if body.reset_queue:
        request_queue.reset()
    if body.reset_tuner:
        weight_tuner.reset(reset_policy_weights=True)
    if body.reset_routing_mode:
        _set_routing_mode("intelliroute")
    if body.reset_provider_daily_quotas:
        daily_quota_tracker.clear()
    if body.reset_user_feedback:
        user_feedback.reset()
    _rr_cursor = 0
    return {
        "ok": True,
        "cleared": "router_runtime_state",
        "reset_feedback": body.reset_feedback,
        "reset_brownout": body.reset_brownout,
        "reset_queue": body.reset_queue,
        "reset_tuner": body.reset_tuner,
        "reset_routing_mode": body.reset_routing_mode,
        "reset_provider_daily_quotas": body.reset_provider_daily_quotas,
        "reset_user_feedback": body.reset_user_feedback,
    }


@app.get("/routing/mode")
async def routing_mode_status() -> dict:
    return {"mode": _get_routing_mode()}


@app.post("/routing/mode")
async def set_routing_mode(body: RoutingModeBody) -> dict:
    try:
        mode = _set_routing_mode(body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"mode": mode}


@app.post("/weights/rebalance/{intent}")
async def rebalance_weights(intent: str) -> dict:
    """Manually trigger a tuner rebalance for a given intent."""
    try:
        intent_enum = Intent(intent)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown intent: {intent}")
    applied = weight_tuner.maybe_rebalance(intent_enum)
    return {"intent": intent, "rebalanced": applied}
>>>>>>> 2b788c2948bcc409fd824497816e061092d81ec0


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
<<<<<<< HEAD
=======
            "quality_score": round(m.quality_score, 4),
>>>>>>> 2b788c2948bcc409fd824497816e061092d81ec0
            "sample_count": m.sample_count,
        }
        for name, m in metrics.items()
    }


@app.post("/feedback/submit")
async def submit_user_feedback(payload: UserFeedbackSubmit) -> dict:
    tenant = (payload.tenant_id or "").strip()
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant_id is required")
    rating = "positive" if payload.rating in {"positive", "helpful"} else "negative"
    try:
        row = user_feedback.submit_feedback(
            tenant_id=tenant,
            request_id=payload.request_id,
            rating=rating,
            comment=payload.comment,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="request_id not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="request_id does not belong to tenant")
    log_event(
        log,
        "user_feedback_recorded",
        tenant_id=tenant,
        request_id=payload.request_id,
        rating=rating,
        provider=row.get("provider"),
        model=row.get("model"),
    )
    return {"ok": True, "feedback": row}


@app.get("/feedback/recent")
async def feedback_recent(tenant_id: str, limit: int = 100) -> dict:
    lim = max(1, min(limit, 500))
    rows = user_feedback.recent_feedback(tenant_id=tenant_id, limit=lim)
    return {"tenant_id": tenant_id, "count": len(rows), "feedback": rows}


@app.get("/feedback/summary")
async def feedback_summary(tenant_id: str) -> dict:
    return user_feedback.summary(tenant_id=tenant_id)


@app.post("/feedback/analyze")
async def feedback_analyze(body: UserFeedbackAnalyzeRequest) -> dict:
    tenant_id = (body.tenant_id or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    max_rows = max(1, settings.feedback_analysis_max_rows)
    default_rows = max(1, settings.feedback_analysis_default_rows)
    raw_limit = body.limit if body.limit is not None else default_rows
    try:
        requested = int(raw_limit)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit must be an integer") from None
    effective_limit = max(1, min(requested, max_rows))
    limit_capped = effective_limit != requested

    revision = user_feedback.revision_for_tenant(tenant_id)
    if not body.force_refresh:
        cached = user_feedback.get_cached_analysis(
            tenant_id=tenant_id, sample_limit=effective_limit, revision=revision
        )
        if cached:
            return {
                "cached": True,
                "limit_capped": limit_capped,
                "requested_limit": requested,
                "analysis_limit": effective_limit,
                **cached,
            }

    summary = user_feedback.summary(tenant_id=tenant_id)
    if summary.get("total_feedback", 0) < 1:
        raise HTTPException(status_code=400, detail="no feedback available for analysis")

    pool_max = max(effective_limit, settings.feedback_analysis_sample_pool_max)
    sample = user_feedback.analysis_sample_rows(
        tenant_id=tenant_id, sample_limit=effective_limit, pool_max=pool_max
    )
    if not sample:
        raise HTTPException(status_code=400, detail="no feedback available for analysis")

    brief_rows: list[str] = []
    for r in sample:
        rid = str(r.get("request_id", ""))
        rid_short = f"{rid[:10]}…" if len(rid) > 12 else rid
        pp = (r.get("prompt_preview") or "").replace("\n", " ")
        rp = (r.get("response_preview") or "").replace("\n", " ")
        brief_rows.append(
            f"- request_id={rid_short} provider={r['provider']} model={r['model']} intent={r['intent']} "
            f"rating={r['rating']} tokens={r['total_tokens']} latency_ms={round(float(r['latency_ms']), 2)} "
            f"comment={(r.get('comment') or '').strip() or '(none)'} "
            f"prompt_preview={pp or '(none)'} response_preview={rp or '(none)'}"
        )
    summary_for_prompt = {k: v for k, v in summary.items() if k != "by_intent_provider"}
    provider_by_intent_block = format_provider_by_intent_for_prompt(summary)
    sample_n = len(sample)
    total_fb = int(summary.get("total_feedback") or 0)
    overall_sat_pct = round(float(summary.get("satisfaction") or 0.0) * 100)
    analysis_context = "\n".join(
        [
            "### What you are given (do not only repeat overall satisfaction)",
            f"- **Total feedback rows** in the database for this tenant: **{total_fb}**",
            f"- **Overall satisfaction** (positive ÷ all feedback): **{overall_sat_pct}%**",
            "- **Summary JSON** (all feedback rows, not just the sample): `total_feedback`, `positive_count`, "
            "`negative_count`, `satisfaction`, `by_provider`, `by_model`, `by_intent`, `negative_comments` "
            "(themes from negative comments).",
            "- **Provider-by-intent** block: satisfaction and counts **per provider within each intent** "
            "(all feedback rows).",
            f"- **Stratified sample** below: **{sample_n}** rows (cap requested/effective: **{effective_limit}**; "
            "drawn from a newest-first pool). Each line includes `rating`, `comment`, `provider`, `model`, "
            "`intent`, `tokens` (total_tokens), `latency_ms`, `prompt_preview`, `response_preview`.",
            "",
            "### What to produce",
            "- Which **provider** looks strongest for **each intent**, and which **provider/model** pairs look weak.",
            "- **Low-sample warnings** where satisfaction is high or low but **n is tiny** (e.g. 100% with n=1).",
            "- **Common negative themes** from `negative_comments` and negative rows in the sample.",
            "- Brief **latency / token** observations from the sample only (qualitative; not full traffic).",
            "- **Suggested improvements** for routing weights, provider selection, prompts, or fallback behavior. "
            "Do **not** claim upstream providers (e.g. Gemini, Groq) are automatically retrained or tuned by this "
            "dashboard; stay with **suggested** operational changes only.",
            "",
            "### Summary aggregate (JSON; all feedback rows; excludes duplicate by_intent_provider tree)",
            json.dumps(summary_for_prompt, separators=(",", ":"), ensure_ascii=False),
            "",
            "### Provider performance by intent (all feedback rows; satisfaction = positive ÷ total)",
            provider_by_intent_block,
            "",
            f"### Stratified sample (n={sample_n}, cap={effective_limit})",
            "\n".join(brief_rows),
        ]
    )
    prompt = (
        "You are an admin analyst for an LLM router. Interpret the metrics below; do not merely restate "
        "overall satisfaction.\n"
        "Each sampled row has only truncated `prompt_preview` and `response_preview`. Do not assume missing "
        "full content; never invent full prompts or full model outputs.\n"
        "Cross-check provider/model/intent aggregates with the provider-by-intent block and the sample lines.\n\n"
        + analysis_context
    )
    req = CompletionRequest(
        tenant_id=tenant_id,
        messages=[
            {
                "role": "system",
                "content": (
                    "Be concise and actionable. Ground every claim in the supplied metrics or sample lines; "
                    "call out uncertainty from small n."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        intent_hint=Intent.REASONING,
        max_tokens=900,
        temperature=0.2,
    )
    result = await _execute_completion(str(uuid.uuid4()), req)
    cached = user_feedback.put_cached_analysis(
        tenant_id=tenant_id,
        sample_limit=effective_limit,
        revision=revision,
        analysis=result.content,
        provider=result.provider,
        model=result.model,
    )
    log_event(
        log,
        "user_feedback_ai_analysis_generated",
        tenant_id=tenant_id,
        provider=result.provider,
        model=result.model,
        total_feedback=summary.get("total_feedback", 0),
        analysis_sample_limit=effective_limit,
    )
    return {
        "cached": False,
        "limit_capped": limit_capped,
        "requested_limit": requested,
        "analysis_limit": effective_limit,
        **cached,
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


<<<<<<< HEAD
@app.post("/decide", response_model=RouteDecision)
async def decide(req: CompletionRequest) -> RouteDecision:
    """Introspection endpoint: return the routing decision without executing it."""
    intent = classify(req)
    health = await _fetch_health_snapshot()
    ranked = policy.rank(
        registry.all(), health=health, intent=intent, latency_budget_ms=req.latency_budget_ms
=======
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
    intent, health, candidates, pe, bs, _budget_ctx = await _prepare_routing(req)
    routing_mode = _get_routing_mode()
    ranked = _rank_candidates(routing_mode, candidates, health, intent, req)
    if ranked:
        before_q = ranked
        ranked = apply_daily_quota_to_ranked(ranked, settings)
        if not ranked and before_q:
            raise HTTPException(status_code=429, detail=QUOTA_EXHAUSTED_DETAIL)
    return RouteDecision(
        intent=intent.value,
        ranked=[s.provider.name for s in ranked],
        scores={s.provider.name: round(s.score, 4) for s in ranked},
        routing_mode=routing_mode,
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
>>>>>>> 2b788c2948bcc409fd824497816e061092d81ec0
    )


@app.post("/complete", response_model=CompletionResponse)
async def complete(req: CompletionRequest) -> CompletionResponse:
    request_id = str(uuid.uuid4())
    intent = classify(req)

    # Determine priority
    priority = INTENT_PRIORITY.get(intent, Priority.MEDIUM)
<<<<<<< HEAD
=======
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
>>>>>>> 2b788c2948bcc409fd824497816e061092d81ec0

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
<<<<<<< HEAD
=======
        _record_brownout_result(
            req.tenant_id, latency_ms=timeout_s * 1000.0, success=False, timed_out=True
        )
>>>>>>> 2b788c2948bcc409fd824497816e061092d81ec0
        raise HTTPException(
            status_code=504, detail=f"request processing timed out after {timeout_s}s"
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
