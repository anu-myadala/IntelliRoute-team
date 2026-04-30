"""Rate limiter HTTP service with multi-replica leader election support.

Wraps the ``RateLimiterStore`` in a FastAPI app with distributed leader election
using the bully algorithm. Gateway/router replicas call ``/check`` on the leader.
Followers forward requests to the leader and replicate state.

Environment Variables
---------------------
RATE_LIMITER_REPLICA_ID : str
    This replica's ID (default: "rl-0").
RATE_LIMITER_PEERS : str
    Comma-separated list of "id=url" pairs for all peers, e.g.
    "rl-0=http://localhost:8011,rl-1=http://localhost:8012"
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..common.logging import get_logger, log_event
from ..common.models import RateLimitCheck, RateLimitResult
from .election import ElectionConfig, ElectionState, LeaderElection, Peer
from .token_bucket import BucketConfig, RateLimiterStore

log = get_logger("rate_limiter")

# Reasonable defaults: 60 requests/min per (tenant, provider) pair with a
# burst of 10. Tweakable at runtime via /config.
_default = BucketConfig(capacity=10, refill_rate=1.0)
store = RateLimiterStore(default_config=_default)

# Opt-in strong consistency: when enabled, a follower with an expired
# leader lease fails closed instead of serving requests from its local
# bucket. Trades availability for guaranteed quota correctness — used to
# satisfy the spec's "strong consistency for quotas (avoid cost overruns)".
_STRONG_CONSISTENCY = os.environ.get("RATE_LIMITER_STRONG_CONSISTENCY", "0") == "1"

app = FastAPI(title="IntelliRoute RateLimiter")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_http: Optional[httpx.AsyncClient] = None
_election: Optional[LeaderElection] = None
_background_tasks: list[asyncio.Task] = []
_replication_offset: int = 0


def _setup_election() -> None:
    """Initialize leader election from environment variables."""
    global _election
    replica_id = os.environ.get("RATE_LIMITER_REPLICA_ID", "rl-0")
    peers_str = os.environ.get("RATE_LIMITER_PEERS", "")

    # Parse peers
    peers = []
    if peers_str:
        for pair in peers_str.split(","):
            pair = pair.strip()
            if "=" in pair:
                peer_id, url = pair.split("=", 1)
                peers.append(Peer(replica_id=peer_id.strip(), url=url.strip()))

    # Filter out self from peers list
    peers = [p for p in peers if p.replica_id != replica_id]

    config = ElectionConfig(
        election_timeout_s=2.0,
        heartbeat_interval_s=1.0,
        heartbeat_timeout_s=3.0,
    )
    _election = LeaderElection(replica_id, peers, config)
    store.set_leader(replica_id)  # Start as own leader candidate
    log_event(log, "election_initialized", replica_id=replica_id, peer_count=len(peers))


@app.on_event("startup")
async def _startup() -> None:
    global _http, _background_tasks
    _http = httpx.AsyncClient(timeout=5.0)
    _setup_election()

    # Start background tasks
    if _election:
        _background_tasks.append(asyncio.create_task(_run_election()))
        _background_tasks.append(asyncio.create_task(_heartbeat_loop()))
        _background_tasks.append(asyncio.create_task(_leader_watchdog()))
        _background_tasks.append(asyncio.create_task(_log_sync_loop()))


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _background_tasks
    if _http is not None:
        await _http.aclose()
    # Cancel background tasks
    for task in _background_tasks:
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)


async def _run_election() -> None:
    """Periodically check for leader timeout and run election if needed."""
    while True:
        try:
            if _election and _election.check_leader_timeout():
                log_event(log, "election_triggered", replica_id=_election._replica_id)
                _election.start_election()
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log_event(log, "election_error", error=str(exc))
        await asyncio.sleep(1.0)


async def _heartbeat_loop() -> None:
    """Send heartbeats if this replica is the leader."""
    while True:
        try:
            if _election and _election.is_leader:
                # In a real implementation, send heartbeats to followers
                await asyncio.sleep(_election._config.heartbeat_interval_s)
            else:
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log_event(log, "heartbeat_error", error=str(exc))


async def _leader_watchdog() -> None:
    """Monitor leader health and trigger election if needed."""
    while True:
        try:
            if _election:
                # Check if leader is still responsive
                if (
                    not _election.is_leader
                    and _election.current_leader
                    and _http
                ):
                    # Follower: check leader health
                    leader_url = None
                    for peer in _election._peers.values():
                        if peer.replica_id == _election.current_leader:
                            leader_url = peer.url
                            break

                    if leader_url:
                        try:
                            r = await _http.get(
                                f"{leader_url}/health", timeout=1.0
                            )
                            if r.status_code != 200:
                                # Leader is down
                                _election.start_election()
                        except Exception:
                            # Leader is unreachable
                            _election.start_election()

            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log_event(log, "watchdog_error", error=str(exc))


async def _log_sync_loop() -> None:
    """Followers pull and replay leader log entries for eventual convergence."""
    global _replication_offset
    while True:
        try:
            if _election and not _election.is_leader and _http:
                leader_id = _election.current_leader
                if not leader_id:
                    await asyncio.sleep(0.2)
                    continue
                leader_url = None
                for peer in _election._peers.values():
                    if peer.replica_id == leader_id:
                        leader_url = peer.url
                        break
                if not leader_url:
                    await asyncio.sleep(0.2)
                    continue

                r = await _http.get(f"{leader_url}/log/since/{_replication_offset}", timeout=2.0)
                if r.status_code == 200:
                    payload = r.json()
                    entries = payload.get("entries", [])
                    for e in entries:
                        store.replay_log_entry(
                            ts=float(e["ts"]),
                            key=str(e["key"]),
                            amount=float(e["amount"]),
                            allowed=bool(e["allowed"]),
                        )
                    _replication_offset = int(payload.get("total_length", _replication_offset))
                await asyncio.sleep(0.2)
            else:
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log_event(log, "sync_error", error=str(exc))
            await asyncio.sleep(0.5)


@app.get("/health")
async def health() -> dict:
    if _election:
        return {
            "status": "healthy",
            "replica_id": _election._replica_id,
            "leader": _election.current_leader,
            "state": _election.state.value,
        }
    return {"status": "healthy"}


@app.get("/leader")
async def leader() -> dict:
    return {"leader": store.leader_id}


@app.post("/check", response_model=RateLimitResult)
async def check(req: RateLimitCheck) -> RateLimitResult:
    forwarded = False
    if _election and not _election.is_leader:
        # Follower: forward to leader
        leader_id = _election.current_leader
        if leader_id and _http:
            leader_url = None
            for peer in _election._peers.values():
                if peer.replica_id == leader_id:
                    leader_url = peer.url
                    break

            if leader_url:
                try:
                    r = await _http.post(
                        f"{leader_url}/check",
                        json=req.model_dump(),
                        timeout=2.0,
                    )
                    if r.status_code == 200:
                        result = r.json()
                        forwarded = True
                        return RateLimitResult(**result)
                except Exception as exc:
                    log_event(
                        log,
                        "forward_to_leader_failed",
                        leader=leader_id,
                        error=str(exc),
                    )

        # Under strong-consistency mode, followers never decide locally.
        # If forwarding fails, fail closed (regardless of lease freshness).
        if _STRONG_CONSISTENCY and not forwarded:
            if not _election.has_valid_lease():
                log_event(
                    log,
                    "lease_expired_fail_closed",
                    replica=_election._replica_id,
                    last_heartbeat=_election._last_heartbeat,
                )
            else:
                log_event(
                    log,
                    "forward_failed_fail_closed",
                    replica=_election._replica_id,
                    leader=leader_id,
                )
            return RateLimitResult(
                allowed=False,
                remaining=0,
                retry_after_ms=int(_election._config.heartbeat_timeout_s * 1000),
                leader_replica=leader_id,
            )

    # Leader or follback: use local store
    key = f"{req.tenant_id}|{req.provider}"
    allowed, remaining, retry_after = store.try_consume(
        key, amount=req.tokens_requested
    )
    log_event(
        log,
        "rate_limit_check",
        key=key,
        allowed=allowed,
        remaining=round(remaining, 3),
        retry_after_ms=retry_after,
    )
    return RateLimitResult(
        allowed=allowed,
        remaining=remaining,
        retry_after_ms=retry_after,
        leader_replica=store.leader_id,
    )


class ConfigPayload(BaseModel):
    key: str
    capacity: float
    refill_rate: float


@app.post("/config")
async def set_config(payload: ConfigPayload) -> dict:
    store.set_config(payload.key, BucketConfig(payload.capacity, payload.refill_rate))
    return {"updated": payload.key}


class TenantProviderQuota(BaseModel):
    tenant_id: str
    provider: str
    capacity: float
    refill_rate: float


@app.post("/config/tenant-provider")
async def set_tenant_provider_quota(payload: TenantProviderQuota) -> dict:
    store.set_tenant_provider_quota(
        payload.tenant_id,
        payload.provider,
        BucketConfig(payload.capacity, payload.refill_rate),
    )
    return {"updated": f"{payload.tenant_id}|{payload.provider}"}


class TenantQuota(BaseModel):
    tenant_id: str
    capacity: float
    refill_rate: float


@app.post("/config/tenant")
async def set_tenant_quota(payload: TenantQuota) -> dict:
    store.set_tenant_default(
        payload.tenant_id,
        BucketConfig(payload.capacity, payload.refill_rate),
    )
    return {"updated": f"{payload.tenant_id}|*"}


class ProviderQuota(BaseModel):
    provider: str
    capacity: float
    refill_rate: float


class ResetPayload(BaseModel):
    clear_configs: bool = True
    clear_log: bool = True


@app.post("/reset")
async def reset_state(payload: ResetPayload = ResetPayload()) -> dict:
    global _replication_offset
    store.reset(clear_configs=payload.clear_configs, clear_log=payload.clear_log)
    _replication_offset = 0
    return {
        "ok": True,
        "cleared": "rate_limiter",
        "clear_configs": payload.clear_configs,
        "clear_log": payload.clear_log,
    }


@app.post("/config/provider")
async def set_provider_quota(payload: ProviderQuota) -> dict:
    store.set_provider_default(
        payload.provider,
        BucketConfig(payload.capacity, payload.refill_rate),
    )
    return {"updated": f"*|{payload.provider}"}


@app.get("/config/resolve/{tenant_id}/{provider}")
async def resolve_quota(tenant_id: str, provider: str) -> dict:
    cfg, source = store.resolve_config(f"{tenant_id}|{provider}")
    return {
        "tenant_id": tenant_id,
        "provider": provider,
        "capacity": cfg.capacity,
        "refill_rate": cfg.refill_rate,
        "source": source,
    }


@app.get("/log/since/{offset}")
async def log_since(offset: int) -> dict:
    """Return replication log entries since the given offset."""
    full_log = store.replication_log()
    entries = full_log[offset:] if offset < len(full_log) else []
    return {
        "offset": offset,
        "total_length": len(full_log),
        "entries": [
            {"ts": ts, "key": key, "amount": amount, "allowed": allowed}
            for ts, key, amount, allowed in entries
        ],
    }


@app.post("/election/challenge")
async def election_challenge(body: dict) -> dict:
    """Handle an election challenge from another replica."""
    if _election:
        challenger_id = body.get("challenger_id")
        if challenger_id:
            _election.receive_challenge(challenger_id)
            return {"acknowledged": True}
    return {"acknowledged": False}


@app.post("/election/victory")
async def election_victory(body: dict) -> dict:
    """Handle a victory announcement from a new leader."""
    if _election:
        leader_id = body.get("leader_id")
        if leader_id:
            _election.receive_victory(leader_id)
            store.set_leader(leader_id)
            return {"acknowledged": True}
    return {"acknowledged": False}


@app.post("/election/heartbeat")
async def election_heartbeat(body: dict) -> dict:
    """Handle a heartbeat from the leader."""
    if _election:
        leader_id = body.get("leader_id")
        if leader_id:
            _election.receive_heartbeat(leader_id)
            return {"acknowledged": True}
    return {"acknowledged": False}


@app.get("/election/status")
async def election_status() -> dict:
    """Return the current election status."""
    if _election:
        return {
            "replica_id": _election._replica_id,
            "state": _election.state.value,
            "current_leader": _election.current_leader,
            "is_leader": _election.is_leader,
        }
    return {"error": "election not initialized"}


@app.get("/log")
async def replication_log() -> dict:
    return {"entries": store.replication_log()}
