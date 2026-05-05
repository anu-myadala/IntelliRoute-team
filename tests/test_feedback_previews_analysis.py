"""Previews, analysis limits, sampling, cache keys, and AI prompt shape."""
from __future__ import annotations

import asyncio

from intelliroute.common.models import ChatMessage, CompletionRequest, CompletionResponse
from intelliroute.router import main as router_main
from intelliroute.router.user_feedback_store import CompletionMeta, UserFeedbackStore


def test_truncate_preview_respects_max() -> None:
    long = "word " * 100
    out = router_main._truncate_preview_text(long, 40)
    assert len(out) <= 40
    assert "…" in out or len(out) < len(long)


def test_last_user_message_picks_last_user() -> None:
    msgs = [
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="mid"),
        ChatMessage(role="user", content="last one"),
    ]
    assert router_main._last_user_message_text(msgs) == "last one"


def test_feedback_row_includes_truncated_previews(tmp_path) -> None:
    s = UserFeedbackStore(str(tmp_path / "pv.sqlite"))
    try:
        long_prompt = "P" * 400
        long_resp = "R" * 500
        s.record_completion(
            CompletionMeta(
                request_id="rid-1",
                tenant_id="t1",
                provider="mock-fast",
                model="fast-1",
                intent="interactive",
                prompt_tokens=1,
                completion_tokens=2,
                total_tokens=3,
                latency_ms=1.0,
                unix_ts=1.0,
                prompt_preview=router_main._truncate_preview_text(long_prompt, 200),
                response_preview=router_main._truncate_preview_text(long_resp, 300),
            )
        )
        s.submit_feedback(tenant_id="t1", request_id="rid-1", rating="positive", comment="ok")
        row = s.recent_feedback(tenant_id="t1", limit=5)[0]
        assert len(row["prompt_preview"]) <= 201
        assert len(row["response_preview"]) <= 301
        assert "PPP" in row["prompt_preview"] or row["prompt_preview"].startswith("P")
        assert "full" not in row or "messages" not in row
    finally:
        s.close()


def test_summary_counts_all_rows_not_capped_at_10k(tmp_path) -> None:
    s = UserFeedbackStore(str(tmp_path / "many.sqlite"))
    try:
        for i in range(25):
            rid = f"r{i}"
            s.record_completion(
                CompletionMeta(
                    request_id=rid,
                    tenant_id="bulk",
                    provider="p",
                    model="m",
                    intent="interactive",
                    prompt_tokens=1,
                    completion_tokens=1,
                    total_tokens=2,
                    latency_ms=1.0,
                    unix_ts=float(i),
                    prompt_preview="x",
                    response_preview="y",
                )
            )
            s.submit_feedback(tenant_id="bulk", request_id=rid, rating="positive" if i % 2 == 0 else "negative", comment="")
        summ = s.summary(tenant_id="bulk")
        assert summ["total_feedback"] == 25
    finally:
        s.close()


def test_analysis_sample_respects_limit_and_priority(tmp_path) -> None:
    s = UserFeedbackStore(str(tmp_path / "samp.sqlite"))
    try:
        for i, (rating, com) in enumerate(
            [
                ("positive", ""),
                ("negative", ""),
                ("negative", "bad"),
                ("positive", "good"),
            ]
        ):
            rid = f"s{i}"
            s.record_completion(
                CompletionMeta(
                    request_id=rid,
                    tenant_id="sam",
                    provider="p",
                    model="m",
                    intent="interactive",
                    prompt_tokens=1,
                    completion_tokens=1,
                    total_tokens=2,
                    latency_ms=1.0,
                    unix_ts=float(i),
                    prompt_preview=f"pv{i}",
                    response_preview=f"rv{i}",
                )
            )
            s.submit_feedback(tenant_id="sam", request_id=rid, rating=rating, comment=com)
        sample = s.analysis_sample_rows(tenant_id="sam", sample_limit=2, pool_max=100)
        assert len(sample) == 2
        assert all(r["rating"] == "negative" for r in sample)
        assert any((r.get("comment") or "").strip() == "bad" for r in sample)
    finally:
        s.close()


