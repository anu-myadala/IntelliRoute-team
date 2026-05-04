"""Per-provider UTC daily quotas (opt-in)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from intelliroute.common.models import ProviderInfo
from intelliroute.router.policy import ScoredProvider
from intelliroute.router.provider_daily_quota import (
    QUOTA_EXHAUSTED_DETAIL,
    DailyQuotaTracker,
    apply_daily_quota_to_ranked,
    daily_quota_limit,
)


@dataclass
class _QuotaCfg:
    enable_provider_daily_quotas: bool = False
    gemini_daily_request_quota: int = 15
    groq_daily_request_quota: int = 700
    mock_fast_daily_request_quota: int = 2000
    mock_cheap_daily_request_quota: int = 3000
    mock_smart_daily_request_quota: int = 1000
    mock_fault_daily_request_quota: int = 500
    provider_quota_warn_ratio: float = 0.7


def _sp(name: str) -> ScoredProvider:
    return ScoredProvider(
        provider=ProviderInfo(name=name, url="http://x", model="m"),
        score=1.0,
        sub_scores={},
    )


def test_defaults_when_disabled_no_limit() -> None:
    s = _QuotaCfg(enable_provider_daily_quotas=False)
    assert daily_quota_limit("gemini", s) is None


def test_gemini_default_limit_when_enabled() -> None:
    s = _QuotaCfg(enable_provider_daily_quotas=True)
    assert daily_quota_limit("gemini", s) == 15


def test_groq_default_limit_when_enabled() -> None:
    s = _QuotaCfg(enable_provider_daily_quotas=True)
    assert daily_quota_limit("groq", s) == 700


def test_mock_limits_when_enabled() -> None:
    s = _QuotaCfg(enable_provider_daily_quotas=True)
    assert daily_quota_limit("mock-fast", s) == 2000
    assert daily_quota_limit("mock-cheap", s) == 3000
    assert daily_quota_limit("mock-smart", s) == 1000
    assert daily_quota_limit("mock-fault", s) == 500


def test_zero_limit_means_unlimited() -> None:
    s = _QuotaCfg(enable_provider_daily_quotas=True, gemini_daily_request_quota=0)
    assert daily_quota_limit("gemini", s) is None


def test_unknown_provider_unlimited() -> None:
    s = _QuotaCfg(enable_provider_daily_quotas=True)
    assert daily_quota_limit("custom-vendor", s) is None


def test_apply_disabled_passthrough() -> None:
    s = _QuotaCfg(enable_provider_daily_quotas=False)
    ranked = [_sp("gemini"), _sp("groq")]
    assert apply_daily_quota_to_ranked(ranked, s) == ranked


def test_apply_blocks_at_limit() -> None:
    s = _QuotaCfg(enable_provider_daily_quotas=True, gemini_daily_request_quota=2)
    tr = DailyQuotaTracker(day_fn=lambda: "2026-05-04")
    tr.record_successful_completion("gemini")
    tr.record_successful_completion("gemini")
    ranked = [_sp("gemini"), _sp("groq")]
    out = apply_daily_quota_to_ranked(ranked, s, tracker=tr)
    assert [x.provider.name for x in out] == ["groq"]


def test_apply_deprioritizes_warn_zone() -> None:
    s = _QuotaCfg(
        enable_provider_daily_quotas=True,
        gemini_daily_request_quota=10,
        groq_daily_request_quota=10,
        provider_quota_warn_ratio=0.7,
    )
    tr = DailyQuotaTracker(day_fn=lambda: "2026-05-04")
    for _ in range(7):
        tr.record_successful_completion("gemini")  # 7/10 >= 0.7, still room
    ranked = [_sp("gemini"), _sp("groq")]
    out = apply_daily_quota_to_ranked(ranked, s, tracker=tr)
    assert [x.provider.name for x in out] == ["groq", "gemini"]


def test_apply_all_blocked_empty() -> None:
    s = _QuotaCfg(enable_provider_daily_quotas=True, gemini_daily_request_quota=1)
    tr = DailyQuotaTracker(day_fn=lambda: "2026-05-04")
    tr.record_successful_completion("gemini")
    ranked = [_sp("gemini")]
    assert apply_daily_quota_to_ranked(ranked, s, tracker=tr) == []


def test_quota_exhausted_detail_shape() -> None:
    assert QUOTA_EXHAUSTED_DETAIL["error"] == "provider_quota_exhausted"
    assert QUOTA_EXHAUSTED_DETAIL["retry_after_ms"] == 86400000


def test_mock_over_quota_fallback_order() -> None:
    s = _QuotaCfg(
        enable_provider_daily_quotas=True,
        mock_fast_daily_request_quota=1,
        mock_smart_daily_request_quota=10,
    )
    tr = DailyQuotaTracker(day_fn=lambda: "d1")
    tr.record_successful_completion("mock-fast")
    ranked = [_sp("mock-fast"), _sp("mock-smart")]
    out = apply_daily_quota_to_ranked(ranked, s, tracker=tr)
    assert [x.provider.name for x in out] == ["mock-smart"]
