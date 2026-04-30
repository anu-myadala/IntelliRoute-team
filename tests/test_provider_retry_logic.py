from __future__ import annotations

import httpx
import pytest

from intelliroute.common.models import ChatMessage, CompletionRequest, ProviderInfo
from intelliroute.router.provider_clients import ProviderCallError, call_provider


def _req() -> CompletionRequest:
    return CompletionRequest(
        tenant_id="t1",
        messages=[ChatMessage(role="user", content="hello")],
    )


@pytest.mark.asyncio
async def test_mock_provider_429_maps_to_rate_limited_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "1"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    info = ProviderInfo(name="mock-fast", url="http://mock", model="m1", provider_type="mock")
    try:
        with pytest.raises(ProviderCallError) as exc:
            await call_provider(http, info, _req(), timeout_s=0.5)
        assert exc.value.kind == "rate_limited"
        assert exc.value.retry_after_ms >= 1000
        assert exc.value.retryable is True
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_mock_provider_5xx_maps_to_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    info = ProviderInfo(name="mock-fast", url="http://mock", model="m1", provider_type="mock")
    try:
        with pytest.raises(ProviderCallError) as exc:
            await call_provider(http, info, _req(), timeout_s=0.5)
        assert exc.value.kind == "server_error"
        assert exc.value.retryable is True
    finally:
        await http.aclose()
