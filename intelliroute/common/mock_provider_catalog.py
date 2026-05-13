"""Canonical ProviderInfo metadata for the three demo mock LLM backends.

Used by the router bootstrap and by each mock process for self-registration
so capability, cost, latency, and tier never drift.
"""
from __future__ import annotations

from typing import Final

from intelliroute.common.config import Settings
from intelliroute.common.models import ProviderInfo

MOCK_PROVIDER_NAMES: Final[tuple[str, ...]] = ("mock-fast", "mock-smart", "mock-cheap")


def mock_provider_info(name: str, host: str, port: int) -> ProviderInfo:
    """Build :class:`ProviderInfo` for a mock listening on ``host:port``."""
    url = f"http://{host}:{port}"
    if name == "mock-fast":
        return ProviderInfo(
            name=name,
            url=url,
            model="fast-1",
            provider_type="mock",
            capability={"interactive": 0.85, "reasoning": 0.45, "batch": 0.5, "code": 0.6},
            cost_per_1k_tokens=0.002,
            typical_latency_ms=30,
            capability_tier=2,
        )
    if name == "mock-smart":
        return ProviderInfo(
            name=name,
            url=url,
            model="smart-1",
            provider_type="mock",
            capability={"interactive": 0.7, "reasoning": 0.95, "batch": 0.8, "code": 0.9},
            cost_per_1k_tokens=0.02,
            typical_latency_ms=120,
            capability_tier=3,
        )
    if name == "mock-cheap":
        return ProviderInfo(
            name=name,
            url=url,
            model="cheap-1",
            provider_type="mock",
            capability={"interactive": 0.55, "reasoning": 0.4, "batch": 0.75, "code": 0.45},
            cost_per_1k_tokens=0.0003,
            typical_latency_ms=80,
            capability_tier=1,
        )
    raise ValueError(f"unknown mock provider name: {name!r}")


def list_mock_provider_infos_from_settings(settings: Settings) -> list[ProviderInfo]:
    """All three demo mocks using configured host and mock ports (router bootstrap)."""
    h = settings.host
    mapping = {
        "mock-fast": settings.mock_fast_port,
        "mock-smart": settings.mock_smart_port,
        "mock-cheap": settings.mock_cheap_port,
    }
    return [mock_provider_info(n, h, mapping[n]) for n in MOCK_PROVIDER_NAMES]
