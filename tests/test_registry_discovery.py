"""Heartbeat TTL and dynamic registration on ProviderRegistry."""
from __future__ import annotations

import time

from intelliroute.common.models import ProviderInfo, ProviderRegisterRequest
from intelliroute.router.registry import ProviderRegistry


def _info(name: str = "dyn-a") -> ProviderInfo:
    return ProviderInfo(
        name=name,
        url="http://127.0.0.1:9999",
        model="m1",
        provider_type="mock",
        capability={"interactive": 0.8, "reasoning": 0.5, "batch": 0.5, "code": 0.5},
        cost_per_1k_tokens=0.001,
        typical_latency_ms=100,
    )


def test_dynamic_register_requires_heartbeat_within_ttl():
    reg = ProviderRegistry()
    reg.register_api(
        ProviderRegisterRequest(
            provider_id="pid-1",
            provider=_info("dyn-a"),
            lease_ttl_seconds=2.0,
            registration_source="test",
            model_tier="standard",
        )
    )
    t0 = time.time()
    assert [p.name for p in reg.all_active(t0)] == ["dyn-a"]
    # No heartbeat refresh — past TTL
    assert reg.all_active(t0 + 3.0) == []
    assert "dyn-a" in reg.stale_names(t0 + 3.0)


def test_heartbeat_refreshes_lease():
    reg = ProviderRegistry()
    reg.register_api(
        ProviderRegisterRequest(
            provider_id="pid-2",
            provider=_info("dyn-b"),
            lease_ttl_seconds=2.0,
        )
    )
    t0 = time.time()
    assert reg.heartbeat("pid-2", now=t0 + 1.5) is True
    assert [p.name for p in reg.all_active(t0 + 2.0)] == ["dyn-b"]
    assert "dyn-b" not in reg.stale_names(t0 + 2.0)


def test_bootstrap_never_stales():
    reg = ProviderRegistry()
    reg.register_bootstrap(_info("boot-x"))
    now = time.time() + 1_000_000.0
    assert [p.name for p in reg.all_active(now)] == ["boot-x"]
    assert reg.stale_names(now) == []


def test_heartbeat_unknown_returns_false():
    reg = ProviderRegistry()
    assert reg.heartbeat("nope") is False


def test_discovery_snapshot_includes_routable_flag():
    reg = ProviderRegistry()
    reg.register_api(
        ProviderRegisterRequest(
            provider_id="pid-3",
            provider=_info("dyn-c"),
            lease_ttl_seconds=0.5,
        )
    )
    now = time.time() + 2.0
    snap = reg.discovery_snapshot(now)
    row = next(r for r in snap if r["name"] == "dyn-c")
    assert row["routable"] is False
    assert row["lease_ttl_seconds"] == 0.5


def test_deregister_removes_id_mapping():
    reg = ProviderRegistry()
    reg.register_api(
        ProviderRegisterRequest(
            provider_id="pid-4",
            provider=_info("dyn-d"),
            lease_ttl_seconds=60.0,
        )
    )
    assert reg.heartbeat("pid-4") is True
    reg.deregister("dyn-d")
    assert reg.heartbeat("pid-4") is False
