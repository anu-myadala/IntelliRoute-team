from __future__ import annotations

from intelliroute.router.user_feedback_store import (
    CompletionMeta,
    UserFeedbackStore,
    format_provider_by_intent_for_prompt,
)


def _meta(rid: str = "r1", tenant: str = "demo-tenant") -> CompletionMeta:
    return CompletionMeta(
        request_id=rid,
        tenant_id=tenant,
        provider="groq",
        model="llama-3.3-70b-versatile",
        intent="interactive",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        latency_ms=123.4,
        unix_ts=1.0,
    )


def test_submit_and_recent_and_summary(tmp_path) -> None:
    s = UserFeedbackStore(str(tmp_path / "a.sqlite"))
    try:
        s.record_completion(_meta("r1"))
        s.record_completion(_meta("r2"))
        s.submit_feedback(tenant_id="demo-tenant", request_id="r1", rating="positive", comment="good")
        s.submit_feedback(tenant_id="demo-tenant", request_id="r2", rating="negative", comment="too slow")

        rows = s.recent_feedback(tenant_id="demo-tenant", limit=10)
        assert len(rows) == 2
        summary = s.summary(tenant_id="demo-tenant")
        assert summary["total_feedback"] == 2
        assert summary["positive_count"] == 1
        assert summary["negative_count"] == 1
        assert "too slow" in summary["negative_comments"]
        assert "groq" in summary["by_provider"]
        bip = summary["by_intent_provider"]
        assert "interactive" in bip
        assert bip["interactive"]["groq"]["total"] == 2
        assert bip["interactive"]["groq"]["positive"] == 1
        assert bip["interactive"]["groq"]["negative"] == 1
        txt = format_provider_by_intent_for_prompt(summary)
        assert "Intent interactive" in txt
        assert "groq" in txt
    finally:
        s.close()


def test_submit_requires_known_request_and_tenant_match(tmp_path) -> None:
    s = UserFeedbackStore(str(tmp_path / "b.sqlite"))
    try:
        s.record_completion(_meta("r1", tenant="t1"))
        try:
            s.submit_feedback(tenant_id="t1", request_id="missing", rating="positive")
            assert False, "expected KeyError"
        except KeyError:
            pass
        try:
            s.submit_feedback(tenant_id="t2", request_id="r1", rating="positive")
            assert False, "expected PermissionError"
        except PermissionError:
            pass
    finally:
        s.close()


def test_analysis_cache_roundtrip(tmp_path) -> None:
    s = UserFeedbackStore(str(tmp_path / "c.sqlite"))
    try:
        assert s.get_cached_analysis(tenant_id="demo-tenant", sample_limit=100, revision="rev1") is None
        row = s.put_cached_analysis(
            tenant_id="demo-tenant",
            sample_limit=100,
            revision="rev1",
            analysis="summary",
            provider="groq",
            model="m",
        )
        assert row["analysis"] == "summary"
        got = s.get_cached_analysis(tenant_id="demo-tenant", sample_limit=100, revision="rev1")
        assert got and got["analysis"] == "summary"
    finally:
        s.close()


def test_submit_invalidates_analysis_cache(tmp_path) -> None:
    s = UserFeedbackStore(str(tmp_path / "d.sqlite"))
    try:
        s.record_completion(_meta("r1"))
        s.put_cached_analysis(
            tenant_id="demo-tenant",
            sample_limit=100,
            revision="rev0",
            analysis="x",
            provider="p",
            model="m",
        )
        assert s.get_cached_analysis(tenant_id="demo-tenant", sample_limit=100, revision="rev0") is not None
        s.submit_feedback(tenant_id="demo-tenant", request_id="r1", rating="positive", comment="")
        assert s.get_cached_analysis(tenant_id="demo-tenant", sample_limit=100, revision="rev0") is None
    finally:
        s.close()


def test_cache_differentiates_sample_limit(tmp_path) -> None:
    s = UserFeedbackStore(str(tmp_path / "e.sqlite"))
    try:
        s.put_cached_analysis(
            tenant_id="demo-tenant",
            sample_limit=100,
            revision="r1",
            analysis="a100",
            provider="p",
            model="m",
        )
        s.put_cached_analysis(
            tenant_id="demo-tenant",
            sample_limit=500,
            revision="r1",
            analysis="a500",
            provider="p",
            model="m",
        )
        assert (
            s.get_cached_analysis(tenant_id="demo-tenant", sample_limit=100, revision="r1")["analysis"]
            == "a100"
        )
        assert (
            s.get_cached_analysis(tenant_id="demo-tenant", sample_limit=500, revision="r1")["analysis"]
            == "a500"
        )
    finally:
        s.close()
