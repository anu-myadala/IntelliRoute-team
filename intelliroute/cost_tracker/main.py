"""Cost tracker HTTP service.

This is the asynchronous-accounting side of IntelliRoute. The router
posts cost events to ``/events`` (fire-and-forget from the request hot
path), and the tracker maintains running per-tenant aggregates that the
gateway can query via ``/summary/{tenant}``.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..common.logging import get_logger, log_event
from ..common.models import CostEvent, CostSummary
from .accounting import CostAccountant

log = get_logger("cost_tracker")
accountant = CostAccountant()

# Running event count since process start.  Not cleared by /reset so operators
# can distinguish "reset just ran" from "no traffic has arrived".
_total_events: int = 0
app = FastAPI(title="IntelliRoute CostTracker")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy"}


@app.post("/events")
async def record(event: CostEvent) -> dict:
    global _total_events
    _total_events += 1
    accountant.record(event)
    log_event(
        log,
        "cost_event_recorded",
        tenant=event.tenant_id,
        team=event.team_id,
        workflow=event.workflow_id,
        provider=event.provider,
        cost=event.estimated_cost_usd,
    )
    return {"ok": True}


@app.get("/summary/{tenant_id}", response_model=CostSummary)
async def summary(tenant_id: str) -> CostSummary:
    return accountant.summary(tenant_id)


class Budget(BaseModel):
    tenant_id: str
    budget_usd: float


class TeamBudget(BaseModel):
    team_id: str
    budget_usd: float


class WorkflowBudget(BaseModel):
    workflow_id: str
    budget_usd: float


class TeamPremiumCap(BaseModel):
    team_id: str
    premium_cap_usd: float


class ResetPayload(BaseModel):
    clear_budgets: bool = True


@app.post("/reset")
async def reset_state(payload: ResetPayload = ResetPayload()) -> dict:
    accountant.reset(clear_budgets=payload.clear_budgets)
    return {"ok": True, "cleared": "cost_tracker", "clear_budgets": payload.clear_budgets}


@app.post("/budget")
async def set_budget(b: Budget) -> dict:
    accountant.set_budget(b.tenant_id, b.budget_usd)
    return {"tenant": b.tenant_id, "budget_usd": b.budget_usd}


@app.get("/budget/{tenant_id}")
async def get_budget(tenant_id: str) -> dict:
    budget = accountant.get_budget(tenant_id)
    return {"tenant_id": tenant_id, "budget_usd": budget}


@app.post("/budget/team")
async def set_team_budget(b: TeamBudget) -> dict:
    accountant.set_team_budget(b.team_id, b.budget_usd)
    return {"team_id": b.team_id, "budget_usd": b.budget_usd}


@app.post("/budget/workflow")
async def set_workflow_budget(b: WorkflowBudget) -> dict:
    accountant.set_workflow_budget(b.workflow_id, b.budget_usd)
    return {"workflow_id": b.workflow_id, "budget_usd": b.budget_usd}


@app.post("/budget/team/premium-cap")
async def set_team_premium_cap(c: TeamPremiumCap) -> dict:
    accountant.set_team_premium_cap(c.team_id, c.premium_cap_usd)
    return {"team_id": c.team_id, "premium_cap_usd": c.premium_cap_usd}


@app.get("/budget/team/{team_id}")
async def get_team_budget(team_id: str) -> dict:
    return accountant.team_budget_status(team_id)


@app.get("/budget/workflow/{workflow_id}")
async def get_workflow_budget(workflow_id: str) -> dict:
    return accountant.workflow_budget_status(workflow_id)


@app.get("/budgets/teams")
async def list_team_budgets() -> dict:
    return {"budgets": accountant.team_budgets()}


@app.get("/budgets/workflows")
async def list_workflow_budgets() -> dict:
    return {"budgets": accountant.workflow_budgets()}


@app.get("/costs/teams")
async def team_costs() -> dict:
    return {"teams": accountant.team_summaries()}


@app.get("/costs/workflows")
async def workflow_costs() -> dict:
    return {"workflows": accountant.workflow_summaries()}


@app.get("/summary/team/{team_id}")
async def team_summary(team_id: str) -> dict:
    return accountant.team_summary(team_id)


@app.get("/summary/workflow/{workflow_id}")
async def workflow_summary(workflow_id: str) -> dict:
    return accountant.workflow_summary(workflow_id)


@app.get("/alerts")
async def alerts() -> dict:
    return {"alerts": accountant.alerts()}


@app.get("/budget/{tenant_id}/headroom")
async def headroom(tenant_id: str) -> dict:
    return {"tenant_id": tenant_id, "headroom_usd": accountant.headroom(tenant_id)}


@app.get("/budget/{tenant_id}/check")
async def check_budget(tenant_id: str, projected_cost_usd: float = 0.0) -> dict:
    return {
        "tenant_id": tenant_id,
        "projected_cost_usd": projected_cost_usd,
        "would_exceed": accountant.would_exceed(tenant_id, projected_cost_usd),
    }


@app.get("/history/{tenant_id}")
async def event_history(
    tenant_id: str,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Return a paginated slice of raw cost events recorded for a tenant.

    Events are ordered oldest-first so a caller can page forward through
    history by incrementing ``offset`` by ``limit`` on each request.

    Query parameters
    ----------------
    limit  : maximum events per page (default: 100, max enforced by the log cap)
    offset : zero-based start index into the tenant's filtered event list
    """
    # Clamp limit to a sane upper bound to avoid accidentally large responses.
    clamped_limit = min(limit, 500)
    events = accountant.event_history(tenant_id, limit=clamped_limit, offset=offset)
    total = accountant.total_event_count(tenant_id)
    return {
        "tenant_id": tenant_id,
        "offset": offset,
        "limit": clamped_limit,
        "total": total,
        "count": len(events),
        # Serialize each CostEvent as a plain dict for JSON transport.
        "events": [e.model_dump() for e in events],
    }


