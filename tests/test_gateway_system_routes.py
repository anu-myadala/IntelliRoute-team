"""Gateway /v1/system/* passthrough routes (shape checks)."""
from __future__ import annotations

from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from intelliroute.gateway import main as gateway_main


def test_system_health_wraps_providers_key() -> None:
    client = TestClient(gateway_main.app)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert "/snapshot" in str(request.url)
        return httpx.Response(200, json={"mock-cheap": {"name": "mock-cheap", "circuit_state": "closed", "healthy": True}})

    transport = httpx.MockTransport(handler)
    mock_client = httpx.AsyncClient(transport=transport)

    with patch.object(gateway_main, "_http", mock_client):
        r = client.get("/v1/system/health")
    assert r.status_code == 200
    body = r.json()
    assert "providers" in body
    assert body["providers"]["mock-cheap"]["circuit_state"] == "closed"


def test_system_registry_proxy() -> None:
    client = TestClient(gateway_main.app)
    reg = {
        "providers": [{"name": "a", "routable": True}],
        "providers_active": 1,
        "providers_total": 1,
        "stale_names": [],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        if "providers/registry" in str(request.url):
            return httpx.Response(200, json=reg)
        return httpx.Response(404)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.object(gateway_main, "_http", mock_client):
        r = client.get("/v1/system/registry")
    assert r.status_code == 200
    assert r.json()["providers_active"] == 1
