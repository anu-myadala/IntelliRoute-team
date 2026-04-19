# IntelliRoute — Professor Q&A Bank

Use this to prep before the demo. Every answer references exact code so
you can pull it up if the professor asks to see it.

---

## 1. BIG-PICTURE / "WHY" QUESTIONS

### Q: Why does this project matter? What real problem does it solve?

Companies like Uber, Stripe, and Notion now use multiple LLM providers
(OpenAI, Anthropic, Google, local models). Each provider has different
pricing, latency, rate limits, and failure modes. Without a control
plane, every application team reinvents the same plumbing: retries,
fallback logic, cost tracking, rate limiting. IntelliRoute centralises
all of that into one system so application teams just send a request and
get the best available answer — the control plane handles the rest.

Think of it like a **load balancer**, but smarter: it doesn't just
round-robin — it classifies what kind of work the request is, scores
every provider on four axes, and picks the optimal one.

### Q: Why no database?

**Deliberate design choice, not laziness.** All state is in-memory
because:

1. **Latency**: A rate-limit check happens on every single request in
   the critical path. Hitting Redis or Postgres would add 1-5 ms per
   hop. Our in-memory check is < 0.01 ms.
2. **Simplicity**: For a control plane that manages *transient*
   operational state (token counts, circuit breaker windows, cost
   rollups), persistence isn't required. If the rate limiter restarts,
   buckets refill to full capacity — which is the *safe* default (you
   briefly over-serve rather than block everyone).
3. **Separation of concerns**: The system is designed so that swapping
   in Redis or Postgres is a **single-file change** — replace
   `RateLimiterStore` internals with a Redis client. The API surface
   stays identical. We chose to keep the focus on distributed-systems
   design rather than ORM boilerplate.
4. **Consistency model fits**: Rate limiting needs strong consistency
   (single leader), cost tracking is eventually consistent anyway
   (fire-and-forget). Neither requires durable storage for correctness
   at this scale.

**Follow-up if pushed**: "If we needed persistence — say, for billing
audits — we'd add a write-ahead log to the cost tracker. The
architecture already separates the event path (async) from the query
path, so adding a database behind `CostAccountant` is straightforward."

### Q: How is this different from just using nginx or HAProxy?

nginx/HAProxy do L4/L7 load balancing — round-robin, least-connections,
weighted. IntelliRoute does **intent-aware, multi-objective routing**:

- It classifies each request as interactive/reasoning/batch/code
- It scores providers on 4 axes with intent-specific weights
- It enforces per-tenant, per-provider rate limits
- It has circuit breakers that auto-trip and recover
- It tracks cost per tenant asynchronously
- It does adaptive fallback when providers degrade

nginx doesn't know what a "reasoning" request is or that it should
prefer the expensive-but-accurate model for it.

---

## 2. ARCHITECTURE QUESTIONS

### Q: Walk me through the architecture. What services exist and how do they communicate?

Six service types, all Python/FastAPI, communicating over HTTP:

```
Client → Gateway (:8000) → Router (:8001) → Mock Providers (:9001-9003)
                                ├──→ Rate Limiter (:8002)
                                ├──→ Health Monitor (:8004)
                                └──→ Cost Tracker (:8003)   [async]
```

| Service | Port | Role |
|---------|------|------|
| **Gateway** | 8000 | Public entry point, API-key auth, X-Request-Id tracing |
| **Router** | 8001 | Intent classification → policy ranking → fallback loop |
| **Rate Limiter** | 8002 (+8012, 8022) | Token-bucket per (tenant, provider), leader election |
| **Cost Tracker** | 8003 | Async cost event ingestion, per-tenant rollups, budget alerts |
| **Health Monitor** | 8004 | Circuit breakers, periodic health polling (every 5s) |
| **Mock Providers** | 9001-9003 | Simulated LLMs: fast, smart, cheap |

**Communication patterns**:
- **Synchronous (HTTP)**: Gateway → Router, Router → Rate Limiter,
  Router → Health Monitor, Router → Provider. These are in the critical
  path — the router needs answers before picking a provider.
