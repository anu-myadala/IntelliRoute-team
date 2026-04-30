# Team/Workflow Budget Scheduling

IntelliRoute now extends tenant-level budgeting with optional team/workflow scopes.

## Data model

- `CompletionRequest` supports optional `team_id` and `workflow_id`.
- `CostEvent` records optional `team_id` and `workflow_id`.
- Existing tenant behavior is unchanged when those fields are omitted.

## Rollups

Cost tracker keeps:

- tenant rollups (existing)
- team rollups (`/summary/team/{team_id}`, `/costs/teams`)
- workflow rollups (`/summary/workflow/{workflow_id}`, `/costs/workflows`)

## Budgets and caps

- tenant budgets (existing): `/budget`, `/budget/{tenant_id}`
- team budgets: `/budget/team`, `/budget/team/{team_id}`
- workflow budgets: `/budget/workflow`, `/budget/workflow/{workflow_id}`
- team premium cap: `/budget/team/premium-cap`

If a team/workflow budget is absent, routing falls back to tenant-level logic.

## Routing behavior

Router checks budget pressure at tenant, team, and workflow scopes:

- premium providers can be restricted under budget pressure
- workflow budget pressure prefers cheaper providers
- batch flows become more aggressively cost-optimized under workflow pressure

Policy explainability includes structured budget actions in `policy_evaluation.budget_actions`.

## Current limitations

- in-memory accounting only (no durable store)
- premium classification is heuristic (provider name based)
- budget pressure thresholds are rule-based, not learned
