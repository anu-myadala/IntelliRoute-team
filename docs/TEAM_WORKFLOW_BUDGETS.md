# Team / Workflow Budget Scheduling

IntelliRoute extends tenant-level budgeting with optional team and
workflow scopes so a single request can be governed by up to three
overlapping budgets.

## Data model

- `CompletionRequest` accepts optional `team_id` and `workflow_id`.
- `CostEvent` records optional `team_id` and `workflow_id`, so cost
  rollups stay attributable across all three scopes.
- Existing tenant behavior is unchanged when those fields are omitted.

## Rollups (cost tracker, `:8003`)

Per-scope summaries:

- Tenant — `GET /summary/{tenant_id}`
- Team   — `GET /summary/team/{team_id}`, `GET /costs/teams`
- Workflow — `GET /summary/workflow/{workflow_id}`, `GET /costs/workflows`

Listings of every active budget per scope:

- `GET /budgets/teams`
- `GET /budgets/workflows`

## Budgets and caps

| Scope    | Set                                | Read                                |
|----------|------------------------------------|-------------------------------------|
| Tenant   | `POST /budget`                     | `GET /budget/{tenant_id}`           |
| Team     | `POST /budget/team`                | `GET /budget/team/{team_id}`        |
| Workflow | `POST /budget/workflow`            | `GET /budget/workflow/{workflow_id}`|
| Team premium cap | `POST /budget/team/premium-cap` | (returned by team budget endpoint) |

Tenant-only helpers:

- `GET /budget/{tenant_id}/headroom` — remaining budget
- `GET /budget/{tenant_id}/check?projected_cost_usd=…` — pre-call gate
- `GET /alerts` — fired budget-exceeded alerts (since the last `/reset`)

If a team or workflow budget is absent, routing falls back to the
tenant-level logic.

## Routing behavior

Pressure across all three scopes is fed into the policy engine
([`intelliroute/router/policy_engine/evaluator.py`](../intelliroute/router/policy_engine/evaluator.py)):

- `_rule_budget_downgrades_premium` — under tenant pressure, premium
  tier-3 providers can be blocked.
- `_rule_team_budget_controls` — same, scoped to a team budget; also
  enforces the team premium-spend cap.
- `_rule_workflow_budget_controls` — workflow pressure prefers
  cheaper providers; batch flows become more aggressively
  cost-optimised.
- Pre-call budget gate (`router/main.py`): if the head candidate's
  projected cost would push *any* of (tenant, team, workflow) over
  budget, demote to the cheapest still-pending candidate (logged as
  `budget_gate_demoted`).

Every routing decision returns a `PolicyEvaluationResult` (see
[`intelliroute/common/models.py`](../intelliroute/common/models.py))
containing:

- `matched_rules` — which rules fired
- `blocked_providers` — what they blocked and why
- `budget_actions` — structured records of the actions taken
- `complexity_score` — used to decide whether the request justified
  premium even under pressure
- `downgrade_reason` — short label (e.g. `team_premium_cap_exhausted`)

## Current limitations

- In-memory accounting only (no durable store). All rollups, alerts,
  and budgets reset on cost-tracker restart.
- Premium classification is heuristic and provider-name based.
- Budget-pressure thresholds are rule-based, not learned.
