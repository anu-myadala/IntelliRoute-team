"""Unit tests for INTELLIROUTE_PROVIDER_MODE and bootstrap planning."""
from __future__ import annotations

from dataclasses import replace

import pytest

from intelliroute.common.config import Settings
from intelliroute.common.models import ProviderInfo
from intelliroute.common.provider_mode import (
    build_bootstrap_result,
    effective_provider_mode,
    normalize_provider_mode,
    should_skip_mock_uvicorn_subprocesses,
)


def _p(name: str) -> ProviderInfo:
    return ProviderInfo(name=name, url="http://example.invalid", model="m")


@pytest.fixture
def base_settings() -> Settings:
    """Isolate from developer `.env` (e.g. INTELLIROUTE_USE_MOCKS, API keys, provider_mode)."""
    s = Settings()
    return replace(
        s,
        use_mock_providers=False,
        provider_mode="auto",
        groq_api_key="",
        gemini_api_key="",
    )


def test_normalize_provider_mode_invalid_returns_auto() -> None:
    assert normalize_provider_mode("not_a_mode") == "auto"


def test_effective_forced_mocks_overrides_hybrid(base_settings: Settings) -> None:
    s = replace(
        base_settings,
        use_mock_providers=True,
        provider_mode="hybrid",
        groq_api_key="k",
    )
    assert effective_provider_mode(s) == "mock_only"


@pytest.mark.parametrize(
    "mode, groq, gemini, use_mock, expect_skip",
    [
        ("auto", "", "", False, False),
        ("auto", "x", "", False, True),
        ("auto", "", "", True, False),
        ("mock_only", "x", "y", False, False),
        ("hybrid", "x", "", False, False),
        ("external_only", "", "", False, True),
        ("external_only", "x", "", False, True),
    ],
)
def test_should_skip_mock_uvicorn(
    base_settings: Settings,
    mode: str,
    groq: str,
    gemini: str,
    use_mock: bool,
    expect_skip: bool,
) -> None:
    s = replace(
        base_settings,
        provider_mode=mode,
        groq_api_key=groq,
        gemini_api_key=gemini,
        use_mock_providers=use_mock,
    )
    assert should_skip_mock_uvicorn_subprocesses(s) == expect_skip


def test_bootstrap_auto_no_keys_mocks(base_settings: Settings) -> None:
    s = replace(base_settings, provider_mode="auto", groq_api_key="", gemini_api_key="")
    mocks = [_p("mock-fast")]
    r = build_bootstrap_result(s, [], mocks, "hybrid")
    assert [p.name for p in r.providers] == ["mock-fast"]
    assert not r.external_only_no_keys
    assert r.log_mode == "hybrid"


def test_bootstrap_auto_with_keys_external_only(base_settings: Settings) -> None:
    s = replace(base_settings, provider_mode="auto", groq_api_key="sk")
    ext = [_p("groq")]
    mocks = [_p("mock-fast")]
    r = build_bootstrap_result(s, ext, mocks, "hybrid")
    assert [p.name for p in r.providers] == ["groq"]
    assert r.log_mode == "external"


def test_bootstrap_mock_only_ignores_keys(base_settings: Settings) -> None:
    s = replace(base_settings, provider_mode="mock_only", groq_api_key="sk", gemini_api_key="sk")
    ext = [_p("groq"), _p("gemini")]
    mocks = [_p("a"), _p("b")]
    r = build_bootstrap_result(s, ext, mocks, "hybrid")
    assert [p.name for p in r.providers] == ["a", "b"]


def test_bootstrap_use_mocks_flag_like_mock_only(base_settings: Settings) -> None:
    s = replace(
        base_settings,
        use_mock_providers=True,
        provider_mode="hybrid",
        groq_api_key="sk",
    )
    ext = [_p("groq")]
    mocks = [_p("mock-cheap")]
    r = build_bootstrap_result(s, ext, mocks, "hybrid")
    assert [p.name for p in r.providers] == ["mock-cheap"]


def test_bootstrap_external_only_no_keys(base_settings: Settings) -> None:
    s = replace(base_settings, provider_mode="external_only")
    r = build_bootstrap_result(s, [], [_p("m")], "hybrid")
    assert r.providers == []
    assert r.external_only_no_keys


def test_bootstrap_external_only_with_keys(base_settings: Settings) -> None:
    s = replace(base_settings, provider_mode="external_only")
    ext = [_p("groq")]
    r = build_bootstrap_result(s, ext, [], "hybrid")
    assert [p.name for p in r.providers] == ["groq"]
    assert r.log_mode == "external_only"


def test_bootstrap_hybrid_both(base_settings: Settings) -> None:
    s = replace(
        base_settings,
        provider_mode="hybrid",
        gemini_api_key="gk",
    )
    ext = [_p("gemini")]
    mocks = [_p("mock-fast"), _p("mock-smart"), _p("mock-cheap")]
    r = build_bootstrap_result(s, ext, mocks, "hybrid")
    assert [p.name for p in r.providers] == [
        "gemini",
        "mock-fast",
        "mock-smart",
        "mock-cheap",
    ]


def test_bootstrap_hybrid_no_external_keys_still_mocks(base_settings: Settings) -> None:
    s = replace(base_settings, provider_mode="hybrid", groq_api_key="", gemini_api_key="")
    r = build_bootstrap_result(s, [], [_p("mock-fast")], "hybrid")
    assert [p.name for p in r.providers] == ["mock-fast"]


def test_bootstrap_hybrid_dynamic_skips_mock_catalog(base_settings: Settings) -> None:
    s = replace(base_settings, provider_mode="hybrid", groq_api_key="k")
    ext = [_p("groq")]
    mocks = [_p("mock-fast")]
    r = build_bootstrap_result(s, ext, mocks, "dynamic")
    assert [p.name for p in r.providers] == ["groq"]
    assert r.skipped_mock_bootstrap
