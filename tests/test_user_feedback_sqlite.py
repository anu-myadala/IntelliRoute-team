"""SQLite persistence, upsert semantics, and reset for user feedback."""
from __future__ import annotations

import os
import sqlite3

from intelliroute.router.user_feedback_store import CompletionMeta, UserFeedbackStore


def _meta(rid: str, tenant: str = "demo-tenant") -> CompletionMeta:
    return CompletionMeta(
        request_id=rid,
        tenant_id=tenant,
        provider="groq",
        model="llama-3.3-70b-versatile",
        intent="interactive",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        latency_ms=100.0,
        unix_ts=42.0,
    )


def test_sqlite_file_created(tmp_path) -> None:
    path = str(tmp_path / "nf.sqlite")
    assert not os.path.isfile(path)
    s = UserFeedbackStore(path)
    s.record_completion(_meta("a1"))
    s.close()
    assert os.path.isfile(path)


def test_feedback_survives_new_store_instance(tmp_path) -> None:
    path = str(tmp_path / "restart.sqlite")
    s1 = UserFeedbackStore(path)
    s1.record_completion(_meta("r1"))
    s1.submit_feedback(tenant_id="demo-tenant", request_id="r1", rating="positive", comment="ok")
    s1.close()

    s2 = UserFeedbackStore(path)
    rows = s2.recent_feedback(tenant_id="demo-tenant", limit=10)
    assert len(rows) == 1
    assert rows[0]["rating"] == "positive"
    s2.close()


def test_same_request_tenant_updates_single_row(tmp_path) -> None:
    path = str(tmp_path / "upsert.sqlite")
    s = UserFeedbackStore(path)
    s.record_completion(_meta("rid"))
    s.submit_feedback(tenant_id="demo-tenant", request_id="rid", rating="positive", comment="a")
    s.submit_feedback(tenant_id="demo-tenant", request_id="rid", rating="negative", comment="b")
    with sqlite3.connect(path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM user_feedback").fetchone()[0]
    assert n == 1
    rows = s.recent_feedback(tenant_id="demo-tenant", limit=5)
    assert rows[0]["rating"] == "negative"
    assert rows[0]["comment"] == "b"
    s.close()


def test_preserves_unix_ts_on_update(tmp_path) -> None:
    path = str(tmp_path / "ts.sqlite")
    s = UserFeedbackStore(path)
    s.record_completion(_meta("t1"))
    s.submit_feedback(tenant_id="demo-tenant", request_id="t1", rating="positive", comment="")
    first_ts = s.recent_feedback(tenant_id="demo-tenant", limit=1)[0]["unix_ts"]
    s.submit_feedback(tenant_id="demo-tenant", request_id="t1", rating="negative", comment="x")
    row = s.recent_feedback(tenant_id="demo-tenant", limit=1)[0]
    assert row["unix_ts"] == first_ts
    assert row["updated_ts"] >= row["unix_ts"]
    s.close()


def test_different_request_id_same_tenant_two_rows(tmp_path) -> None:
    path = str(tmp_path / "two.sqlite")
    s = UserFeedbackStore(path)
    s.record_completion(_meta("x1"))
    s.record_completion(_meta("x2"))
    s.submit_feedback(tenant_id="demo-tenant", request_id="x1", rating="positive", comment="")
    s.submit_feedback(tenant_id="demo-tenant", request_id="x2", rating="negative", comment="")
    with sqlite3.connect(path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM user_feedback").fetchone()[0]
    assert n == 2
    s.close()


def test_summary_counts_final_state_only(tmp_path) -> None:
    path = str(tmp_path / "sum.sqlite")
    s = UserFeedbackStore(path)
    s.record_completion(_meta("s1"))
    s.submit_feedback(tenant_id="demo-tenant", request_id="s1", rating="positive", comment="")
    s.submit_feedback(tenant_id="demo-tenant", request_id="s1", rating="negative", comment="bad")
    summ = s.summary(tenant_id="demo-tenant")
    assert summ["total_feedback"] == 1
    assert summ["positive_count"] == 0
    assert summ["negative_count"] == 1
    s.close()


def test_analysis_cache_invalidates_on_feedback_update(tmp_path) -> None:
    path = str(tmp_path / "cache.sqlite")
    s = UserFeedbackStore(path)
    s.record_completion(_meta("c1"))
    s.submit_feedback(tenant_id="demo-tenant", request_id="c1", rating="positive", comment="")
    rev = s.revision_for_tenant("demo-tenant")
    s.put_cached_analysis(
        tenant_id="demo-tenant",
        sample_limit=100,
        revision=rev,
        analysis="cached",
        provider="p",
        model="m",
    )
    assert s.get_cached_analysis(tenant_id="demo-tenant", sample_limit=100, revision=rev) is not None
    s.submit_feedback(tenant_id="demo-tenant", request_id="c1", rating="negative", comment="n")
    assert s.get_cached_analysis(tenant_id="demo-tenant", sample_limit=100, revision=rev) is None
    s.close()


def test_reset_clears_sqlite(tmp_path) -> None:
    path = str(tmp_path / "reset.sqlite")
    s = UserFeedbackStore(path)
    s.record_completion(_meta("z1"))
    s.submit_feedback(tenant_id="demo-tenant", request_id="z1", rating="positive", comment="")
    s.reset()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_feedback").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM user_feedback_completion").fetchone()[0] == 0
    assert s.recent_feedback(tenant_id="demo-tenant", limit=10) == []
    s.close()


def test_submit_after_restart_without_memory_uses_sqlite_completion(tmp_path) -> None:
    path = str(tmp_path / "meta.sqlite")
    s1 = UserFeedbackStore(path)
    s1.record_completion(_meta("m1"))
    s1.close()

    s2 = UserFeedbackStore(path)
    # No in-memory record_completion on s2
    s2.submit_feedback(tenant_id="demo-tenant", request_id="m1", rating="positive", comment="late")
    rows = s2.recent_feedback(tenant_id="demo-tenant", limit=5)
    assert len(rows) == 1
    assert rows[0]["comment"] == "late"
    s2.close()
