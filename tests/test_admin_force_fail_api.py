"""Admin force-fail proxy for cloud mock-provider demos."""
from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException

from intelliroute.common.models import ProviderInfo
from intelliroute.router import main as router_main
from intelliroute.router.registry import ProviderRegistry


class FakeHttpClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def post(self, url: str, json: dict, timeout: float | None = None) -> httpx.Response:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(200, json={"force_fail": json["fail"]})


@pytest.mark.asyncio
async def test_admin_force_fail_proxies_to_registered_mock(monkeypatch) -> None:
    fake_http = FakeHttpClient()
    registry = ProviderRegistry()
    registry.register_bootstrap(
        ProviderInfo(
            name="mock-smart",
            url="http://127.0.0.1:9002",
            model="smart-1",
            provider_type="mock",
        )
    )
    monkeypatch.setattr(router_main, "registry", registry)
    monkeypatch.setattr(router_main, "_http", fake_http)

    body = router_main.AdminForceFailBody(fail=True)
    result = await router_main.admin_provider_force_fail("mock-smart", body)

    assert result["provider"] == "mock-smart"
    assert result["force_fail"] is True
    assert fake_http.calls == [
        {
            "url": "http://127.0.0.1:9002/admin/force_fail",
            "json": {"fail": True},
            "timeout": 3.0,
        }
    ]


@pytest.mark.asyncio
async def test_admin_force_fail_sets_router_flag_for_external_provider(monkeypatch) -> None:
    registry = ProviderRegistry()
    registry.register_bootstrap(
        ProviderInfo(
            name="gemini",
            url="https://generativelanguage.googleapis.com/v1beta",
            model="gemini-2.5-flash",
            provider_type="gemini",
        )
    )
    monkeypatch.setattr(router_main, "registry", registry)
    router_main._forced_provider_failures.clear()

    result = await router_main.admin_provider_force_fail("gemini", router_main.AdminForceFailBody(fail=True))

    assert result == {
        "provider": "gemini",
        "provider_type": "gemini",
        "force_fail": True,
        "mode": "router_simulated",
    }
    assert router_main._is_provider_force_failed("gemini") is True

    result = await router_main.admin_provider_force_fail("gemini", router_main.AdminForceFailBody(fail=False))

    assert result["force_fail"] is False
    assert router_main._is_provider_force_failed("gemini") is False


@pytest.mark.asyncio
async def test_forced_external_provider_fails_before_http_call() -> None:
    router_main._forced_provider_failures.clear()
    router_main._set_provider_force_failed("gemini", True)
    try:
        ok, _latency_ms, data, error_kind, _retry_ms, status_code, retryable = await router_main._call_provider(
            ProviderInfo(
                name="gemini",
                url="https://generativelanguage.googleapis.com/v1beta",
                model="gemini-2.5-flash",
                provider_type="gemini",
            ),
            router_main.CompletionRequest(
                tenant_id="t1",
                messages=[{"role": "user", "content": "explain fallback"}],
            ),
        )
    finally:
        router_main._forced_provider_failures.clear()

    assert ok is False
    assert data is None
    assert error_kind == "forced_failure"
    assert status_code == 503
    assert retryable is False
