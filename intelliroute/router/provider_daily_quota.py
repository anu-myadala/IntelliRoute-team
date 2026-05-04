"""Per-provider UTC calendar-day request quotas (opt-in).

Separate from the distributed token-bucket rate limiter (tenant|provider
burst/refill). Use :env:`INTELLIROUTE_ENABLE_PROVIDER_DAILY_QUOTAS` to turn on.

Successful completions increment usage; at limit the provider is skipped; near
limit (warn ratio) providers are deprioritized after peers.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from ..common.logging import get_logger, log_event
from .policy import ScoredProvider

log = get_logger("router.quota")

QUOTA_EXHAUSTED_DETAIL: dict = {
    "error": "provider_quota_exhausted",
    "message": "All available providers have reached their daily quota. Please try again later.",
    "retry_after_ms": 86400000,
}


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


class DailyQuotaTracker:
    """Thread-safe counts keyed by (UTC date, provider name)."""

    def __init__(self, day_fn: Callable[[], str] | None = None) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str], int] = {}
        self._day_fn = day_fn or _utc_day

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()

    def usage(self, provider: str) -> int:
        day = self._day_fn()
        with self._lock:
            return self._counts.get((day, provider), 0)

    def record_successful_completion(self, provider: str) -> None:
        day = self._day_fn()
        with self._lock:
            k = (day, provider)
            self._counts[k] = self._counts.get(k, 0) + 1


daily_quota_tracker = DailyQuotaTracker()


def daily_quota_limit(provider: str, settings) -> int | None:
    """Positive int = cap; ``None`` = no quota for this provider."""
    if not getattr(settings, "enable_provider_daily_quotas", False):
        return None
    lut: dict[str, int] = {
        "gemini": settings.gemini_daily_request_quota,
        "groq": settings.groq_daily_request_quota,
        "mock-fast": settings.mock_fast_daily_request_quota,
        "mock-cheap": settings.mock_cheap_daily_request_quota,
        "mock-smart": settings.mock_smart_daily_request_quota,
        "mock-fault": settings.mock_fault_daily_request_quota,
    }
    lim = lut.get(provider, 0)
    if lim <= 0:
        return None
    return lim


def _in_warn_zone(used: int, limit: int, warn_ratio: float) -> bool:
    if limit <= 0 or warn_ratio <= 0:
        return False
    return used >= limit * warn_ratio and used < limit


def apply_daily_quota_to_ranked(
    ranked: list[ScoredProvider],
    settings,
    tracker: DailyQuotaTracker | None = None,
) -> list[ScoredProvider]:
    """Drop at-limit providers; deprioritize warn-zone providers (stable order)."""
    if not ranked or not getattr(settings, "enable_provider_daily_quotas", False):
        return ranked
    tr = tracker or daily_quota_tracker
    warn_ratio = float(getattr(settings, "provider_quota_warn_ratio", 0.7))

    not_blocked: list[ScoredProvider] = []
    for s in ranked:
        name = s.provider.name
        lim = daily_quota_limit(name, settings)
        if lim is None:
            not_blocked.append(s)
            continue
        used = tr.usage(name)
        if used >= lim:
            log_event(
                log,
                "provider_daily_quota_skip",
                provider=name,
                used=used,
                limit=lim,
                reason="daily_quota_reached",
            )
            continue
        not_blocked.append(s)

    non_warn: list[ScoredProvider] = []
    warn_list: list[ScoredProvider] = []
    for s in not_blocked:
        name = s.provider.name
        lim = daily_quota_limit(name, settings)
        if lim is None:
            non_warn.append(s)
            continue
        used = tr.usage(name)
        if _in_warn_zone(used, lim, warn_ratio):
            log_event(
                log,
                "provider_daily_quota_deprioritize",
                provider=name,
                used=used,
                limit=lim,
                warn_ratio=warn_ratio,
            )
            warn_list.append(s)
        else:
            non_warn.append(s)

    return non_warn + warn_list
