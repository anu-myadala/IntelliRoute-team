# IntelliRoute

**A distributed control plane for multi-LLM orchestration.**

CMPE 273 — Enterprise Distributed Systems, Spring 2026, San José State University.
**Team:** Anukrithi Myadala, Larry Nguyen, James Pham, Surbhi Singh.

IntelliRoute sits between client applications and LLM providers (OpenAI, Anthropic, Groq, Gemini, local open-source models) and turns them into a single, governed compute resource. It classifies each request by intent, picks the best provider with a multi-objective scoring policy, enforces distributed rate limits via a leader-elected token-bucket cluster, fails over automatically when providers degrade, tracks cost per tenant / team / workflow, and exposes the internal state needed for live observability via a web dashboard.

---

## For the professor — the 60-second version

- **What it is:** a Python/FastAPI microservices system (gateway, router, rate limiter, cost tracker, health monitor, mock providers) that demonstrates the major distributed-systems concepts from CMPE 273 in a single coherent project.
- **How to run it:** create a venv, `pip install -e .[dev]`, then `PYTHONPATH=. python3 scripts/start_stack.py`. Open `http://127.0.0.1:3000` for the live demo dashboard, or call `http://127.0.0.1:8000` directly with the demo API key. Full step-by-step in [Quick start](#quick-start).
- **How to verify it works:** `PYTHONPATH=. python3 -m pytest tests/` runs the **265-test** suite (252 unit + 13 end-to-end integration tests that spawn the full stack on ephemeral ports). Should finish in ~12 s on a laptop.
- **Where the distributed-systems concepts live:** see the [concept map](#distributed-systems-concepts-demonstrated) — it's a per-topic table that points to the exact module that implements each concept.
- **Demo-able failure modes:** force-fail a provider, exhaust a token bucket, kill a rate-limiter replica, drive the system into brownout — all live, all visible from the dashboard. See [Live demo walkthrough](#live-demo-walkthrough).
- **Architecture deep dive:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Replay-harness usage: [`docs/REPLAY_EVAL_HARNESS.md`](docs/REPLAY_EVAL_HARNESS.md).

---

## Table of contents

1. [What it does](#what-it-does)
2. [Architecture](#architecture)
3. [Distributed-systems concepts demonstrated](#distributed-systems-concepts-demonstrated)
4. [Quick start](#quick-start)
5. [Live demo walkthrough](#live-demo-walkthrough)
6. [Project layout](#project-layout)
7. [API surface](#api-surface)
8. [Configuration](#configuration)
9. [Team contributions](#team-contributions)
10. [What is intentionally simplified](#what-is-intentionally-simplified)

---

## What it does

1. **Intent-aware routing.** Each request is classified into one of `interactive`, `reasoning`, `batch`, or `code`. The routing policy picks the best provider for that intent.
2. **Multi-objective scoring.** Providers are ranked by a weighted combination of latency, cost, capability, and historical success rate. Weights vary per intent and are auto-tuned online by an EMA-driven `WeightTuner`.
3. **Distributed rate limiting with leader election.** Three token-bucket replicas run on `:8002`, `:8012`, `:8022`. They elect a leader using a bully-style protocol; followers forward `/check` calls to the leader and tail its replication log so state converges.
4. **Adaptive fallback + circuit breakers.** A three-state breaker (`closed → open → half_open`) per provider trips after consecutive failures; the router skips tripped or rate-limited providers and tries the next-best one. SLA-aware retries with jittered exponential back-off.
5. **Brownout / load shedding.** Under sustained queue depth, p95 latency, error rate, or timeout rate breaches, the router enters brownout and degrades gracefully (drops or delays low-priority traffic, blocks premium providers, clamps `max_tokens`). Per-tenant brownout state on top of global state.
6. **Priority queue + workers.** Four async worker tasks drain a priority queue. HIGH (interactive, code) bypasses the queue; MEDIUM (reasoning) and LOW (batch) go through. Load shedding when depth exceeds configured threshold.
7. **Cost-aware accounting.** Cost events are published asynchronously to a tracker that maintains tenant / team / workflow rollups, headroom checks, and budget-exceeded alerts. Pre-call budget gate demotes to a cheaper provider when the projected cost would push over budget.
8. **Per-provider daily quotas.** Opt-in UTC daily caps on successful completions per provider; quota-exhausted providers are skipped in the fallback chain.
9. **Dynamic service discovery.** Providers can self-register with a TTL lease and refresh via heartbeats. The router tracks active vs. stale providers and exposes both via `/providers` and `/providers/registry`.
10. **User feedback loop.** Every completion is persisted to a SQLite store (`artifacts/user_feedback.sqlite3`) along with a prompt/response preview. The dashboard lets users rate completions and triggers an LLM-powered analysis of recent feedback (sample-stratified, cached by revision).
11. **Observability dashboard.** A static HTML/JS frontend on `:3000` shows live provider health, routing visualization, leader-election state, cost rollups, queue depth, brownout status, and a 50-trace ring buffer of recent requests.

## Architecture

```
                ┌──────────────────────────────────────────────────────┐
                │                                                      │
   client ──▶  Gateway  ──HTTP─▶  Router  ──HTTP─▶  Mock Providers
   (X-API-Key)  :8000             :8001               :9001 mock-fast
                                    │                 :9002 mock-smart
                                    │                 :9003 mock-cheap
                                    │              + optional Groq / Gemini
                                    │
                                    ├──HTTP─▶  Rate Limiter cluster
                                    │              :8002 rl-0
                                    │              :8012 rl-1   (leader-elected)
                                    │              :8022 rl-2
                                    │
                                    ├──HTTP─▶  Health Monitor :8004
                                    │              (circuit breakers + polling)
                                    │
                                    └──async─▶  Cost Tracker  :8003
                                                 (tenant / team / workflow rollups + alerts)

   Dashboard (static)    http://127.0.0.1:3000   (talks to gateway over CORS)
```

Eleven processes total: 5 platform services (gateway, router, cost tracker, health monitor) + 3 rate-limiter replicas + 3 mock providers + 1 static HTTP server for the frontend.

| Service        | Port(s)             | Responsibility                                                        |
|----------------|---------------------|-----------------------------------------------------------------------|
| Gateway        | 8000                | Public entry point, API-key auth, request tracing                     |
| Router         | 8001                | Intent classification, multi-objective ranking, fallback, queue, brownout |
| Rate Limiter   | 8002 / 8012 / 8022  | Distributed token-bucket; leader-elected; replication log             |
| Cost Tracker   | 8003                | Async cost events; tenant/team/workflow rollups; budget alerts        |
| Health Monitor | 8004                | Per-provider circuit breakers; periodic liveness polling              |
| Mock Providers | 9001 / 9002 / 9003  | Three simulated upstream LLMs; configurable latency/cost/failure      |
| Frontend       | 3000                | Static HTML/JS dashboard served via `python -m http.server`           |

## Distributed-systems concepts demonstrated

| Course topic                       | Where it lives in IntelliRoute                                                                           |
|------------------------------------|----------------------------------------------------------------------------------------------------------|
| Service discovery / naming         | `intelliroute/router/registry.py` — bootstrap + dynamic leases; in-memory analogue of Consul/etcd        |
| Lease-based liveness               | Mock providers self-register and call `POST /providers/heartbeat`; expired leases mark providers stale   |
| Communication: sync + async        | HTTP between gateway/router/limiter; fire-and-forget async cost events to the cost tracker               |
| Coordination & leader election     | `intelliroute/rate_limiter/election.py` — bully-style election across 3 replicas; followers forward writes |
| Replication & consistency          | Strong consistency for rate-limit decisions (single leader; opt-in fail-closed mode); eventual via replication log |
| Fault tolerance                    | `intelliroute/health_monitor/circuit_breaker.py` (3-state breaker) + router fallback chain               |
| Adaptive retry / back-off          | SLA-aware jittered exponential back-off in `intelliroute/router/main.py::_error_backoff_ms`              |
| Backpressure & queueing            | `intelliroute/router/queue.py` priority queue + 4 worker tasks; load shedding above configured depth     |
| Graceful degradation / brownout    | `intelliroute/router/brownout.py` — global + per-tenant brownout based on queue, p95, error/timeout rate |
| Multi-objective optimisation       | `intelliroute/router/policy.py` + `policy_engine/` — latency × cost × capability × success scoring       |
| Online learning                    | `intelliroute/router/weight_tuner.py` rebalances per-intent weights from observed sub-score outcomes     |
| Budget governance                  | `intelliroute/cost_tracker/accounting.py` — tenant / team / workflow budgets, premium caps, alerts       |
| Security                           | API-key auth at the gateway; tenant identity rewritten from the authenticated principal, not the body    |
| Observability                      | `intelliroute/common/logging.py` JSON logger; `/snapshot`, `/stats`, `/traces` endpoints; X-Request-Id   |

---

## Quick start

Tested on macOS and Linux with **Python 3.10+**. Need ports `8000-8004`, `8012`, `8022`, `9001-9003`, and `3000` free on localhost.

### 1. Install

```bash
git clone <repo-url> IntelliRoute-team
cd IntelliRoute-team
python3 -m venv .venv
source .venv/bin/activate                # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e ".[dev]"
```

The package metadata (`pyproject.toml`) declares all runtime + dev dependencies: `fastapi`, `uvicorn`, `httpx`, `pydantic`, `pytest`, `pytest-asyncio`. No databases, no compiled extensions, no Docker required.

### 2. Run the test suite

```bash
PYTHONPATH=. python3 -m pytest tests/
```

Expected: **`265 passed in ~12s`**. The suite includes 13 integration tests in [`tests/test_integration.py`](tests/test_integration.py) that spawn the full 10-process backend on ephemeral ports and exercise it over real HTTP — they're the single fastest way to confirm a fresh clone is healthy.

To skip the integration tests (faster iteration, no port allocation):

```bash
PYTHONPATH=. python3 -m pytest tests/ --ignore=tests/test_integration.py
```

### 3. Launch the stack

```bash
PYTHONPATH=. python3 scripts/start_stack.py
```

You will see ten "starting …" lines (3 mocks, 3 rate-limiter replicas, cost tracker, health monitor, router, gateway) plus a frontend line, ending with:

```
IntelliRoute stack running.
  Providers:  mock demo provider processes + router bootstrap (see INTELLIROUTE_PROVIDER_MODE)
  Gateway:    http://127.0.0.1:8000
  Frontend:   http://127.0.0.1:3000
  Press Ctrl-C to stop.
```

Open **http://127.0.0.1:3000** in any modern browser for the live dashboard (chat panel, provider health, routing visualization, leader election, cost tracker, queue/brownout state, traces, and feedback analytics).

### 4. (Optional) Use real providers

By default the stack uses the three mock providers — perfect for the classroom demo and for grading, since it has no rate limits or API costs.

To call live models, drop a `.env` file in the repo root with one or both of:

```env
GROQ_API_KEY=...
GEMINI_API_KEY=...
```

The router will register the corresponding provider(s) at startup. Set `INTELLIROUTE_USE_MOCKS=1` (or `INTELLIROUTE_PROVIDER_MODE=mock_only`) to force mock-only routing even if keys are present — useful for offline testing.

### 5. Stop the stack

`Ctrl-C` in the terminal running `start_stack.py`. The launcher cleanly signals every child process and waits up to 5 s before killing.

### Troubleshooting

| Symptom                                              | Fix                                                                                       |
|------------------------------------------------------|-------------------------------------------------------------------------------------------|
| `ModuleNotFoundError: intelliroute`                  | Prefix the command with `PYTHONPATH=.` and run from the repo root                         |
| `Address already in use` when starting               | Something else is on 8000-8004, 8012, 8022, 9001-9003, or 3000. Free it (`lsof -nP -iTCP:8000`) or override with the env vars in [Configuration](#configuration) |
| `Errno 48` on the frontend line                      | Port 3000 is busy. Run `INTELLIROUTE_FRONTEND_PORT=3005 PYTHONPATH=. python3 scripts/start_stack.py` |
| Integration tests hang                               | Stale subprocesses from a previous run: `pkill -f "uvicorn intelliroute"`                 |
| Tests pass but the stack won't start                 | Almost always a port collision: `lsof -nP -iTCP:8000-8004,8012,8022,9001-9003,3000`        |

---

## Live demo walkthrough

With the stack running, exercise each distributed-systems concept from a second terminal. Everything below is also visible on the dashboard.

The default demo API key is `demo-key-123` (mapped to `demo-tenant`).

**(a) End-to-end completions across all four intents**

```bash
PYTHONPATH=. python3 scripts/demo.py
```

Sends one request per intent (interactive / reasoning / batch / code), prints which provider was chosen and the latency/cost, then prints the cost summary, feedback EMA snapshot, queue stats, and leader-election state.

**(b) Routing introspection — see the ranked decision without executing**

```bash
curl -s -X POST http://127.0.0.1:8001/decide \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"demo-tenant","messages":[{"role":"user","content":"Explain step by step the CAP theorem and analyze the tradeoffs."}]}' \
  | python3 -m json.tool
```

Returns the classified intent, ranked provider list, sub-scores, matched policy rules, and brownout state.

**(c) Automatic failover via the circuit breaker**

```bash
# Force mock-fast to fail every request
curl -s -X POST http://127.0.0.1:9001/admin/force_fail \
  -H "Content-Type: application/json" -d '{"fail": true}'

# Send an interactive request — the router should fall back
curl -s -X POST http://127.0.0.1:8000/v1/complete \
  -H "X-API-Key: demo-key-123" -H "Content-Type: application/json" \
  -d '{"tenant_id":"x","messages":[{"role":"user","content":"hi"}]}' | python3 -m json.tool

# Check breaker state — after a few failures the breaker should be 'open'
curl -s http://127.0.0.1:8004/snapshot | python3 -m json.tool

# Restore
curl -s -X POST http://127.0.0.1:9001/admin/force_fail -d '{"fail": false}'
```

There's also a scripted version: `PYTHONPATH=. python3 scripts/demo_failure_recovery.py`. It walks the breaker through `closed → open → half_open → closed` and prints the snapshot at each step.

The mock providers also expose `/admin/force_timeout`, `/admin/force_rate_limit`, and `/admin/force_malformed` for the other failure modes.

**(d) Distributed rate limiting at runtime**

```bash
# Shrink (demo-tenant, mock-cheap) to 1 token, glacial refill
curl -s -X POST http://127.0.0.1:8002/config \
  -H "Content-Type: application/json" \
  -d '{"key":"demo-tenant|mock-cheap","capacity":1,"refill_rate":0.01}'

# First batch request — uses the single token, routes to mock-cheap
curl -s -X POST http://127.0.0.1:8000/v1/complete \
  -H "X-API-Key: demo-key-123" -H "Content-Type: application/json" \
  -d '{"tenant_id":"x","messages":[{"role":"user","content":"Summarize the following document into bullet points"}]}' \
  | python3 -m json.tool

# Second request — bucket is empty, router falls back, "fallback_used": true
```

**(e) Leader election — kill the leader and watch failover**

```bash
# See current state across all 3 replicas
for p in 8002 8012 8022; do curl -s http://127.0.0.1:$p/election/status; echo; done

# Find which replica is the leader, then kill it (replace <PID>)
lsof -nP -iTCP:8002 -sTCP:LISTEN
kill <PID>

# Within a few seconds the survivors elect a new leader
for p in 8012 8022; do curl -s http://127.0.0.1:$p/election/status; echo; done
```

**(f) Cost rollups and budget alerts**

```bash
# Per-tenant rollup
curl -s http://127.0.0.1:8000/v1/cost/summary -H "X-API-Key: demo-key-123" | python3 -m json.tool

# Set a $0.001 budget for demo-tenant and watch the alert fire
curl -s -X POST http://127.0.0.1:8003/budget \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"demo-tenant","budget_usd":0.001}'

# Drive a few requests, then:
curl -s http://127.0.0.1:8003/alerts | python3 -m json.tool
```

**(g) Replay-based scenario evaluation** *(advanced — for the report)*

```bash
PYTHONPATH=. python3 scripts/replay_eval.py --scenario all --policy all --size 100 --seed-count 3
```

Replays a deterministic workload across `intelliroute`, `round_robin`, `cheapest_first`, `latency_first`, and `premium_first` policies under four scenarios (`normal_mixed`, `degraded_provider`, `budget_pressure`, `overload_brownout`) and writes per-run + aggregate artifacts to `artifacts/`. See [`docs/REPLAY_EVAL_HARNESS.md`](docs/REPLAY_EVAL_HARNESS.md).

---

## Project layout

```
IntelliRoute-team/
├── README.md                          <-- you are here
├── CHANGELOG.md
├── pyproject.toml                     <-- package metadata & pytest config
├── requirements.txt                   <-- pip-style mirror of dependencies
├── docs/
│   ├── ARCHITECTURE.md                <-- service-by-service architecture deep dive
│   ├── REPLAY_EVAL_HARNESS.md         <-- evaluation harness usage
│   ├── PROVIDER_QUALITY_SCORE.md
│   └── TEAM_WORKFLOW_BUDGETS.md
├── intelliroute/                      <-- Python source package
│   ├── common/
│   │   ├── config.py                  <-- env-driven Settings dataclass
│   │   ├── env.py                     <-- minimal .env loader
│   │   ├── logging.py                 <-- JSON structured logger
│   │   ├── models.py                  <-- shared Pydantic over-the-wire types
│   │   ├── mock_provider_catalog.py   <-- shared mock ProviderInfo definitions
│   │   └── provider_mode.py           <-- mock_only / external_only / hybrid / auto
│   ├── gateway/main.py                <-- public API (port 8000)
│   ├── router/
│   │   ├── main.py                    <-- routing pipeline + queue workers
│   │   ├── intent.py                  <-- deterministic intent classifier
│   │   ├── policy.py                  <-- multi-objective ranking
│   │   ├── policy_engine/             <-- declarative pre-rank policy rules
│   │   ├── registry.py                <-- service registry (bootstrap + leases)
│   │   ├── feedback.py                <-- per-provider EMA feedback collector
│   │   ├── weight_tuner.py            <-- online weight tuner
│   │   ├── queue.py                   <-- priority queue + load shedding
│   │   ├── brownout.py                <-- graceful-degradation manager
│   │   ├── provider_clients.py        <-- mock / Groq / Gemini adapters
│   │   ├── provider_daily_quota.py    <-- opt-in per-provider UTC daily caps
│   │   └── user_feedback_store.py     <-- SQLite-backed user feedback + analysis
│   ├── rate_limiter/
│   │   ├── main.py                    <-- 3-replica HTTP service (port 8002/8012/8022)
│   │   ├── token_bucket.py            <-- TokenBucket + replication log
│   │   └── election.py                <-- bully-style leader election
│   ├── cost_tracker/
│   │   ├── main.py                    <-- port 8003
│   │   └── accounting.py              <-- tenant/team/workflow rollups + budgets
│   ├── health_monitor/
│   │   ├── main.py                    <-- port 8004
│   │   └── circuit_breaker.py         <-- 3-state breaker
│   ├── mock_provider/main.py          <-- ports 9001-9003; admin/force_* hooks
│   └── eval_harness/                  <-- workload generator + replay runner
├── frontend/
│   ├── index.html                     <-- live dashboard (chat, health, traces, …)
│   └── demo/
├── scripts/
│   ├── start_stack.py                 <-- launch the full local stack
│   ├── start_stack_cloud.py           <-- Render / Railway / Fly.io launcher
│   ├── demo.py                        <-- end-to-end CLI demo
│   ├── demo_failure_recovery.py       <-- breaker close→open→half_open→close walk
│   ├── replay_eval.py                 <-- scenario × policy replay harness
│   ├── generate_workload.py           <-- emit a JSONL workload deterministically
│   ├── run_all.py                     <-- single-process variant (no mock subprocs)
│   └── apply_model_logging_patch.py
├── tests/                             <-- 265 tests (252 unit + 13 integration)
└── artifacts/                         <-- runtime SQLite + replay artifacts
```

## API surface

Below is the trimmed list — every service also exposes `GET /health` (liveness), `GET /stats` (per-replica counters), and `POST /reset` (clear in-memory state for test isolation).

### Gateway (`:8000`) — the only service clients should talk to

| Method | Path                        | Description                                  |
|--------|-----------------------------|----------------------------------------------|
| POST   | `/v1/complete`              | Submit an LLM completion (X-API-Key header)  |
| GET    | `/v1/cost/summary`          | Per-tenant cost rollup                       |
| GET    | `/v1/system/health`         | Aggregate provider health snapshot           |
| POST   | `/v1/feedback`              | Submit a thumbs-up/down on a completion      |
| GET    | `/v1/feedback/recent`       | Recent feedback rows for the tenant          |
| GET    | `/v1/feedback/summary`      | Tenant feedback aggregate                    |
| POST   | `/v1/feedback/analyze`      | LLM-assisted analysis of recent feedback     |

### Router (`:8001`)

| Method | Path                          | Description                                                 |
|--------|-------------------------------|-------------------------------------------------------------|
| POST   | `/complete`                   | Internal: full routing + fallback execution                 |
| POST   | `/decide`                     | Introspection: returns the routing decision without executing |
| GET    | `/providers`                  | Currently routable providers (active leases only)           |
| POST   | `/providers`                  | Bootstrap-style provider registration                       |
| POST   | `/providers/register`         | Lease-based dynamic registration                            |
| POST   | `/providers/heartbeat`        | Refresh a provider's lease                                  |
| GET    | `/providers/registry`         | Full registry incl. stale entries (debug/observability)     |
| DELETE | `/providers/{name}`           | Deregister a provider                                       |
| GET    | `/feedback`                   | Per-provider EMA metrics (latency, success, anomaly, quality) |
| GET    | `/queue/stats`                | Priority queue depth, shed count, timeouts                  |
| GET    | `/brownout`                   | Global brownout state                                       |
| GET    | `/brownout/{tenant_id}`       | Per-tenant brownout state                                   |
| GET    | `/brownout/metrics`           | Global + per-tenant brownout metrics                        |
| GET    | `/traces`                     | Last 50 completed request traces (ring buffer)              |
| GET    | `/weights`                    | Current per-intent weights + tuner sample counts            |
| POST   | `/weights/rebalance/{intent}` | Manually trigger a tuner rebalance                          |
| GET    | `/routing/mode`               | Active routing policy                                       |
| POST   | `/routing/mode`               | Switch routing policy (`intelliroute`, `round_robin`, …)    |

### Rate Limiter (`:8002` / `:8012` / `:8022`)

| Method | Path                                | Description                                                |
|--------|-------------------------------------|------------------------------------------------------------|
| POST   | `/check`                            | Try to consume tokens for `(tenant, provider)`             |
| POST   | `/config` / `/config/tenant` / `/config/provider` / `/config/tenant-provider` | Update bucket capacity & refill at any granularity |
| GET    | `/config/resolve/{tenant}/{provider}` | Show which bucket config a key resolves to + source level |
| GET    | `/leader`                           | Current leader (from store)                                |
| GET    | `/election/status`                  | This replica's election state                              |
| POST   | `/election/{challenge,victory,heartbeat}` | Bully-protocol message handlers                       |
| GET    | `/log` / `/log/since/{offset}`      | Replication log (full / incremental for follower replay)   |

### Cost Tracker (`:8003`)

| Method | Path                            | Description                                                  |
|--------|---------------------------------|--------------------------------------------------------------|
| POST   | `/events`                       | Publish a cost event (fire-and-forget from the router)       |
| GET    | `/summary/{tenant_id}`          | Per-tenant rollup                                            |
| GET    | `/summary/team/{team_id}` / `/summary/workflow/{workflow_id}` | Team / workflow rollups          |
| POST   | `/budget` / `/budget/team` / `/budget/workflow` | Set tenant / team / workflow budgets         |
| POST   | `/budget/team/premium-cap`      | Cap a team's spend on premium-tier providers                 |
| GET    | `/budget/{tenant_id}/headroom`  | Remaining budget                                             |
| GET    | `/budget/{tenant_id}/check?projected_cost_usd=…` | Pre-call budget check                       |
| GET    | `/alerts`                       | Budget-exceeded alerts                                       |
| GET    | `/history/{tenant_id}`          | Paginated raw cost-event history                             |

### Health Monitor (`:8004`)

| Method | Path                  | Description                                  |
|--------|-----------------------|----------------------------------------------|
| POST   | `/register`           | Register a provider URL for periodic polling |
| POST   | `/report/{provider}`  | Router pushes success/failure outcomes       |
| GET    | `/snapshot`           | All breaker states (also advances OPEN→HALF_OPEN cooldowns) |
| GET    | `/snapshot/{provider}`| Per-provider breaker state                   |

### Mock Providers (`:9001` / `:9002` / `:9003`)

| Method | Path                       | Description                                            |
|--------|----------------------------|--------------------------------------------------------|
| POST   | `/v1/chat`                 | Simulated LLM completion                               |
| POST   | `/admin/force_fail`        | Return HTTP 503 on every request                       |
| POST   | `/admin/force_timeout`     | Hang every request (exercises router timeout handling) |
| POST   | `/admin/force_rate_limit`  | Return HTTP 429 with `Retry-After`                     |
| POST   | `/admin/force_malformed`   | Return HTTP 200 with broken JSON                       |
| POST   | `/admin/reset`             | Clear all fault-injection flags                        |
| GET    | `/admin/state`             | Inspect current fault flags                            |

## Configuration

Every service reads its configuration from environment variables; defaults live in [`intelliroute/common/config.py`](intelliroute/common/config.py). The most useful ones:

| Variable                                              | Default                              | Purpose                                 |
|-------------------------------------------------------|--------------------------------------|-----------------------------------------|
| `INTELLIROUTE_HOST`                                   | `127.0.0.1`                          | Bind host                               |
| `INTELLIROUTE_GATEWAY_PORT`                           | `8000`                               | Gateway port                            |
| `INTELLIROUTE_ROUTER_PORT`                            | `8001`                               | Router port                             |
| `INTELLIROUTE_RATE_LIMITER_PORT[_1,_2]`               | `8002`, `8012`, `8022`               | Three rate-limiter replicas             |
| `INTELLIROUTE_COST_TRACKER_PORT`                      | `8003`                               |                                         |
| `INTELLIROUTE_HEALTH_MONITOR_PORT`                    | `8004`                               |                                         |
| `INTELLIROUTE_MOCK_FAST_PORT` / `_SMART_PORT` / `_CHEAP_PORT` | `9001` / `9002` / `9003`     | Mock provider ports                     |
| `INTELLIROUTE_FRONTEND_PORT`                          | `3000`                               | Static dashboard                        |
| `INTELLIROUTE_DEMO_KEY`                               | `demo-key-123`                       | Demo API key → `demo-tenant`            |
| `INTELLIROUTE_PROVIDER_MODE`                          | `auto`                               | `auto` / `mock_only` / `external_only` / `hybrid` |
| `INTELLIROUTE_USE_MOCKS`                              | `0`                                  | If `1`, force mock-only routing         |
| `GROQ_API_KEY` / `GEMINI_API_KEY`                     | *(empty)*                            | Live provider keys (in `.env` or shell) |
| `INTELLIROUTE_GROQ_MODEL` / `INTELLIROUTE_GEMINI_MODEL` | `llama-3.3-70b-versatile` / `gemini-2.5-flash` | Per-provider model id     |
| `INTELLIROUTE_PROVIDER_TIMEOUT_S`                     | `30`                                 | Per-call upstream timeout (seconds)     |
| `INTELLIROUTE_MOCK_REGISTRATION`                      | `hybrid`                             | `legacy` / `hybrid` / `dynamic` — controls mock self-registration |
| `INTELLIROUTE_PROVIDER_LEASE_TTL_SECONDS`             | `30`                                 | Dynamic-registration lease TTL          |
| `INTELLIROUTE_PROVIDER_HEARTBEAT_INTERVAL_SECONDS`    | `8`                                  | Mock heartbeat interval                 |
| `INTELLIROUTE_ROUTING_MODE`                           | `intelliroute`                       | Initial routing mode                    |
| `INTELLIROUTE_ENABLE_PROVIDER_DAILY_QUOTAS`           | `0`                                  | Turn on per-provider UTC daily caps     |
| `RATE_LIMITER_REPLICA_ID` / `RATE_LIMITER_PEERS`      | `rl-0` / 3-peer cluster              | Per-replica election identity           |
| `RATE_LIMITER_STRONG_CONSISTENCY`                     | `0`                                  | If `1`, followers fail closed when forwarding fails |
| `INTELLIROUTE_USER_FEEDBACK_DB_PATH`                  | `artifacts/user_feedback.sqlite3`    | SQLite path for user feedback           |

`scripts/start_stack.py` sets the most important variables automatically — including the per-mock public ports needed for hybrid registration and the 3-peer cluster topology for the rate limiter.

### Safe local testing (no Gemini / Groq quota burned)

To run the stack **without registering or calling** Gemini or Groq even if keys exist in `.env`:

```bash
INTELLIROUTE_USE_MOCKS=1 PYTHONPATH=. python3 scripts/start_stack.py
```

`INTELLIROUTE_USE_MOCKS=1` forces effective `mock_only` mode (`tests/test_provider_mode.py` covers the matrix). With it set, only `mock-fast`, `mock-smart`, and `mock-cheap` appear in `GET http://127.0.0.1:8001/providers`; no traffic ever leaves localhost.

---

## Team contributions

Work was divided across four subsystem owners. Everyone reviewed each other's pull requests.

### Anukrithi Myadala — Gateway, shared infrastructure, and the dashboard frontend

- Designed the shared Pydantic data model in [`intelliroute/common/models.py`](intelliroute/common/models.py) so every service speaks the same wire format.
- Implemented [`intelliroute/common/config.py`](intelliroute/common/config.py) (env-driven settings) and [`intelliroute/common/env.py`](intelliroute/common/env.py) (minimal `.env` loader).
- Built the structured JSON logger in [`intelliroute/common/logging.py`](intelliroute/common/logging.py) used by every service.
- Wrote the API gateway ([`intelliroute/gateway/main.py`](intelliroute/gateway/main.py)): API-key auth, tenant identity rewriting, `X-Request-Id` trace propagation, cost-summary / system-health / feedback passthroughs, CORS for the dashboard.
- Owned the live dashboard ([`frontend/index.html`](frontend/index.html)): chat panel, provider health, routing visualization, cost rollups, leader-election state, queue/brownout, and the user-feedback / AI-analysis flow.
- Wrote [`scripts/start_stack.py`](scripts/start_stack.py) and [`scripts/demo.py`](scripts/demo.py) and authored this README's quick-start walkthrough.

### Larry Nguyen — Router, routing policy, intent classification, and online weight tuning

- Implemented the intent classifier in [`intelliroute/router/intent.py`](intelliroute/router/intent.py).
- Built the multi-objective routing policy in [`intelliroute/router/policy.py`](intelliroute/router/policy.py) and the declarative policy engine in [`intelliroute/router/policy_engine/`](intelliroute/router/policy_engine/).
- Implemented the provider registry in [`intelliroute/router/registry.py`](intelliroute/router/registry.py) (bootstrap + dynamic leases).
- Wrote the router service itself ([`intelliroute/router/main.py`](intelliroute/router/main.py)): bootstrap, fallback loop, async health & cost reporting, request queue + worker model, brownout integration, retry / SLA back-off.
- Added the online [`WeightTuner`](intelliroute/router/weight_tuner.py) and [`provider_clients.py`](intelliroute/router/provider_clients.py) (mock / Groq / Gemini adapters).
- Authored [`tests/test_intent.py`](tests/test_intent.py), [`tests/test_policy.py`](tests/test_policy.py), [`tests/test_policy_engine.py`](tests/test_policy_engine.py), [`tests/test_registry.py`](tests/test_registry.py), [`tests/test_weight_tuner.py`](tests/test_weight_tuner.py), [`tests/test_router_routing_mode.py`](tests/test_router_routing_mode.py).

### James Pham — Rate limiter cluster, leader election, circuit breakers, and the eval harness

- Implemented the core `TokenBucket` and `RateLimiterStore` in [`intelliroute/rate_limiter/token_bucket.py`](intelliroute/rate_limiter/token_bucket.py) including the replication log used by follower replicas.
- Wrote the bully-style leader election in [`intelliroute/rate_limiter/election.py`](intelliroute/rate_limiter/election.py) and the rate-limiter HTTP service in [`intelliroute/rate_limiter/main.py`](intelliroute/rate_limiter/main.py): `/check` forwarding, `/log/since/{offset}` incremental sync, `/election/*` handlers, opt-in fail-closed mode.
- Implemented the three-state circuit breaker in [`intelliroute/health_monitor/circuit_breaker.py`](intelliroute/health_monitor/circuit_breaker.py) and the health monitor service in [`intelliroute/health_monitor/main.py`](intelliroute/health_monitor/main.py) (snapshot-driven cooldown advancement, periodic liveness polling).
- Built the replay evaluation harness ([`intelliroute/eval_harness/`](intelliroute/eval_harness/), [`scripts/replay_eval.py`](scripts/replay_eval.py), [`scripts/generate_workload.py`](scripts/generate_workload.py)) and the cloud launcher ([`scripts/start_stack_cloud.py`](scripts/start_stack_cloud.py)).
- Authored [`tests/test_token_bucket.py`](tests/test_token_bucket.py), [`tests/test_circuit_breaker.py`](tests/test_circuit_breaker.py), [`tests/test_election.py`](tests/test_election.py), [`tests/test_eval_*.py`](tests/), [`tests/test_brownout.py`](tests/test_brownout.py).

### Surbhi Singh — Cost tracker, mock providers, observability, end-to-end testing

- Implemented [`CostAccountant`](intelliroute/cost_tracker/accounting.py): tenant / team / workflow rollups, headroom, premium caps, budget alerts.
- Wrote the cost tracker service ([`intelliroute/cost_tracker/main.py`](intelliroute/cost_tracker/main.py)): `/events`, `/summary/*`, `/budget/*`, `/alerts`, `/history`.
- Built the parametrised mock LLM provider ([`intelliroute/mock_provider/main.py`](intelliroute/mock_provider/main.py)) with configurable latency, jitter, failure rate, cost per 1K tokens, and the four `/admin/force_*` fault-injection hooks.
- Designed the user-feedback subsystem: [`intelliroute/router/user_feedback_store.py`](intelliroute/router/user_feedback_store.py) (SQLite + sample-stratified analysis cache) and the corresponding gateway / router endpoints.
- Authored [`tests/test_accounting.py`](tests/test_accounting.py), [`tests/test_models_budget_scopes.py`](tests/test_models_budget_scopes.py), [`tests/test_user_feedback_*.py`](tests/), [`tests/test_feedback*.py`](tests/), [`tests/test_router_user_feedback_api.py`](tests/test_router_user_feedback_api.py), [`tests/test_provider_*.py`](tests/), and the 13-test [`tests/test_integration.py`](tests/test_integration.py) end-to-end suite that spawns the full stack on ephemeral ports.
- Owned the failure-mode demo scripts ([`scripts/demo_failure_recovery.py`](scripts/demo_failure_recovery.py)) and the live-trace ring buffer + `/traces` endpoint surfaced in the dashboard.

### Joint contributions (all four)

- Architecture design, weekly integration reviews, code reviews on every PR.
- Final demo preparation and the in-class presentation.

---

## What is intentionally simplified

For the scope of a one-semester project we deliberately stop short of:

- **Production-grade Raft.** The rate limiter implements a working bully-style election, replication log, and follower forwarding — sufficient to demonstrate the consistency / availability tradeoff. A formally verified Raft implementation is the natural extension.
- **Persistent state.** All in-memory state (rate-limit buckets, breakers, registry, queue) resets on restart. Only user-feedback rows are persisted (SQLite at `artifacts/user_feedback.sqlite3`). Swapping in Redis or Postgres for the rest is a single-file change per subsystem.
- **A learned intent classifier.** The classifier is deterministic heuristics so it can be unit tested without an external model.
- **Production LLM adapters at scale.** We ship working Groq + Gemini adapters in [`provider_clients.py`](intelliroute/router/provider_clients.py), but for the demo we default to mock providers — they let us simulate latency, cost, and four distinct failure modes without burning real-provider quota.

These scoping decisions keep the focus on distributed-systems design and engineering tradeoffs, which is what the course is grading.
