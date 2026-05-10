"""Router admin daily-quota snapshot for Overview UI."""
from __future__ import annotations

from fastapi.testclient import TestClient

from intelliroute.common.models import ProviderInfo
from intelliroute.router import main as router_main


def test_admin_daily_quotas_disabled() -> None:
    client = TestClient(router_main.app)
    r = client.get("/admin/daily-quotas")
    assert r.status_code == 200
    data = r.json()
    assert data["quotas_enabled"] is False
    assert data["leader"] is None
    assert data["empty_reason"] == "disabled"


def test_admin_daily_quotas_enabled_leader(monkeypatch) -> None:
    monkeypatch.setattr(router_main.settings, "enable_provider_daily_quotas", True)
    router_main.daily_quota_tracker.clear()
    router_main.daily_quota_tracker.record_successful_completion("mock-cheap")
    router_main.daily_quota_tracker.record_successful_completion("mock-cheap")
    client = TestClient(router_main.app)
    r = client.get("/admin/daily-quotas")
    assert r.status_code == 200
    data = r.json()
    assert data["quotas_enabled"] is True
    assert data["leader"] is not None
    assert data["leader"]["provider"] == "mock-cheap"
    assert data["leader"]["used"] == 2
    assert data["empty_reason"] is None


def test_admin_daily_quotas_enabled_no_usage(monkeypatch) -> None:
    monkeypatch.setattr(router_main.settings, "enable_provider_daily_quotas", True)
    router_main.daily_quota_tracker.clear()
    router_main.registry.register_bootstrap(
        ProviderInfo(name="mock-cheap", url="http://example", model="m")
    )
    client = TestClient(router_main.app)
    r = client.get("/admin/daily-quotas")
    assert r.status_code == 200
    data = r.json()
    assert data["quotas_enabled"] is True
    assert data["leader"] is None
    assert data["empty_reason"] == "no_usage"