- **Asynchronous (fire-and-forget)**: Router → Cost Tracker. Cost
  events are published after the response is sent. If the cost tracker
  is down, routing is unaffected.

*(See `intelliroute/router/main.py` lines 250-350 for the full flow.)*

### Q: What happens when a request comes in? Walk me through the complete request lifecycle.

1. **Gateway** (`gateway/main.py`):
   - Authenticates `X-API-Key` header → maps to `tenant_id`
     (e.g., `demo-key-123` → `demo-tenant`)
   - Overwrites `tenant_id` in the request body (security: never trust
     the client's self-reported identity)
   - Generates `X-Request-Id` (UUID4) for distributed tracing
   - Forwards to Router at `:8001/complete`

2. **Router** (`router/main.py`):
   - **Classify intent** (`intent.py`): hint-first, then
     batch → code → reasoning → interactive heuristics
   - **Determine priority**: INTERACTIVE/CODE → HIGH (bypass queue),
     REASONING → MEDIUM, BATCH → LOW
   - **Fetch health snapshot** from Health Monitor (`:8004/snapshot`)
   - **Rank providers** using multi-objective policy (`policy.py`)
   - **Fallback loop** — iterate through ranked providers:
     - Check rate limit (`:8002/check`) → skip if rate-limited
     - Call provider (`/v1/chat`) with 5s timeout
     - If success → record feedback, publish cost event, return
     - If failure → report to health monitor, try next provider
   - Sets `fallback_used=true` if we skipped past the #1 choice

3. **Response** flows back: Router → Gateway → Client

4. **Async side-effects** (non-blocking):
   - Cost event published to Cost Tracker
   - Success/failure reported to Health Monitor

### Q: What if ALL providers are down?

The policy engine (`policy.py` lines 78-82) detects this: if every
provider has an open circuit breaker, it enters **degraded mode** — it
puts all providers back into the candidate list and tries them anyway.
The response will have `degraded=true`. If they all still fail, the
router returns a structured `503 Service Unavailable` error — it never
hangs.

### Q: Why FastAPI instead of Flask or Django?

- **Async-native**: FastAPI runs on `uvicorn` (ASGI), so we get
  `async/await` for all I/O. A synchronous framework would block on
  every downstream HTTP call.
- **Pydantic integration**: Request/response validation is automatic.
  The shared `models.py` ensures all 6 services speak the same wire
  format with zero boilerplate.
- **Performance**: FastAPI is one of the fastest Python web frameworks.
  For a control plane in the critical path, this matters.

---

## 3. DISTRIBUTED SYSTEMS CONCEPTS

### Q: What distributed systems concepts does this project demonstrate?

| Course Topic | Where It Lives |
|---|---|
| Service discovery / naming | `router/registry.py` — in-memory analogue of Consul/etcd |
| Sync + async communication | HTTP for routing decisions; fire-and-forget for cost events |
| Leader election | `rate_limiter/election.py` — bully algorithm, 3 replicas |
| Consistency & replication | Strong consistency for rate limits (single leader); eventual consistency for costs |
| Fault tolerance | Circuit breakers (`health_monitor/circuit_breaker.py`) + router fallback chain |
| Backpressure & queueing | Priority request queue (`router/queue.py`) with load shedding |
| Multi-objective optimisation | `router/policy.py` — weighted scoring across 4 axes |
| Security | API-key auth at gateway; tenant identity rewritten server-side |
| Observability | Structured JSON logger, `/snapshot` endpoints, X-Request-Id tracing |

### Q: Explain the circuit breaker pattern. How does yours work?

**File**: `health_monitor/circuit_breaker.py`

Three states:

```
CLOSED ──(5 consecutive failures)──→ OPEN
OPEN ──(10 seconds elapse)──→ HALF_OPEN
HALF_OPEN ──(2 successes)──→ CLOSED
HALF_OPEN ──(any failure)──→ OPEN
```

- **CLOSED**: Normal operation. Every success/failure is recorded in a
  sliding window of 20 outcomes. If `consecutive_failures >= 5`, trip
  to OPEN.
- **OPEN**: Fast-fail. All requests are blocked for 10 seconds. No
  traffic hits the provider — this gives it time to recover.
- **HALF_OPEN**: Trial mode. Let requests through. If 2 succeed in a
  row, close the breaker. If any single request fails, snap back to
  OPEN.

**Why this matters**: Without a circuit breaker, a failing provider
would eat up latency budget on every request (timeout after 5s), and
the client would wait the full fallback chain. With the breaker open,
the router skips that provider instantly.

**Config** (in `health_monitor/main.py`):
```
failure_threshold = 3
open_duration_s = 5.0
half_open_success_required = 2
window_size = 20
```

### Q: Explain the token bucket algorithm for rate limiting.

**File**: `rate_limiter/token_bucket.py`

The token bucket is like a jar that holds tokens:

- **Capacity** = 10 tokens (burst size). The jar can hold at most 10.
- **Refill rate** = 1 token/second. One new token appears every second.
- Steady-state throughput: 60 requests/minute.

**On each request**:
1. Calculate elapsed time since last check
2. Add `elapsed * refill_rate` tokens (capped at capacity)
3. If `tokens >= 1`: consume one token, allow the request
4. If `tokens < 1`: deny, return `retry_after_ms` telling the client
   how long to wait

**Key per (tenant, provider)**: Each combination gets its own bucket.
So `demo-tenant|mock-fast` is independent from `demo-tenant|mock-cheap`.

**Why token bucket vs. fixed window?** Fixed windows have the boundary
problem (you can do 2x the limit at a window boundary). Token buckets
allow bursts (up to capacity) but enforce a smooth steady-state rate.

### Q: How does leader election work? Why do you need it?

**File**: `rate_limiter/election.py`

**Why**: Rate limiting must be strongly consistent — if two replicas
each think they have 10 tokens, they'd issue 20 total. One replica must
be the authoritative **leader** that makes all rate-limit decisions.

**Algorithm**: Bully election (simplified).
- Each replica has an ID (`rl-0`, `rl-1`, `rl-2`).
- **Highest ID wins**. When an election starts, each replica compares
  its ID against peers. The highest declares victory.
- **Leader sends heartbeats** every 1 second.
- **Followers watch**: if no heartbeat for 3 seconds, they assume the
  leader is dead and trigger a new election.
- **Followers forward** rate-limit checks to the leader over HTTP.

**Demo setup** (`start_stack.py`): Three replicas on ports 8002, 8012,
8022 with IDs `rl-0`, `rl-1`, `rl-2`. `rl-2` wins (highest ID).

### Q: What's the consistency model? Where is it strong vs. eventual?

| Data | Consistency | Why |
|---|---|---|
| Rate-limit tokens | **Strong** (single leader) | Can't over-issue tokens; all decisions go through one authoritative replica |
| Cost rollups | **Eventual** | Fire-and-forget async events; slight delay is acceptable for billing |
| Circuit breaker state | **Per-instance** | Each health monitor maintains its own sliding window; no cross-instance sync needed |
| Provider registry | **Per-router** | Providers registered at boot; no multi-router sync required for this project |

### Q: How does backpressure work?

**File**: `router/queue.py`

The request queue has three priority lanes:

```
INTERACTIVE, CODE  → HIGH priority (bypass queue entirely)
REASONING          → MEDIUM priority
BATCH              → LOW priority
```

**Backpressure mechanisms**:

1. **Load shedding**: When queue depth hits 80 (out of max 100), LOW
   priority requests are rejected immediately with 503.
2. **Low-priority cap**: At most 50 LOW requests can be queued.
3. **Hard cap**: At 100 total, everything is rejected.
4. **Timeout**: Each queued request has a 30-second timeout. If it
   hasn't been processed by then, it's rejected with 504.

**Why HIGH bypasses the queue**: Interactive chat and code completion
are latency-sensitive. A user typing in a chat UI should never wait
behind a batch summarisation job.

### Q: How does the feedback loop work?

**File**: `router/feedback.py`

After every completion, the router records an outcome:
- Provider name, latency (ms), success (bool), token counts

**Exponential Moving Average (EMA)** with alpha = 0.2:
```
new_ema = 0.2 * current_sample + 0.8 * old_ema
```

This means 80% weight on history, 20% on the latest observation. The
system remembers trends but adapts to recent changes.

**Tracked metrics**:
- `latency_ema`: Running average latency per provider
- `success_rate_ema`: Running success probability (starts at 1.0)
- `anomaly_score`: 0.0 = normal, 1.0 = extreme outlier

**How it feeds back into routing**: The policy engine reads
`latency_ema` to estimate future latency and `success_rate_ema` for
the success sub-score. Anomaly score becomes a penalty on the final
score. So a provider that's been slow recently will be scored lower
on the next request.

---

## 4. ROUTING POLICY DEEP DIVE

### Q: How does the multi-objective scoring policy work?

**File**: `router/policy.py`

Each provider is scored on four normalised axes (all 0-1, higher = better):

1. **Latency score**: `1.0 - (estimated_latency / worst_latency)`
2. **Cost score**: `1.0 - (cost_per_1k / worst_cost)`
3. **Capability score**: Direct from provider registration (e.g.,
   mock-smart has reasoning=0.95, mock-cheap has reasoning=0.4)
4. **Success score**: `1.0 - error_rate` (from health monitor +
   feedback EMA)

**Intent-specific weight vectors**:
```
INTERACTIVE: latency=0.55  cost=0.15  capability=0.20  success=0.10
REASONING:   latency=0.10  cost=0.10  capability=0.50  success=0.30
BATCH:       latency=0.05  cost=0.65  capability=0.15  success=0.15
CODE:        latency=0.25  cost=0.10  capability=0.45  success=0.20
```

Final score = weighted sum - anomaly penalty.

**Example**: For a BATCH request (cost-optimised):
- mock-cheap: cost_score=1.0 (cheapest), cost_weight=0.65 → dominates
- mock-smart: cost_score≈0.0 (most expensive) → penalised heavily
- Result: mock-cheap wins for batch, mock-fast wins for interactive

### Q: How does intent classification work?

**File**: `router/intent.py`

Deterministic heuristic classifier (priority order):

1. **Explicit hint wins**: If `intent_hint` is set, return it.
2. **Batch**: Keywords like "summarize the following", "translate the
   following", "extract", "batch"
3. **Code**: Keywords like "```", "def ", "class ", "traceback",
   regex for `\b(bug|error|exception)\b`
4. **Reasoning**: Keywords like "explain", "step by step", "why ",
   "analyze" — but requires EITHER (1+ keyword AND >200 chars) OR
   (2+ keywords). This prevents short "why?" messages from being
   classified as reasoning.
5. **Default**: `INTERACTIVE`

**Why deterministic, not ML?** So it can be unit-tested without an
external model. The classifier has 7 unit tests
(`tests/test_intent.py`) that verify exact inputs → outputs. A learned
model would make tests non-deterministic. The architecture supports
swapping in a real classifier later — it's a single function call.

---

## 5. TESTING QUESTIONS

### Q: How thorough is the test suite?

**42 tests total**: 35 unit tests + 7 integration tests.

| Test File | Count | What It Tests |
|---|---|---|
| `test_intent.py` | 7 | Intent classifier edge cases |
| `test_policy.py` | 7 | Routing policy scoring and ranking |
| `test_token_bucket.py` | 7 | Token bucket refill, consume, edge cases |
| `test_circuit_breaker.py` | 6 | State transitions, sliding window |
| `test_registry.py` | 4 | Provider register/deregister/lookup |
| `test_accounting.py` | 4 | Cost rollups, budget alerts |
| `test_feedback.py` | ~7 | EMA calculations, anomaly detection |
| `test_queue.py` | ~7 | Priority ordering, shedding, caps |
| `test_election.py` | ~7 | Election protocol, heartbeats |
| `test_integration.py` | 7 | **Full stack** — spawns all 8 processes |

### Q: What do the integration tests prove?

The integration tests (`test_integration.py`) spawn **all 8 real
processes** on ephemeral ports and exercise the system over HTTP:

1. **Interactive routing**: Send a short chat → verify it goes to
   mock-fast (lowest latency)
2. **Batch routing**: Send a "summarize" prompt → verify it goes to
   mock-cheap (lowest cost)
3. **Routing introspection**: Hit `/decide` → verify intent=reasoning
   ranks mock-smart first
4. **Automatic failover**: Force mock-fast to fail →
   verify the router falls back to another provider, `fallback_used=true`
5. **Rate limiting**: Shrink the bucket to capacity=1 → second request
   is rate-limited, falls back to another provider
6. **Cost tracking**: Verify the cost summary shows non-zero spend
   after completions
7. **Auth**: Verify unauthenticated request gets 401
8. **Feedback**: Verify feedback metrics are populated after completions
9. **Queue stats**: Verify queue endpoint returns expected shape
10. **Election**: Verify election status endpoint works

These aren't mock tests — they prove the services actually work together.

---

## 6. SECURITY QUESTIONS

### Q: How do you handle authentication?

**Gateway** (`gateway/main.py`): API-key authentication.

- Client sends `X-API-Key` header
- Gateway looks up the key in a map: `demo-key-123` → `demo-tenant`
- **Critical**: The gateway **overwrites** `tenant_id` in the request
  body with the authenticated principal. The client cannot impersonate
  another tenant by setting `tenant_id` in the JSON body.
- Missing/invalid key → 401 Unauthorized

**Why not OAuth/JWT?** Scope of a class project. The architecture
supports it — you'd replace the key lookup with a JWT validator. The
important design principle (server-side identity rewriting) is
demonstrated.

### Q: How do you prevent a tenant from seeing another tenant's data?

Tenant isolation is enforced at two levels:
1. **Gateway**: Rewrites tenant_id from the authenticated key, not
   from the request body
2. **Cost Tracker**: Rollups are keyed by tenant_id. The
   `/v1/cost/summary` endpoint on the gateway uses the authenticated
   tenant_id to query, so you can only see your own costs.

---

## 7. OBSERVABILITY QUESTIONS

### Q: How do you trace a request through the system?

**X-Request-Id propagation**:
1. Gateway generates a UUID4 (or uses one from the client header)
2. Passes it to the Router in the `X-Request-Id` header
3. Router includes it in the `CompletionResponse.request_id`
4. Router includes it in the `CostEvent.request_id`
5. All structured JSON logs include the request_id

So you can grep for one UUID across all service logs and see the full
request lifecycle.

### Q: What logging do you have?

**File**: `common/logging.py`

Structured JSON logs. Every log line is machine-parseable:
```json
{"ts": 1713456789.123, "level": "INFO", "service": "router", "msg": "route_decision", "intent": "interactive", "provider": "mock-fast"}
```

Every service uses `log_event(logger, event_name, **fields)` to emit
structured events. This is the pattern used by production systems
(Datadog, ELK, Splunk).

---

## 8. SCALABILITY & FUTURE WORK QUESTIONS

### Q: What would you change for production?

1. **Real Raft consensus** for rate limiter replication (currently
   simplified bully election with single authoritative leader)
2. **Persistent storage** (Redis for rate limits, Postgres for cost
   audit trail) — single-file changes
3. **Learned intent classifier** (fine-tuned small model instead of
   heuristics)
4. **Real LLM providers** — just change `ProviderInfo.url` in the
   registry
5. **Horizontal scaling** — run multiple routers behind a real load
   balancer; they're stateless (all state is in rate limiter / health
   monitor)