def test_analysis_prompt_contains_previews_not_full_messages(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(router_main, "user_feedback", UserFeedbackStore(str(tmp_path / "ap.sqlite")))
    router_main.user_feedback.reset()
    router_main.user_feedback.record_completion(
        CompletionMeta(
            request_id="req-1",
            tenant_id="demo-tenant",
            provider="mock-fast",
            model="fast-1",
            intent="interactive",
            prompt_tokens=5,
            completion_tokens=7,
            total_tokens=12,
            latency_ms=1.0,
            unix_ts=1.0,
            prompt_preview="short prompt prev",
            response_preview="short resp prev",
        )
    )
    router_main.user_feedback.submit_feedback(
        tenant_id="demo-tenant",
        request_id="req-1",
        rating="negative",
        comment="too slow",
    )
    captured: dict = {}

    async def _fake_execute(_rid, req: CompletionRequest):
        captured["content"] = req.messages[-1].content
        return CompletionResponse(
            request_id="a1",
            provider="mock-smart",
            model="smart-1",
            content="analysis text",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            latency_ms=1.0,
            estimated_cost_usd=0.0,
        )

    monkeypatch.setattr(router_main, "_execute_completion", _fake_execute)
    req = router_main.UserFeedbackAnalyzeRequest(tenant_id="demo-tenant", limit=100, force_refresh=True)
    out = asyncio.run(router_main.feedback_analyze(req))
    assert out["cached"] is False
    assert out["analysis_limit"] == 100
    body = captured["content"]
    assert "prompt_preview=short prompt prev" in body
    assert "response_preview=short resp prev" in body
    assert "Summary aggregate" in body


def test_feedback_analyze_caps_limit_at_max(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(router_main, "user_feedback", UserFeedbackStore(str(tmp_path / "cap.sqlite")))
    router_main.user_feedback.reset()
    for i in range(3):
        rid = f"q{i}"
        router_main.user_feedback.record_completion(
            CompletionMeta(
                request_id=rid,
                tenant_id="demo-tenant",
                provider="p",
                model="m",
                intent="interactive",
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                latency_ms=1.0,
                unix_ts=float(i),
                prompt_preview="a",
                response_preview="b",
            )
        )
        router_main.user_feedback.submit_feedback(tenant_id="demo-tenant", request_id=rid, rating="positive", comment="")

    calls = []

    async def _fake_execute(_rid, req):
        calls.append(req)
        return CompletionResponse(
            request_id="x",
            provider="p",
            model="m",
            content="ok",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            latency_ms=1.0,
            estimated_cost_usd=0.0,
        )

    monkeypatch.setattr(router_main, "_execute_completion", _fake_execute)
    out = asyncio.run(
        router_main.feedback_analyze(
            router_main.UserFeedbackAnalyzeRequest(tenant_id="demo-tenant", limit=10_000, force_refresh=True)
        )
    )
    assert out["analysis_limit"] == 500
    assert out["limit_capped"] is True


def test_cache_miss_when_sample_limit_changes(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(router_main, "user_feedback", UserFeedbackStore(str(tmp_path / "cc.sqlite")))
    router_main.user_feedback.reset()
    router_main.user_feedback.record_completion(
        CompletionMeta(
            request_id="req-1",
            tenant_id="demo-tenant",
            provider="p",
            model="m",
            intent="interactive",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            latency_ms=1.0,
            unix_ts=1.0,
            prompt_preview="p",
            response_preview="r",
        )
    )
    router_main.user_feedback.submit_feedback(
        tenant_id="demo-tenant", request_id="req-1", rating="positive", comment=""
    )
    n = {"c": 0}

    async def _fake_execute(_rid, _req):
        n["c"] += 1
        return CompletionResponse(
            request_id="x",
            provider="p",
            model="m",
            content="a",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            latency_ms=1.0,
            estimated_cost_usd=0.0,
        )

    monkeypatch.setattr(router_main, "_execute_completion", _fake_execute)
    asyncio.run(
        router_main.feedback_analyze(router_main.UserFeedbackAnalyzeRequest(tenant_id="demo-tenant", limit=100))
    )
    asyncio.run(
        router_main.feedback_analyze(router_main.UserFeedbackAnalyzeRequest(tenant_id="demo-tenant", limit=500))
    )
    assert n["c"] == 2
