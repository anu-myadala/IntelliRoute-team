# Replay Evaluation Harness

This adds a reproducible replay experiment framework around the existing IntelliRoute stack.

> **Prereq:** the harness drives the *running* stack over HTTP. Start it first with `PYTHONPATH=. python3 scripts/start_stack.py` (or `start_stack_cloud.py` against a remote deployment) and then run `replay_eval.py` from a second terminal.

## What it does

- Generates deterministic workloads for:
  - `normal_mixed`
  - `degraded_provider`
  - `budget_pressure`
  - `overload_brownout`
- Replays requests against the real gateway (`/v1/complete`) with deterministic seeds
- Resets mutable service state before each `(scenario, policy)` run for fair comparisons
- Switches router mode for comparisons (full set in `intelliroute/eval_harness/types.py::POLICIES`):
  - `intelliroute` (default existing behavior)
  - `round_robin`
  - `cheapest_first`
  - `latency_first`
  - `premium_first`
- Exports:
  - quick outputs: `eval_results/results.jsonl`, `eval_results/summary.csv`
  - artifacts per run/matrix under `artifacts/`

## CLI

> Run from the repo root with `PYTHONPATH=.` so the `intelliroute.eval_harness` package resolves.

Generate a workload only:

`PYTHONPATH=. python3 scripts/generate_workload.py --scenario normal_mixed --size 80 --seed 7 --out eval_results/normal.jsonl`

Run one scenario + one policy:

`PYTHONPATH=. python3 scripts/replay_eval.py --scenario normal_mixed --policy intelliroute --size 60`

Run one scenario x all policies:

`PYTHONPATH=. python3 scripts/replay_eval.py --scenario normal_mixed --policy all --size 100 --seed 42`

Run all scenarios x all policies x 3 generated seeds:

`PYTHONPATH=. python3 scripts/replay_eval.py --scenario all --policy all --size 100 --seed-count 3`

Run brownout-focused burst replay:

`PYTHONPATH=. python3 scripts/replay_eval.py --scenario overload_brownout --policy all --size 120 --concurrency 12`

Disable automatic reset/isolation (debug only):

`PYTHONPATH=. python3 scripts/replay_eval.py --scenario normal_mixed --policy all --size 40 --no-reset`

Use explicit seeds:

`PYTHONPATH=. python3 scripts/replay_eval.py --scenario all --policy all --size 100 --seeds 11,22,33`

## Result record schema

Per-request output includes:

- `request_id`, `scenario_name`, `policy_name`
- `success`, `status_code`, `detail`
- `provider`, `latency_ms`, `estimated_cost_usd`
- `fallback_used`, `premium_used`
- `reroute_or_downgrade`, `reject`
- `brownout_degraded`
- `budget_actions` (if surfaced by policy metadata)
- `tenant_id`, `team_id`, `workflow_id`, `seed`, `timestamp_utc`

## Summary metrics

Computed per `(scenario_name, policy_name)`:

- total requests
- success rate
- error rate
- average latency
- median latency
- p50 latency
- p95 latency
- p99 latency
- latency std deviation
- total cost
- average cost per request
- premium usage rate
- fallback count
- reroute/downgrade count
- reject count
- provider distribution

## Artifact Structure

Single run:

`artifacts/replay_runs/<timestamp>_<scenario>_<policy>_seed<seed>/`

- `summary.json`
- `metrics.csv`
- `config.json`
- `reset_report.json`
- `timeline.csv`
- `run.log`

Matrix run:

`artifacts/matrix_runs/<timestamp>/`

- `aggregate_summary.json`
- `run_index.json`
- `report.md`
- `individual_runs/<run_id>/...` (same files as single run)

## Router policy switching

The router now exposes:

- `GET /routing/mode`
- `POST /routing/mode` with `{"mode":"intelliroute|round_robin|cheapest_first|latency_first|premium_first"}`

Default remains `intelliroute` unless `INTELLIROUTE_ROUTING_MODE` is set.

## Experiment isolation/reset

Before each `(scenario, policy)` run the harness now:

1. Recovers mock providers from forced-fail state
2. Resets router runtime state (feedback, queue, brownout, tuner, routing mode)
3. Resets cost tracker rollups/budgets
4. Resets health monitor circuit-breaker state
5. Resets configured rate limiter replicas
6. Applies scenario-specific setup (e.g. degrade provider, budget setup)

If reset fails:

- run is marked invalid
- workload is not executed
- reset failure is surfaced clearly

Reset endpoints used:

- `POST /reset` on the router (clears feedback, brownout, queue, tuner, routing mode, daily quotas, user feedback)
- `POST /reset` on the cost tracker (clears tenant/team/workflow rollups and budgets)
- `POST /reset` on the health monitor (clears all circuit breakers)
- `POST /reset` on each configured rate-limiter replica (default `8002`, `8012`, `8022`)

Mock recovery uses:

- `INTELLIROUTE_MOCK_PROVIDER_ADMIN_URLS` (comma-separated admin URLs), defaulting to the three local mock providers — calls `POST /admin/reset` on each so any forced fail/timeout/rate-limit/malformed flag from a previous run is cleared.

## Scenario Intent and Calibration

- `normal_mixed`: healthy baseline with mixed interactive/reasoning/batch load.
- `degraded_provider`: one provider degraded after reset to test failover/reroute.
- `budget_pressure`: constrained tenant/team/workflow budgets to force policy tradeoffs without full collapse.
- `overload_brownout`: overload-focused workload; keep optional for final report if it becomes noisy.

## Reproducibility Workflow

1. Start stack
2. Run replay with fixed seed(s)
3. Archive `artifacts/` and `eval_results/`
4. Cite `config.json`, `reset_report.json`, and `aggregate_summary.json` in report.

## Limitations

- Replay quality depends on realism of mock providers and traffic mixes.
- `degraded_provider` uses `INTELLIROUTE_DEGRADED_PROVIDER_ADMIN_URL` (defaults to `http://127.0.0.1:9002/admin/force_fail`).
- Brownout behavior is most visible when replay concurrency is increased.