6. **mTLS** between services instead of plain HTTP
7. **Prometheus metrics** + Grafana dashboards instead of JSON logs

### Q: Can the router scale horizontally?

Yes. The router is **stateless** — it reads registry and feedback from
memory but makes no durable decisions. You could run 10 router instances
behind an nginx round-robin. The rate limiter and health monitor are
the stateful services, and they're designed to be centralised (one
leader for rate limiting, one health monitor for circuit breakers).

### Q: What are the failure modes and how does the system handle them?

| Failure | System Response |
|---|---|
| Provider returns 5xx | Router tries next provider in ranked list (`fallback_used=true`) |
| Provider times out (>5s) | Same — skip, try next |
| Circuit breaker open | Router skips that provider instantly (no wasted time) |
| Rate limit exhausted | Router skips that provider, tries next |
| All providers down | Degraded mode — try anyway, return 503 if all fail |
| Cost tracker down | Fire-and-forget fails silently; routing unaffected |
| Health monitor down | Router uses stale health data; still works but won't update breakers |
| Rate limiter leader down | Followers detect missing heartbeat after 3s, trigger re-election |

---

## 9. IMPLEMENTATION DETAIL QUESTIONS

### Q: What are the exact provider configurations?

Registered in `router/main.py`, launched in `start_stack.py`:

