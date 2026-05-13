# IntelliRoute — Architecture Document

## 1. Overview

IntelliRoute is a distributed control plane for multi-LLM orchestration.
It sits between client applications and upstream LLM providers (mock
demo providers, Groq, Gemini), making routing decisions based on intent
classification, provider health, multi-objective scoring, cost
constraints, and distributed rate limits — and gracefully degrades the
system under overload.

## 2. Design Goals

| Goal                       | Approach                                                                  |
|----------------------------|---------------------------------------------------------------------------|
| Intent-aware routing       | Classify each request, pick the best provider per-intent                  |
| Fault tolerance            | 3-state circuit breakers + automatic fallback chain + SLA-aware retries   |
| Adaptive routing weights   | Online weight tuner that reweights latency/cost/capability/success on EMA |
| Cost governance            | Async cost tracking with tenant / team / workflow budgets and premium caps |
| Distributed rate limits    | 3-replica token-bucket cluster with bully-style leader election           |
| Service discovery          | Bootstrap registry plus optional dynamic registration with TTL leases     |
| Graceful degradation       | Brownout manager + priority queue + load shedding under sustained overload |
| Observability              | Structured JSON logs, `/snapshot`, `/stats`, `/traces`, frontend dashboard |

## 3. Service Topology

```
Client
  │
  ▼
Gateway (:8000)            API-key auth, tenant rewriting, X-Request-Id
  │
  ▼
Router (:8001)             intent → policy_engine → rank → fallback loop
  │                        priority queue + 4 worker tasks + brownout manager
  │
  ├──▶ Rate Limiter cluster (:8002 rl-0, :8012 rl-1, :8022 rl-2)
  │      bully-style leader election; followers forward writes & tail the log
  ├──▶ Health Monitor (:8004)        3-state circuit breakers + liveness polling
  └──▶ Cost Tracker (:8003)          async cost events; tenant/team/workflow rollups
  │
  ▼
Mock Providers (:9001 fast, :9002 smart, :9003 cheap)
   + optional Groq / Gemini (when API keys are present)

Frontend (:3000)           static HTML/JS dashboard served by python -m http.server
```

All inter-service communication is HTTP (FastAPI + httpx). Cost events
are fire-and-forget (async). Rate-limit and health checks are
synchronous because the router must have their answers before choosing
a provider.

## 4. Component Details

### 4.1 Gateway (`intelliroute/gateway/main.py`)

