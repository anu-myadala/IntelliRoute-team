# Changelog

All notable changes to IntelliRoute are documented in this file.

## [0.2.0] — 2026-04-12

### Added
- Live dashboard frontend (`frontend/index.html`) with dark-themed UI:
  chat panel, provider health, routing visualization, cost tracker,
  and leader election status.
- `scripts/run_all.py` convenience launcher for all services.
- `tests/test_feedback.py`, `tests/test_queue.py`, `tests/test_election.py`
  unit tests.
- Feedback loop endpoint (`/feedback`) on the router service for
  per-provider latency and success-rate EMA tracking.
- Request queue with priority scheduling and load shedding
  (`intelliroute/router/queue.py`).
- Leader election status endpoint on the rate limiter
  (`/election/status`).

### Changed
- Integration test now covers 7 scenarios (was 4): added
  `test_feedback_endpoint_populated_after_completions`,
  `test_queue_stats_endpoint_shape`, and
  `test_election_status_shows_leader`.

### Fixed
- `start_stack.py` now spawns three rate-limiter replicas for the
  leader-election demo.

## [0.1.0] — 2026-03-28

### Added
- Initial distributed control plane: gateway, router, rate limiter,
  cost tracker, health monitor, and three mock LLM providers.
- Intent-aware routing with multi-objective scoring policy.
- Distributed token-bucket rate limiting with replication log.
- Three-state circuit breaker (closed → open → half-open).
- Per-tenant cost accounting with budget alerts.
- 35 unit tests + 4 integration tests.
- `scripts/start_stack.py` and `scripts/demo.py`.
- README with quick-start walkthrough and full API reference.