| Provider | Model | Latency (sim) | Cost/1K tokens | Best For |
|---|---|---|---|---|
| mock-fast | fast-1 | 30ms ± 10ms | $0.002 | Interactive, Code |
| mock-smart | smart-1 | 120ms ± 20ms | $0.02 | Reasoning |
| mock-cheap | cheap-1 | 80ms ± 15ms | $0.0003 | Batch |

**Capability scores** (registered in router):
```
mock-fast:  interactive=0.85, reasoning=0.45, batch=0.50, code=0.60
mock-smart: interactive=0.70, reasoning=0.95, batch=0.80, code=0.90
mock-cheap: interactive=0.55, reasoning=0.40, batch=0.75, code=0.45
```

### Q: How does cost calculation work?

After a successful completion:
```
estimated_cost = (total_tokens / 1000.0) * provider.cost_per_1k_tokens
```

This is published as a `CostEvent` (fire-and-forget) to the cost
tracker, which aggregates per-tenant rollups with per-provider
breakdown.

**Budget alerting**: You can set a budget via `POST /budget`. When
cumulative spend crosses it, an alert is generated (exactly once per
tenant per crossing).

### Q: How does the mock provider simulate real LLM behaviour?

**File**: `mock_provider/main.py`

- **Latency**: `sleep(base_latency ± uniform_jitter)` in milliseconds
- **Tokens**: `prompt_tokens = len(text) // 4` (≈4 chars per token,
  matches real tokenisers roughly),
  `completion_tokens = min(max_tokens, prompt_tokens // 2)`