- Single public entry point for all client traffic.
- Authenticates via `X-API-Key` header → maps to a `tenant_id` (the
  body's `tenant_id` is overwritten so clients cannot impersonate).
- Generates `X-Request-Id` and propagates it to all downstream calls.
- Exposes passthroughs for cost summaries, system health, and the user
  feedback API (`/v1/feedback`, `/v1/feedback/recent`,
  `/v1/feedback/summary`, `/v1/feedback/analyze`) so the dashboard
  never has to talk to internal services directly.
- CORS is open so the static dashboard on `:3000` can call it.

### 4.2 Router (`intelliroute/router/`)

- **Intent classifier** (`intent.py`): deterministic keyword/heuristic
  classifier. Recognises `interactive`, `reasoning`, `batch`, `code`.
  Honours an optional `intent_hint` on the request.
- **Policy engine** (`policy_engine/`): ordered, declarative pre-rank
  rules — batch avoids premium, premium requires reasoning or
  complexity, tenant/team/workflow budget downgrades, brownout
  degradation, interactive latency gate. Each rule contributes audit
  metadata returned in `policy_evaluation`.
- **Multi-objective ranker** (`policy.py`): scores survivors on four
  axes (latency, cost, capability, success rate) with intent-specific
  weight vectors. Final score = weighted sum of normalised sub-scores.
- **Online weight tuner** (`weight_tuner.py`): observes per-attempt
  outcomes, accumulates net credit per sub-score, and rebalances the
  policy's weights when enough samples accumulate. Triggerable manually
  via `POST /weights/rebalance/{intent}`.
- **Registry** (`registry.py`): in-memory service registry. Bootstrap
  rows have `lease_ttl_seconds=None` and never expire; API-registered
  rows must heartbeat (`POST /providers/heartbeat`) within their lease
  TTL or they fall out of the routable set (still listed by
  `/providers/registry` with `routable=false`).
- **Mock catalog** (`common/mock_provider_catalog.py`): shared
  `ProviderInfo` definitions reused by router bootstrap and by each
  mock's self-registration payload, so metadata stays consistent.
- **Registration mode** (`INTELLIROUTE_MOCK_REGISTRATION`, default
  `hybrid`): `legacy` — router only bootstraps mocks, no client-side
  registration; `hybrid` — bootstrap for fast startup *plus* mocks
  re-register with leases and heartbeats; `dynamic` — router skips mock
  bootstrap entirely, mocks must register before they're routable.
- **Provider clients** (`provider_clients.py`): adapters for the mock
  HTTP shape, Groq's OpenAI-compatible API, and Gemini. Wraps
  `ProviderCallError` with `kind` (`rate_limited`, `timeout`,
  `server_error`, `transport_error`) and a `retry_after_ms` hint that
  the router's back-off logic respects.
- **Provider mode** (`common/provider_mode.py`): `auto` (keys present →
  externals; otherwise mocks), `mock_only`, `external_only`, `hybrid`.
  `INTELLIROUTE_USE_MOCKS=1` forces effective `mock_only`.
- **Per-provider daily quotas** (`provider_daily_quota.py`): opt-in UTC
  daily caps on successful completions per provider. Skipped providers
  fall through to the next candidate in the fallback chain.
- **Feedback** (`feedback.py`): per-provider EMA of latency,
  success-rate, token efficiency, and an anomaly score that combines
  latency-band anomalies with a hallucination-signal proxy. Feeds back
  into the policy's success sub-score and surfaces a derived
  `quality_score` (see `docs/PROVIDER_QUALITY_SCORE.md`).
- **User feedback store** (`user_feedback_store.py`): SQLite-backed
  per-completion record (request id, prompt/response previews, rating).
  Supports stratified sampling and a revision-keyed analysis cache so
  `/feedback/analyze` only re-runs the LLM analyst when feedback has
  actually changed.
- **Queue** (`queue.py`): priority request queue. `INTERACTIVE` and
  `CODE` are HIGH (bypass the queue), `REASONING` is MEDIUM, `BATCH`
  is LOW. Load shedding above `shed_threshold`; per-priority depth caps;
  per-request timeout. Drained by 4 async worker tasks.
- **Brownout** (`brownout.py`): global plus per-tenant manager. Enters
  brownout when *any* of (queue depth, p95 latency, error rate, timeout
  rate) exceeds the enter threshold for `enter_consecutive` evaluations
  in a row; leaves only when all four sit below their exit threshold for
  `exit_consecutive` consecutive evaluations (hysteresis to avoid
  flapping). When degraded: drops or delays LOW-priority requests,
  blocks premium providers for MEDIUM/LOW, prefers low-latency
  providers, and clamps `max_tokens` for REASONING/BATCH.
- **Main** (`main.py`): the FastAPI app. Bootstrap can register mock
  and external providers; exposes `/complete`, `/decide`,
  `/providers/*`, `/feedback/*`, `/queue/stats`, `/brownout`,
  `/brownout/{tenant_id}`, `/traces`, `/weights`, `/routing/mode`,
  `/reset`. The `/complete` endpoint runs the full routing pipeline
  with budget gating, quota gating, retries, and cost publishing.
  `/decide` returns the ranking without executing for introspection.

### 4.3 Rate Limiter (`intelliroute/rate_limiter/`)

- **Token bucket** (`token_bucket.py`): `TokenBucket` per
  `(tenant, provider)` key with optional tenant-level / provider-level
  defaults. `RateLimiterStore` exposes a `leader_id` and appends every
  mutation to a replication log followers tail and replay.
- **Leader election** (`election.py`): bully-style — replicas exchange
  `/election/challenge`, `/election/victory`, `/election/heartbeat`.
  The leader serves all `/check` calls authoritatively; followers
  forward to the current leader and fall back to local state only if
  forwarding fails (or fail closed when
  `RATE_LIMITER_STRONG_CONSISTENCY=1`).
- **Replication log** (`/log`, `/log/since/{offset}`): incremental
  endpoint lets a follower pull only the entries it hasn't seen yet.
- **Main** (`main.py`): `/check`, `/config`, `/config/tenant`,
  `/config/provider`, `/config/tenant-provider`, `/config/resolve/...`,
  `/leader`, `/election/*`, `/log`, `/log/since/{offset}`, `/stats`,
  `/reset`. Three replicas run by default: `:8002` (rl-0), `:8012`
  (rl-1), `:8022` (rl-2).

### 4.4 Health Monitor (`intelliroute/health_monitor/`)

- **Circuit breaker** (`circuit_breaker.py`): three-state machine
  (`closed → open → half_open`) with a sliding error window.
  Configurable failure threshold, open-duration cooldown, and required
  half-open successes before re-closing.
- **Main** (`main.py`): `/register` (register provider URL for
  polling), `/report/{provider}` (router-pushed success/failure
  signal), `/snapshot` (all breaker states; advances OPEN→HALF_OPEN
  cooldowns lazily on read), `/snapshot/{provider}`, `/stats`,
  `/reset`. Background task periodically polls registered provider
  `/health` endpoints.

### 4.5 Cost Tracker (`intelliroute/cost_tracker/`)

- **Accounting** (`accounting.py`): per-tenant rollups (request count,
  tokens, cost, per-provider breakdown), per-team and per-workflow
  rollups, headroom checks, projected-cost gates, budget-exceeded
  alerts, and a per-team premium-spend cap.
- **Main** (`main.py`): `/events` (publish a cost event),
  `/summary/{tenant_id}`, `/summary/team/{team_id}`,
  `/summary/workflow/{workflow_id}`, `/budget`, `/budget/team`,
  `/budget/workflow`, `/budget/team/premium-cap`,
  `/budget/{tenant_id}`, `/budget/{tenant_id}/headroom`,
  `/budget/{tenant_id}/check?projected_cost_usd=…`, `/alerts`,
  `/history/{tenant_id}` (paginated raw events), `/stats`, `/reset`.

### 4.6 Mock Provider (`intelliroute/mock_provider/main.py`)

- Configurable via env vars: name, model, latency, jitter, failure
  rate, cost per 1K tokens.
- `/v1/chat` simulates an LLM completion.
- Fault-injection hooks for the four classes of upstream failure:
  - `/admin/force_fail` — return HTTP 503 on every request
  - `/admin/force_timeout` — sleep 300 s so the router's per-call
    timeout fires (exercises `timeout` retry path)
  - `/admin/force_rate_limit` — return HTTP 429 with `Retry-After: 5`
  - `/admin/force_malformed` — return HTTP 200 with broken JSON
- `/admin/reset` clears all four flags. `/admin/state` reports them.
- When `INTELLIROUTE_MOCK_REGISTRATION` is `hybrid` or `dynamic`, a
  background task self-registers with the router (retry with backoff
  if the router is not up yet), then heartbeats every
  `INTELLIROUTE_PROVIDER_HEARTBEAT_INTERVAL_SECONDS` (default 8 s) so
  its `INTELLIROUTE_PROVIDER_LEASE_TTL_SECONDS` lease (default 30 s)
  stays valid.

### 4.7 Eval Harness (`intelliroute/eval_harness/` + `scripts/replay_eval.py`)

- Generates deterministic JSONL workloads for four scenarios
  (`normal_mixed`, `degraded_provider`, `budget_pressure`,
  `overload_brownout`).
- Replays them against `/v1/complete` while sweeping the router's
  routing mode through five policies (`intelliroute`, `round_robin`,
  `cheapest_first`, `latency_first`, `premium_first`).
- Resets mutable state on every service before each `(scenario, policy)`
  run so cross-run comparisons are fair (router runtime state, cost
  rollups, breakers, all 3 limiter replicas, and mock fault flags).
- Writes per-run and matrix-aggregate artifacts to `artifacts/` plus a
  quick-look `eval_results/results.jsonl` and `eval_results/summary.csv`.
- Full schema, scenario semantics, and reproducibility recipe are in
  [`docs/REPLAY_EVAL_HARNESS.md`](REPLAY_EVAL_HARNESS.md).

### 4.8 Frontend (`frontend/index.html`)

Static single-page dashboard served by `python -m http.server` on
`:3000` (launched as part of `scripts/start_stack.py`). Uses CORS to
talk to the gateway directly. Surfaces:

- Live chat panel (drives `/v1/complete`)
- Provider health + circuit-breaker states
- Routing visualization (intent → ranking → chosen provider)
- Leader-election state across all 3 limiter replicas
- Cost rollups + budget alerts
- Queue depth and brownout status
- Last 50 traces (ring buffer fed by `/traces`)
- User-feedback flow (rate completions; trigger AI analysis)

## 5. Consistency Model

| Data path                       | Consistency             | Rationale                                                            |
|---------------------------------|-------------------------|----------------------------------------------------------------------|
| Rate-limit checks               | Strong (single leader)  | Must not over-issue tokens; opt-in fail-closed for stricter mode     |
| Replication log replay          | Eventual                | Followers tail the log so they can serve as a hot leader candidate   |
| Cost rollups                    | Eventual                | Fire-and-forget cost events; small lag is acceptable                 |
| Circuit breaker                 | Per-instance            | Each health-monitor process maintains its own breaker state          |
| Registry (within a router)      | Strong                  | In-process dict guarded by a lock                                    |
| Registry (across routers)       | Per-router              | No cross-replica sync — heartbeats refresh local TTLs                |
| Brownout state                  | Per-router              | Each router process tracks its own queue/p95/error/timeout window    |
| Online tuner weights            | Per-router              | Observed locally; not currently shared between router replicas       |

## 6. Failure Modes and Recovery

1. **Provider failure.** Circuit breaker trips after `failure_threshold`
   consecutive failures. Router fallback loop skips OPEN breakers and
   tries the next-best provider. Probing happens via HALF_OPEN advance
   on `/snapshot` reads.
2. **Provider rate-limit (429).** Router records the failure, applies
   SLA-aware jittered exponential back-off bounded by the provider's
   declared p95 SLA, and either retries the same provider (if its retry
   budget isn't exhausted) or falls back.
3. **Provider timeout.** Per-provider timeout derived from the
   provider's declared p95 SLA. The router's `httpx` client raises
   before the mock's 300 s sleep returns, exercising the `timeout`
   retry path.
4. **Malformed upstream response.** Provider client fails to parse;
   the response feeds into the feedback collector's hallucination
   signal so a flapping provider's quality score drops.
5. **Daily quota exhausted.** Provider is skipped in the fallback chain
   for the rest of the UTC day. If *all* candidates are exhausted, the
   router returns `429 daily_quota_exhausted`.
6. **Tenant/team/workflow budget pressure.** Pre-call budget gate
   demotes the head candidate to the cheapest still-pending provider
   when projected cost would push past any active budget.
7. **Rate-limit exhaustion.** Router treats a follower's `denied`
   response as a fallback signal and tries the next candidate.
8. **Rate-limiter leader loss.** Surviving replicas detect leader
   timeout, run a bully-style election, the new leader takes over the
   replication log, and followers redirect their forwards.
9. **Cost tracker down.** Router's fire-and-forget publish silently
   fails; routing is unaffected. Recent rollups will be slightly stale
   until traffic resumes.
10. **All providers down.** Router returns a structured 503 to the
    client rather than hanging.
11. **Sustained overload.** Brownout manager flips the system into
    degraded mode: drops or delays LOW-priority traffic, blocks
    premium providers for MEDIUM/LOW, prefers low-latency providers,
    clamps `max_tokens`. Recovers automatically once metrics fall back
    below exit thresholds.

## 7. Future Work

- A formally verified Raft implementation in place of the current
  bully-style election + replication log.
- Persistent state (Redis / Postgres) for buckets, breakers, and the
  registry so restarts don't lose runtime data. (User feedback already
  persists to SQLite at `artifacts/user_feedback.sqlite3`.)
- A learned intent classifier (small fine-tuned model) replacing the
  current deterministic heuristics — useful where the keyword-based
  classifier is ambiguous.
- More provider adapters beyond the Groq + Gemini clients already in
  `provider_clients.py` (OpenAI, Anthropic, local vLLM).
- Cross-router sharing of online weight-tuner state so the policy
  converges faster in multi-router deployments.
