<<<<<<< HEAD
"""In-memory service registry for LLM providers.

This plays the role of Consul/etcd in the course's "service discovery"
topic. Providers register themselves, and the router queries by intent to
find candidates. Health information is applied on top of the registry by
the routing policy.
=======
"""In-memory service registry for LLM providers with optional heartbeat TTL.

Bootstrap providers (mocks / static config) use ``lease_ttl_seconds=None`` and
never expire. API-registered providers must send periodic heartbeats within
``lease_ttl_seconds`` or they are excluded from :meth:`all_active` until they
recover or are removed.
>>>>>>> 2b788c2948bcc409fd824497816e061092d81ec0
"""
from __future__ import annotations

import threading
<<<<<<< HEAD
from typing import Iterable, Optional

from ..common.models import ProviderInfo
=======
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from ..common.models import ProviderInfo, ProviderRegisterRequest


@dataclass
class ProviderEntry:
    """Registry row: wire ``ProviderInfo`` plus discovery / liveness metadata."""

    info: ProviderInfo
    provider_id: str
    lease_ttl_seconds: float | None
    last_heartbeat_at: float | None
    registration_source: str
    model_tier: str
    registered_at: float

    def is_routable(self, now: float) -> bool:
        if self.lease_ttl_seconds is None:
            return True
        if self.last_heartbeat_at is None:
            return False
        return (now - self.last_heartbeat_at) <= self.lease_ttl_seconds
>>>>>>> 2b788c2948bcc409fd824497816e061092d81ec0


class ProviderRegistry:
    def __init__(self) -> None:
<<<<<<< HEAD
        self._providers: dict[str, ProviderInfo] = {}
        self._lock = threading.Lock()

    def register(self, info: ProviderInfo) -> None:
        with self._lock:
            self._providers[info.name] = info

    def deregister(self, name: str) -> None:
        with self._lock:
            self._providers.pop(name, None)

    def get(self, name: str) -> Optional[ProviderInfo]:
        with self._lock:
            return self._providers.get(name)

    def all(self) -> list[ProviderInfo]:
        with self._lock:
            return list(self._providers.values())

    def bulk_register(self, providers: Iterable[ProviderInfo]) -> None:
        with self._lock:
            for p in providers:
                self._providers[p.name] = p
=======
        self._providers: dict[str, ProviderEntry] = {}
        self._id_to_name: dict[str, str] = {}
        self._lock = threading.Lock()

    def register_bootstrap(self, info: ProviderInfo) -> None:
        """Register a static provider that does not require heartbeats."""
        now = time.time()
        pid = info.name
        with self._lock:
            self._providers[info.name] = ProviderEntry(
                info=info,
                provider_id=pid,
                lease_ttl_seconds=None,
                last_heartbeat_at=None,
                registration_source="bootstrap",
                model_tier=info.provider_type,
                registered_at=now,
            )
            self._id_to_name[pid] = info.name

    def register(self, info: ProviderInfo) -> None:
        """Backward-compatible alias for :meth:`register_bootstrap`."""
        self.register_bootstrap(info)

    def register_api(self, req: ProviderRegisterRequest) -> None:
        """Dynamic registration with heartbeat lease (overwrites same ``name``)."""
        now = time.time()
        pid = (req.provider_id or req.provider.name).strip()
        name = req.provider.name
        with self._lock:
            old = self._providers.pop(name, None)
            if old and old.provider_id in self._id_to_name:
                if self._id_to_name.get(old.provider_id) == name:
                    self._id_to_name.pop(old.provider_id, None)
            entry = ProviderEntry(
                info=req.provider,
                provider_id=pid,
                lease_ttl_seconds=req.lease_ttl_seconds,
                last_heartbeat_at=now,
                registration_source=req.registration_source,
                model_tier=req.model_tier or req.provider.provider_type,
                registered_at=now,
            )
            self._providers[name] = entry
            self._id_to_name[pid] = name

    def heartbeat(self, provider_id: str, now: float | None = None) -> bool:
        """Refresh lease for a dynamic provider. Returns False if unknown or static."""
        t = time.time() if now is None else now
        with self._lock:
            name = self._id_to_name.get(provider_id)
            if name is None and provider_id in self._providers:
                name = provider_id
            if name is None:
                return False
            e = self._providers.get(name)
            if e is None or e.lease_ttl_seconds is None:
                return False
            self._providers[name] = ProviderEntry(
                info=e.info,
                provider_id=e.provider_id,
                lease_ttl_seconds=e.lease_ttl_seconds,
                last_heartbeat_at=t,
                registration_source=e.registration_source,
                model_tier=e.model_tier,
                registered_at=e.registered_at,
            )
            return True

    def deregister(self, name: str) -> None:
        with self._lock:
            e = self._providers.pop(name, None)
            if e is None:
                return
            if self._id_to_name.get(e.provider_id) == name:
                self._id_to_name.pop(e.provider_id, None)

    def get(self, name: str) -> Optional[ProviderInfo]:
        with self._lock:
            e = self._providers.get(name)
            return e.info if e else None

    def get_entry(self, name: str) -> Optional[ProviderEntry]:
        with self._lock:
            return self._providers.get(name)

    def all_entries(self) -> list[ProviderEntry]:
        with self._lock:
            return list(self._providers.values())

    def all_active(self, now: float) -> list[ProviderInfo]:
        """Providers eligible for routing (heartbeat lease valid or bootstrap)."""
        with self._lock:
            return [e.info for e in self._providers.values() if e.is_routable(now)]

    def all(self) -> list[ProviderInfo]:
        """Same as :meth:`all_active` at current time (backward compatible name)."""
        return self.all_active(time.time())

    def stale_names(self, now: float) -> list[str]:
        """Names present in the registry but not routable (TTL expired)."""
        with self._lock:
            return [
                name
                for name, e in self._providers.items()
                if not e.is_routable(now)
            ]

    def bulk_register(self, providers: Iterable[ProviderInfo]) -> None:
        for p in providers:
            self.register_bootstrap(p)

    def discovery_snapshot(self, now: float) -> list[dict]:
        """Structured rows for observability / debug API."""
        rows: list[dict] = []
        with self._lock:
            for name, e in self._providers.items():
                sec_since: float | None = None
                if e.last_heartbeat_at is not None:
                    sec_since = round(now - e.last_heartbeat_at, 3)
                rows.append(
                    {
                        "provider_id": e.provider_id,
                        "name": name,
                        "model": e.info.model,
                        "provider_type": e.info.provider_type,
                        "model_tier": e.model_tier,
                        "url": e.info.url,
                        "lease_ttl_seconds": e.lease_ttl_seconds,
                        "last_heartbeat_at": e.last_heartbeat_at,
                        "seconds_since_heartbeat": sec_since,
                        "registration_source": e.registration_source,
                        "registered_at": e.registered_at,
                        "routable": e.is_routable(now),
                        "cost_per_1k_tokens": e.info.cost_per_1k_tokens,
                        "typical_latency_ms": e.info.typical_latency_ms,
                    }
                )
        return rows
>>>>>>> 2b788c2948bcc409fd824497816e061092d81ec0