- **Failures**: Bernoulli random with configurable rate, plus
  `/admin/force_fail` test hook
- **Response**: Returns a canned string: "This is a simulated
  response from {model}..."

### Q: How does the frontend work?

**File**: `frontend/index.html` — single-file SPA, no build step.

- Dark-themed dashboard with 5 panels:
  1. **Chat panel** — send prompts, see routed responses with metadata
  2. **Provider health** — live circuit breaker states (polls
     `/snapshot`)
  3. **Routing visualisation** — feedback metrics (latency EMA,
     success rate, anomaly score)
  4. **Cost tracker** — per-provider cost bars + total
  5. **Leader election** — shows which rate-limiter replica is leader
- Polls all endpoints every 2 seconds
- Served by a simple HTTP server on port 3000

---

## 10. TEAM CONTRIBUTION QUESTIONS

### Q: Who did what?

| Teammate | Modules | Tests |
|---|---|---|
| **Anukrithi** | models, config, logging, gateway, start_stack, demo, frontend, README, ARCHITECTURE | — |
| **Larry** | intent, policy, registry, feedback, queue, router/main | test_intent, test_policy, test_registry, test_feedback |
| **James** | token_bucket, election, rate_limiter/main, circuit_breaker, health_monitor/main | test_token_bucket, test_circuit_breaker, test_election |
| **Surbhi** | accounting, cost_tracker/main, mock_provider, run_all | test_accounting, test_queue, test_integration (x2) |

