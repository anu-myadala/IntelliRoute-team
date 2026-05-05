from __future__ import annotations

import asyncio

import pytest

from intelliroute.common.models import CompletionResponse
from intelliroute.router import main as router_main
from intelliroute.router.user_feedback_store import CompletionMeta, UserFeedbackStore


@pytest.fixture(autouse=True)
def _isolated_router_user_feedback(tmp_path, monkeypatch):
    path = str(tmp_path / "router_user_feedback.sqlite")
    store = UserFeedbackStore(path)
    monkeypatch.setattr(router_main, "user_feedback", store)
    yield store
    store.close()


def _seed_completion() -> None:
    router_main.user_feedback.record_completion(
        CompletionMeta(
            request_id="req-1",
            tenant_id="demo-tenant",
            provider="groq",
            model="llama-3.3-70b-versatile",
            intent="interactive",
            prompt_tokens=5,
            completion_tokens=7,
            total_tokens=12,
            latency_ms=111.2,
            unix_ts=1.0,
        )
    )


def test_feedback_submit_recent_summary() -> None:
    router_main.user_feedback.reset()
    _seed_completion()
    payload = router_main.UserFeedbackSubmit(
        tenant_id="demo-tenant",
        request_id="req-1",
        rating="helpful",
        comment="nice answer",
    )
    out = asyncio.run(router_main.submit_user_feedback(payload))
    assert out["ok"] is True
    rec = asyncio.run(router_main.feedback_recent(tenant_id="demo-tenant", limit=10))
    assert rec["count"] == 1
    summ = asyncio.run(router_main.feedback_summary(tenant_id="demo-tenant"))
    assert summ["positive_count"] == 1
    assert summ["negative_count"] == 0


def test_feedback_analysis_uses_router_flow_and_caches(monkeypatch) -> None:
    router_main.user_feedback.reset()
    _seed_completion()
    router_main.user_feedback.submit_feedback(
        tenant_id="demo-tenant",
        request_id="req-1",
        rating="negative",
        comment="answer was too brief",
    )

    calls = {"n": 0}

    async def _fake_execute(_request_id, _req):
        calls["n"] += 1
        return CompletionResponse(
            request_id="analysis-r1",
            provider="mock-smart",
            model="smart-1",
            content="AI analysis summary",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            latency_ms=12.3,
            estimated_cost_usd=0.0001,
        )

    monkeypatch.setattr(router_main, "_execute_completion", _fake_execute)
    req = router_main.UserFeedbackAnalyzeRequest(tenant_id="demo-tenant", limit=20)
    r1 = asyncio.run(router_main.feedback_analyze(req))
    assert r1["cached"] is False
    assert calls["n"] == 1

    r2 = asyncio.run(router_main.feedback_analyze(req))
    assert r2["cached"] is True
    assert calls["n"] == 1


def test_feedback_resubmit_updates_row() -> None:
    router_main.user_feedback.reset()
    _seed_completion()
    asyncio.run(
        router_main.submit_user_feedback(
            router_main.UserFeedbackSubmit(
                tenant_id="demo-tenant",
                request_id="req-1",
                rating="helpful",
                comment="first",
            )
        )
    )
    asyncio.run(
        router_main.submit_user_feedback(
            router_main.UserFeedbackSubmit(
                tenant_id="demo-tenant",
                request_id="req-1",
                rating="not_helpful",
                comment="revised",
            )
        )
    )
    summ = asyncio.run(router_main.feedback_summary(tenant_id="demo-tenant"))
    assert summ["total_feedback"] == 1
    assert summ["negative_count"] == 1
    assert summ["positive_count"] == 0
    rec = asyncio.run(router_main.feedback_recent(tenant_id="demo-tenant", limit=5))
    assert rec["feedback"][0]["comment"] == "revised"


def test_reset_clears_user_feedback_sqlite_only() -> None:
    router_main.user_feedback.reset()
    _seed_completion()
    asyncio.run(
        router_main.submit_user_feedback(
            router_main.UserFeedbackSubmit(
                tenant_id="demo-tenant",
                request_id="req-1",
                rating="helpful",
                comment="x",
            )
        )
    )
    body = router_main.ResetPayload(
        reset_feedback=False,
        reset_brownout=False,
        reset_queue=False,
        reset_tuner=False,
        reset_routing_mode=False,
        reset_provider_daily_quotas=False,
        reset_user_feedback=True,
    )
    asyncio.run(router_main.reset_runtime_state(body))
    rec = asyncio.run(router_main.feedback_recent(tenant_id="demo-tenant", limit=10))
    assert rec["count"] == 0
