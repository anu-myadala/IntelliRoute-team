# IntelliRoute — Architecture Document

## 1. Overview

IntelliRoute is a distributed control plane for multi-LLM orchestration.
It sits between client applications and upstream LLM providers, making
intelligent routing decisions based on intent classification, provider
health, cost constraints, and rate limits.

## 2. Design Goals

| Goal                    | Approach                                               |
|-------------------------|--------------------------------------------------------|
| Intent-aware routing    | Classify each request, pick the best provider per-intent |
| Fault tolerance         | Circuit breakers + automatic fallback chain             |
| Cost governance         | Async cost tracking with per-tenant budgets             |
| Distributed rate limits | Token-bucket with leader/replication-log model          |
| Observability           | Structured JSON logs, `/snapshot` endpoints, trace ids  |

## 3. Service Topology

```
Client
  │
  ▼
Gateway (:8000)  ── API-key auth, tenant rewriting, trace id
  │
  ▼
Router (:8001)   ── intent → policy → rank → fallback loop
  │
  ├──▶ Rate Limiter (:8002)     token-bucket, leader + replication log
  ├──▶ Health Monitor (:8004)   circuit breakers, liveness polling
  └──▶ Cost Tracker (:8003)     async cost events, rollups, alerts
  │
  ▼
Mock Providers (:9001-9003)     fast / smart / cheap
```

All inter-service communication is HTTP (FastAPI + httpx). Cost events
are fire-and-forget (async). Rate-limit and health checks are
synchronous because the router must have their answers before choosing
a provider.

## 4. Component Details

### 4.1 Gateway (`intelliroute/gateway/main.py`)

- Single public entry point for all client traffic.
- Authenticates via `X-API-Key` header → maps to a `tenant_id`.
- Generates `X-Request-Id` and propagates it to all downstream calls.
- Exposes `/v1/cost/summary` and `/v1/system/health` as passthroughs
  to the cost tracker and health monitor.

### 4.2 Router (`intelliroute/router/`)

- **Intent classifier** (`intent.py`): deterministic keyword/heuristic
  classifier. Recognises `interactive`, `reasoning`, `batch`, `code`.
- **Policy engine** (`policy.py`): multi-objective scoring. Each
  provider is scored on four axes (latency, cost, capability, success
  rate) with intent-specific weight vectors. Final score = weighted
  sum of normalised sub-scores.
- **Registry** (`registry.py`): in-memory service registry analogous
  to Consul/etcd. Stores `ProviderInfo` records with URL, model,
  capabilities, and cost metadata. Bootstrap rows use
  `lease_ttl_seconds=None` (always routable until removed). API
  registrations from mocks use a finite lease and require periodic
  `POST /providers/heartbeat` refresh or they become non-routable
  (still listed with `stale_names` in snapshots).
- **Mock catalog** (`common/mock_provider_catalog.py`): shared
  `ProviderInfo` definitions used by router bootstrap and by each mock’s
  `POST /providers/register` payload so metadata stays consistent.
- **Registration mode** (`INTELLIROUTE_MOCK_REGISTRATION`, default
  `hybrid`): `legacy` — router only bootstraps mocks (no mock client);
  `hybrid` — bootstrap for fast demo startup plus mocks overwrite with
  leased rows and heartbeats; `dynamic` — router skips mock bootstrap,
  mocks must register before routing.
- **Feedback** (`feedback.py`): tracks per-provider latency EMA and
  success-rate EMA. Fed back into the policy's success-rate sub-score.
- **Queue** (`queue.py`): priority request queue with load-shedding
  when depth exceeds a configurable threshold.
- **Main** (`main.py`): the FastAPI app. On startup, bootstrap may
  register mock providers (unless `dynamic` mode). Exposes
  `/providers/register`, `/providers/heartbeat`, and registry snapshots.
  The `/complete` endpoint runs the full routing + fallback pipeline.
  `/decide` returns the ranking without executing.

### 4.3 Rate Limiter (`intelliroute/rate_limiter/`)

- **Token bucket** (`token_bucket.py`): `TokenBucket` per
  `(tenant, provider)` key. `RateLimiterStore` wraps a dict of
  buckets, exposes a `leader_id`, and appends every mutation to a
  replication log that follower replicas can tail and replay.
- **Election** (`election.py`): simple leader-election protocol
  (bully-style) for multi-replica deployments.
- **Main** (`main.py`): `/check` (consume tokens), `/config` (update
  bucket), `/leader`, `/log`, `/election/status`.

### 4.4 Health Monitor (`intelliroute/health_monitor/`)

- **Circuit breaker** (`circuit_breaker.py`): three-state machine
  (closed → open → half-open) with a sliding error window. Configurable
  failure threshold and recovery timeout.
- **Main** (`main.py`): `/register` (register provider for polling),
  `/report/{provider}` (success/failure signal), `/snapshot` (all
  breaker states). Background task periodically polls registered
  provider `/health` endpoints.

### 4.5 Cost Tracker (`intelliroute/cost_tracker/`)

- **Accounting** (`accounting.py`): `CostAccountant` maintains
  per-tenant rollups (total requests, tokens, cost, per-provider
  breakdown) and fires budget-exceeded alerts.
- **Main** (`main.py`): `/events` (publish cost event), `/summary`,
  `/budget`, `/alerts`.

### 4.6 Mock Provider (`intelliroute/mock_provider/main.py`)

- Configurable via env vars: name, model, latency, jitter, failure
  rate, cost per 1K tokens.
- `/v1/chat` simulates an LLM completion.
- `/admin/force_fail` test hook to flip into failing mode.
- When `INTELLIROUTE_MOCK_REGISTRATION` is `hybrid` or `dynamic`, a
  background task registers with the router (retry with backoff if the
  router is not up yet), then sends heartbeats on
  `INTELLIROUTE_PROVIDER_HEARTBEAT_INTERVAL_SECONDS` (default 8s) so
  the lease (`INTELLIROUTE_PROVIDER_LEASE_TTL_SECONDS`, default 30s)
  stays valid.

## 5. Consistency Model

| Data path         | Consistency   | Rationale                                            |
|-------------------|---------------|------------------------------------------------------|
| Rate-limit checks | Strong (leader) | Must not over-issue tokens                          |
| Cost rollups      | Eventual      | Fire-and-forget; slight delay is acceptable          |
| Circuit breaker   | Per-instance  | Each health-monitor instance maintains its own state |
| Registry          | Per-router    | Bootstrap + optional dynamic leases; heartbeats refresh TTL; no cross-replica sync |

## 6. Failure Modes and Recovery

1. **Provider failure**: circuit breaker trips → router skips provider
   in fallback loop → request succeeds via next-best provider.
2. **Rate-limit exhaustion**: router receives 429 → falls back to
   next provider that has quota.
3. **Cost tracker down**: gateway fire-and-forget publish silently
   fails; routing is unaffected.
4. **All providers down**: router returns structured 503 error to
   the client rather than hanging.

## 7. Future Work

- Real Raft consensus for multi-replica rate limiter.
- Persistent state (Redis/Postgres) so restarts don't lose data.
- Learned intent classifier (fine-tuned small model).
- Real LLM provider adapters (OpenAI, Anthropic, local vLLM).