**Joint**: Architecture design, weekly integration reviews, project
report, presentation.

---

## 11. TRICKY / ADVERSARIAL QUESTIONS

### Q: This is just microservices calling each other over HTTP — what makes it "distributed"?

It demonstrates the **hard problems** of distributed systems:

1. **Partial failure**: Provider A is down but B and C are up. The
   system must handle this gracefully (circuit breakers + fallback).
2. **Consistency under concurrency**: Two concurrent requests must not
   both consume the last rate-limit token (solved by single-leader
   architecture).
3. **Leader failure**: What happens when the rate-limiter leader dies?
   Heartbeat timeout → bully election → new leader.
4. **Backpressure propagation**: When the system is overloaded, it
   sheds low-priority work rather than degrading everything equally.

These are the same problems Netflix, Uber, and AWS solve at scale.
The protocols are the same; our scale is smaller.

### Q: Your rate limiter only has one real leader — isn't that a single point of failure?

Yes, and that's intentional. The leader is the **authoritative** source
for rate-limit decisions because token counts must be strongly
consistent. But we mitigate the risk:

1. **Fast re-election**: Followers detect leader death within 3 seconds
   (heartbeat timeout) and elect a new leader.
2. **Safe default on restart**: New leader starts with full buckets.
   Worst case: you briefly over-serve (allow more requests than the
   limit). This is better than under-serving (blocking legitimate
   traffic).
