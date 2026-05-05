"""User feedback records and aggregate analytics for admin views.

Rows are persisted in SQLite (see :env:`INTELLIROUTE_USER_FEEDBACK_DB_PATH`).
At most one feedback row per ``(request_id, tenant_id)``; resubmitting updates
the same row and preserves the original ``unix_ts``. Stores truncated
``prompt_preview`` / ``response_preview`` only (no full prompts/responses).

Summary aggregates include ``by_intent_provider`` (satisfaction and counts per
provider within each intent) for admin dashboards and AI analysis prompts.

AI analysis cache is keyed by ``(tenant_id, sample_limit, revision)`` where
``revision`` changes when feedback rows for that tenant change.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class CompletionMeta:
    request_id: str
    tenant_id: str
    provider: str
    model: str
    intent: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    unix_ts: float
    prompt_preview: str = ""
    response_preview: str = ""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {str(row[1]) for row in cur.fetchall()}


def _ensure_feedback_columns(conn: sqlite3.Connection) -> None:
    """Add new columns on existing DBs (SQLite)."""
    for table in ("user_feedback_completion", "user_feedback"):
        existing = _table_columns(conn, table)
        if "prompt_preview" not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN prompt_preview TEXT NOT NULL DEFAULT ''")
        if "response_preview" not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN response_preview TEXT NOT NULL DEFAULT ''")
    conn.commit()


class UserFeedbackStore:
    """SQLite-backed feedback; in-memory completion index and AI analysis cache."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            from ..common.config import settings

            self._db_path = settings.user_feedback_db_path
        else:
            self._db_path = db_path
        self._lock = threading.RLock()
        self._completions: dict[str, CompletionMeta] = {}
        self._analysis_cache: dict[tuple[str, int, str], dict] = {}
        self._conn: sqlite3.Connection | None = None
        self._ensure_db()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        parent = os.path.dirname(os.path.abspath(self._db_path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        return conn

    def _ensure_db(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_feedback_completion (
                    request_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    provider TEXT,
                    model TEXT,
                    intent TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    latency_ms REAL,
                    unix_ts REAL NOT NULL,
                    prompt_preview TEXT NOT NULL DEFAULT '',
                    response_preview TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (request_id, tenant_id)
                );

                CREATE TABLE IF NOT EXISTS user_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    provider TEXT,
                    model TEXT,
                    intent TEXT,
                    rating TEXT NOT NULL,
                    comment TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    latency_ms REAL,
                    unix_ts REAL NOT NULL,
                    updated_ts REAL NOT NULL,
                    prompt_preview TEXT NOT NULL DEFAULT '',
                    response_preview TEXT NOT NULL DEFAULT '',
                    UNIQUE(request_id, tenant_id)
                );
                """
            )
            conn.commit()
            _ensure_feedback_columns(conn)

    def reset(self) -> None:
        with self._lock:
            self._completions.clear()
            self._analysis_cache.clear()
            conn = self._connect()
            conn.execute("DELETE FROM user_feedback")
            conn.execute("DELETE FROM user_feedback_completion")
            conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @staticmethod
    def tenant_revision(conn: sqlite3.Connection, tenant_id: str) -> str:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(updated_ts), 0.0) FROM user_feedback WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        if row is None:
            return "0:0"
        return f"{int(row[0])}:{float(row[1]):.6f}"

    def invalidate_analysis_cache_for_tenant(self, tenant_id: str) -> None:
        with self._lock:
            rm = [k for k in self._analysis_cache if k[0] == tenant_id]
            for k in rm:
                del self._analysis_cache[k]

    def revision_for_tenant(self, tenant_id: str) -> str:
        with self._lock:
            return UserFeedbackStore.tenant_revision(self._connect(), tenant_id)

    def record_completion(self, meta: CompletionMeta) -> None:
        with self._lock:
            self._completions[meta.request_id] = meta
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO user_feedback_completion (
                    request_id, tenant_id, provider, model, intent,
                    prompt_tokens, completion_tokens, total_tokens, latency_ms, unix_ts,
                    prompt_preview, response_preview
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id, tenant_id) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    intent = excluded.intent,
                    prompt_tokens = excluded.prompt_tokens,
                    completion_tokens = excluded.completion_tokens,
                    total_tokens = excluded.total_tokens,
                    latency_ms = excluded.latency_ms,
                    unix_ts = excluded.unix_ts,
                    prompt_preview = excluded.prompt_preview,
                    response_preview = excluded.response_preview
                """,
                (
                    meta.request_id,
                    meta.tenant_id,
                    meta.provider,
                    meta.model,
                    meta.intent,
                    meta.prompt_tokens,
                    meta.completion_tokens,
                    meta.total_tokens,
                    meta.latency_ms,
                    meta.unix_ts,
                    meta.prompt_preview,
                    meta.response_preview,
                ),
            )
            conn.commit()

    def _meta_for_request(self, conn: sqlite3.Connection, request_id: str) -> CompletionMeta | None:
        cur = conn.execute(
            """
            SELECT request_id, tenant_id, provider, model, intent,
                   prompt_tokens, completion_tokens, total_tokens, latency_ms, unix_ts,
                   prompt_preview, response_preview
            FROM user_feedback_completion
            WHERE request_id = ?
            """,
            (request_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return CompletionMeta(
            request_id=row["request_id"],
            tenant_id=row["tenant_id"],
            provider=row["provider"] or "",
            model=row["model"] or "",
            intent=row["intent"] or "",
            prompt_tokens=int(row["prompt_tokens"] or 0),
            completion_tokens=int(row["completion_tokens"] or 0),
            total_tokens=int(row["total_tokens"] or 0),
            latency_ms=float(row["latency_ms"] or 0.0),
            unix_ts=float(row["unix_ts"]),
            prompt_preview=row["prompt_preview"] or "",
            response_preview=row["response_preview"] or "",
        )

    def submit_feedback(
        self,
        *,
        tenant_id: str,
        request_id: str,
        rating: str,
        comment: str = "",
    ) -> dict:
        now = time.time()
        with self._lock:
            meta = self._completions.get(request_id)
            conn = self._connect()
            if meta is None:
                meta = self._meta_for_request(conn, request_id)
            if meta is None:
                raise KeyError("request_id_not_found")
            if meta.tenant_id != tenant_id:
                raise PermissionError("tenant_mismatch_for_request_id")

            conn.execute(
                """
                INSERT INTO user_feedback (
                    request_id,
                    tenant_id,
                    provider,
                    model,
                    intent,
                    rating,
                    comment,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    latency_ms,
                    unix_ts,
                    updated_ts,
                    prompt_preview,
                    response_preview
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id, tenant_id)
                DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    intent = excluded.intent,
                    rating = excluded.rating,
                    comment = excluded.comment,
                    prompt_tokens = excluded.prompt_tokens,
                    completion_tokens = excluded.completion_tokens,
                    total_tokens = excluded.total_tokens,
                    latency_ms = excluded.latency_ms,
                    unix_ts = user_feedback.unix_ts,
                    updated_ts = excluded.updated_ts,
                    prompt_preview = excluded.prompt_preview,
                    response_preview = excluded.response_preview
                """,
                (
                    meta.request_id,
                    meta.tenant_id,
                    meta.provider,
                    meta.model,
                    meta.intent,
                    rating,
                    comment.strip(),
                    meta.prompt_tokens,
                    meta.completion_tokens,
                    meta.total_tokens,
                    meta.latency_ms,
                    now,
                    now,
                    meta.prompt_preview,
                    meta.response_preview,
                ),
            )
            conn.commit()
            self.invalidate_analysis_cache_for_tenant(tenant_id)

            cur = conn.execute(
                """
                SELECT request_id, tenant_id, provider, model, intent, rating, comment,
                       prompt_tokens, completion_tokens, total_tokens, latency_ms, unix_ts, updated_ts,
                       prompt_preview, response_preview
                FROM user_feedback
                WHERE request_id = ? AND tenant_id = ?
                """,
                (request_id, tenant_id),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("user_feedback row missing after upsert")
            return _row_to_dict(row)

    def recent_feedback(self, *, tenant_id: str, limit: int = 100) -> list[dict]:
        lim = max(1, limit)
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """
                SELECT request_id, tenant_id, provider, model, intent, rating, comment,
                       prompt_tokens, completion_tokens, total_tokens, latency_ms, unix_ts, updated_ts,
                       prompt_preview, response_preview
                FROM user_feedback
                WHERE tenant_id = ?
                ORDER BY updated_ts DESC
                LIMIT ?
                """,
                (tenant_id, lim),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]

    def _summary_rows(self, conn: sqlite3.Connection, tenant_id: str) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                """
                SELECT rating, provider, model, intent, comment
                FROM user_feedback
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchall()
        )

    def summary(self, *, tenant_id: str) -> dict:
        with self._lock:
            conn = self._connect()
            rows = self._summary_rows(conn, tenant_id)
        total = len(rows)
        pos = sum(1 for r in rows if r["rating"] == "positive")
        neg = sum(1 for r in rows if r["rating"] == "negative")

        by_provider: dict[str, dict] = {}
        by_model: dict[str, dict] = {}
        by_intent: dict[str, dict] = {}
        by_intent_provider: dict[str, dict[str, dict]] = {}
        negative_comments: list[str] = []

        def _bump(bucket: dict[str, dict], key: str, rating: str) -> None:
            if key not in bucket:
                bucket[key] = {"total": 0, "positive": 0, "negative": 0, "satisfaction": 0.0}
            b = bucket[key]
            b["total"] += 1
            if rating == "positive":
                b["positive"] += 1
            else:
                b["negative"] += 1

        def _bump_intent_provider(intent: str, provider: str, rating: str) -> None:
            if intent not in by_intent_provider:
                by_intent_provider[intent] = {}
            _bump(by_intent_provider[intent], provider, rating)

        for r in rows:
            p, m, it = r["provider"] or "", r["model"] or "", r["intent"] or ""
            _bump(by_provider, p, r["rating"])
            _bump(by_model, m, r["rating"])
            _bump(by_intent, it, r["rating"])
            _bump_intent_provider(it, p, r["rating"])
            if r["rating"] == "negative" and (r["comment"] or "").strip():
                negative_comments.append(str(r["comment"]).strip())

        for bucket in (by_provider, by_model, by_intent):
            for k, v in bucket.items():
                sat = (v["positive"] / v["total"]) if v["total"] else 0.0
                bucket[k]["satisfaction"] = round(sat, 4)

        for _intent, provs in by_intent_provider.items():
            for _p, v in provs.items():
                sat = (v["positive"] / v["total"]) if v["total"] else 0.0
                provs[_p]["satisfaction"] = round(sat, 4)

        overall_sat = (pos / total) if total else 0.0
        return {
            "tenant_id": tenant_id,
            "total_feedback": total,
            "positive_count": pos,
            "negative_count": neg,
            "satisfaction": round(overall_sat, 4),
            "by_provider": by_provider,
            "by_model": by_model,
            "by_intent": by_intent,
            "by_intent_provider": by_intent_provider,
            "negative_comments": negative_comments[:200],
        }

    def analysis_sample_rows(self, *, tenant_id: str, sample_limit: int, pool_max: int) -> list[dict]:
        """Recent-first pool (capped), then stratified sample for AI (no full prompt/response)."""
        cap = max(1, sample_limit)
        pool = max(cap, min(pool_max, 50_000))
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """
                SELECT request_id, tenant_id, provider, model, intent, rating, comment,
                       prompt_tokens, completion_tokens, total_tokens, latency_ms, unix_ts, updated_ts,
                       prompt_preview, response_preview
                FROM user_feedback
                WHERE tenant_id = ?
                ORDER BY updated_ts DESC
                LIMIT ?
                """,
                (tenant_id, pool),
            )
            rows = [_row_to_dict(r) for r in cur.fetchall()]
        return _stratified_feedback_sample(rows, cap)

    def get_cached_analysis(
        self, *, tenant_id: str, sample_limit: int, revision: str
    ) -> Optional[dict]:
        key = (tenant_id, sample_limit, revision)
        with self._lock:
            c = self._analysis_cache.get(key)
            return dict(c) if c else None

    def put_cached_analysis(
        self,
        *,
        tenant_id: str,
        sample_limit: int,
        revision: str,
        analysis: str,
        provider: str,
        model: str,
    ) -> dict:
        row = {
            "tenant_id": tenant_id,
            "analysis": analysis,
            "provider": provider,
            "model": model,
            "generated_at_unix": time.time(),
            "analysis_sample_limit": sample_limit,
            "analysis_revision": revision,
        }
        key = (tenant_id, sample_limit, revision)
        with self._lock:
            self._analysis_cache[key] = dict(row)
        return row


def format_provider_by_intent_for_prompt(summary: dict) -> str:
    """Compact provider×intent satisfaction block for admin-only LLM prompts."""
    bip = summary.get("by_intent_provider") or {}
    if not bip:
        return "(no provider-by-intent aggregates yet)"

    intent_totals: list[tuple[str, int]] = []
    for intent, provs in bip.items():
        vol = sum(int((v or {}).get("total", 0) or 0) for v in provs.values())
        intent_totals.append((intent, vol))
    intent_totals.sort(key=lambda x: (-x[1], x[0]))

    lines: list[str] = []
    for intent, _vol in intent_totals:
        provs = bip.get(intent) or {}
        rows = sorted(
            provs.items(),
            key=lambda kv: (
                -float((kv[1] or {}).get("satisfaction") or 0.0),
                -int((kv[1] or {}).get("total") or 0),
                kv[0],
            ),
        )
        label = (intent or "").strip() or "(unknown)"
        lines.append(f"Intent {label}:")
        if not rows:
            lines.append("  (no providers)")
            lines.append("")
            continue
        for prov, stats in rows:
            plabel = (prov or "").strip() or "(unknown)"
            total = int((stats or {}).get("total", 0) or 0)
            pos = int((stats or {}).get("positive", 0) or 0)
            neg = int((stats or {}).get("negative", 0) or 0)
            sat = float((stats or {}).get("satisfaction") or 0.0)
            pct = round(sat * 100)
            lines.append(f"  - {plabel}: {pct}% satisfaction, +{pos}/-{neg}, n={total}")
        lines.append("")
    return "\n".join(lines).strip()


def _stratified_feedback_sample(rows: list[dict], cap: int) -> list[dict]:
    """Prioritize recent negatives (esp. with comments), then other negatives, then positives with comments, then rest."""
    neg_c = [r for r in rows if r.get("rating") == "negative" and (r.get("comment") or "").strip()]
    neg_o = [r for r in rows if r.get("rating") == "negative" and not (r.get("comment") or "").strip()]
    pos_c = [r for r in rows if r.get("rating") == "positive" and (r.get("comment") or "").strip()]
    pos_o = [r for r in rows if r.get("rating") == "positive" and not (r.get("comment") or "").strip()]
    out: list[dict] = []
    for pool in (neg_c, neg_o, pos_c, pos_o):
        for r in pool:
            if len(out) >= cap:
                return out
            out.append(r)
    return out


def _row_to_dict(row: sqlite3.Row) -> dict:
    d: dict[str, Any] = {
        "request_id": row["request_id"],
        "tenant_id": row["tenant_id"],
        "provider": row["provider"] or "",
        "model": row["model"] or "",
        "intent": row["intent"] or "",
        "prompt_tokens": int(row["prompt_tokens"] or 0),
        "completion_tokens": int(row["completion_tokens"] or 0),
        "total_tokens": int(row["total_tokens"] or 0),
        "latency_ms": float(row["latency_ms"] or 0.0),
        "rating": row["rating"],
        "comment": row["comment"] or "",
        "unix_ts": float(row["unix_ts"]),
        "updated_ts": float(row["updated_ts"]),
    }
    if "prompt_preview" in row.keys():
        d["prompt_preview"] = row["prompt_preview"] or ""
    else:
        d["prompt_preview"] = ""
    if "response_preview" in row.keys():
        d["response_preview"] = row["response_preview"] or ""
    else:
        d["response_preview"] = ""
    return d