class CostTrackerStats(BaseModel):
    """Aggregate cost-tracker statistics returned by GET /stats."""

    # Number of distinct tenants that have sent at least one cost event.
    total_tenants: int

    # Total POST /events calls received since process start (not reset by /reset).
    total_events_received: int

    # Platform-wide aggregates derived from in-memory rollups.
    platform_total_requests: int
    platform_total_cost_usd: float

    # Budget governance counters.
    active_tenant_budgets: int
    active_team_budgets: int
    active_workflow_budgets: int

    # Number of budget-exceeded alerts that have fired since the last /reset.
    triggered_alerts: int


@app.get("/stats", response_model=CostTrackerStats)
async def cost_stats() -> CostTrackerStats:
    """Return aggregate cost and budget governance statistics."""
    # Aggregate across all tenant rollups held in the accountant.
    # We access the rollup dict directly since CostAccountant is in the same
    # service process; no remote call is needed.
    with accountant._lock:
        total_tenants = len(accountant._rollups)
        platform_requests = sum(r.total_requests for r in accountant._rollups.values())
        platform_cost = round(
            sum(r.total_cost_usd for r in accountant._rollups.values()), 6
        )
        active_tenant_budgets = len(accountant._budgets)
        active_team_budgets = len(accountant._team_budgets)
        active_workflow_budgets = len(accountant._workflow_budgets)
        triggered_alerts = len(accountant._alerts)
    return CostTrackerStats(
        total_tenants=total_tenants,
        total_events_received=_total_events,
        platform_total_requests=platform_requests,
        platform_total_cost_usd=platform_cost,
        active_tenant_budgets=active_tenant_budgets,
        active_team_budgets=active_team_budgets,
        active_workflow_budgets=active_workflow_budgets,
        triggered_alerts=triggered_alerts,
    )