3. **Production improvement**: Real Raft would replicate the bucket
   state to followers before acknowledging, so the new leader has the
   exact token counts. That's the natural next step.

### Q: What if the intent classifier gets it wrong?

Two safety nets:

1. **Fallback chain**: Even if we pick the "wrong" provider, if it
   fails or is rate-limited, we try the next one. The system is
   self-correcting.
2. **Explicit hint**: The API accepts `intent_hint` so the caller can
   override the classifier. The frontend dropdown uses this.
3. **Feedback loop**: Over time, the EMA metrics track which providers
   actually perform well for which requests, and the anomaly detector
   flags unusual behaviour.

### Q: How do you prevent thundering herd when a circuit breaker closes?

When a breaker transitions from OPEN → HALF_OPEN, it doesn't open the
floodgates. Only a small number of trial requests get through. If 2
succeed, the breaker closes. If any fails, it snaps back to OPEN for
another 10 seconds. This is the standard "probe and promote" pattern.

### Q: Why EMA instead of a simple average for feedback?

Simple averages weight all history equally. If a provider was great for
1000 requests and then started failing, the average would barely move.
EMA with alpha=0.2 means 20% weight on the latest observation — the
system adapts to recent changes within ~5 observations while still
being resistant to single outliers.

---

## 12. DEMO SCRIPT

If the professor asks for a live demo, here's the flow:

1. **Start the stack**: `PYTHONPATH=. python3 scripts/start_stack.py`
2. **Open dashboard**: `http://127.0.0.1:3000`
3. **Send a chat message**: Type "Hi there!" → routes to mock-fast
   (interactive, lowest latency)
4. **Send a reasoning prompt**: "Explain the CAP theorem step by step
   with analysis of tradeoffs" → routes to mock-smart (reasoning,
   highest capability)
5. **Send a batch prompt**: "Summarize the following document into
   bullet points" → routes to mock-cheap (batch, lowest cost)
6. **Show failover**: In terminal, run:
   ```
   curl -s -X POST http://127.0.0.1:9001/admin/force_fail \
     -H "Content-Type: application/json" -d '{"fail": true}'
   ```
   Then send another chat message — it falls back to another provider.
   Show the circuit breaker state on the dashboard.
7. **Show rate limiting**: Tighten the bucket:
   ```
   curl -s -X POST http://127.0.0.1:8002/config \
     -H "Content-Type: application/json" \
     -d '{"key":"demo-tenant|mock-cheap","capacity":1,"refill_rate":0.01}'
   ```
   Send two batch requests — second one falls back.
8. **Show cost tracking**: Point to the cost panel on the dashboard.
9. **Show election**: Point to the leader election panel — shows which
   replica is leader.
10. **Run tests**: `PYTHONPATH=. python3 -m pytest tests/ -v` → 42 passed

**Restore after demo**:
```
curl -s -X POST http://127.0.0.1:9001/admin/force_fail \
  -H "Content-Type: application/json" -d '{"fail": false}'
curl -s -X POST http://127.0.0.1:8002/config \
  -H "Content-Type: application/json" \
  -d '{"key":"demo-tenant|mock-cheap","capacity":10,"refill_rate":1.0}'
```
