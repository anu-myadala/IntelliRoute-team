from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Optional


POLICIES = ("intelliroute", "round_robin", "cheapest_first", "latency_first", "premium_first")
SCENARIOS = ("normal_mixed", "degraded_provider", "budget_pressure", "overload_brownout")


@dataclass
class WorkloadRequest:
    request_id: str
    tenant_id: str
    prompt: str
    max_tokens: int
    intent_hint: str
    priority: str
    team_id: Optional[str] = None
    workflow_id: Optional[str] = None
    confidence_hint: Optional[float] = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "messages": [{"role": "user", "content": self.prompt}],
            "max_tokens": self.max_tokens,
            "intent_hint": self.intent_hint,
        }
        if self.team_id:
            payload["team_id"] = self.team_id
        if self.workflow_id:
            payload["workflow_id"] = self.workflow_id
        if self.confidence_hint is not None:
            payload["confidence_hint"] = self.confidence_hint
        return payload

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReplayResult:
    request_id: str
    scenario_name: str
    policy_name: str
    success: bool
    status_code: int
    provider: Optional[str]
    latency_ms: float
    estimated_cost_usd: float
    fallback_used: bool
    premium_used: bool
    reject: bool
    reroute_or_downgrade: bool
    brownout_degraded: bool
    budget_actions: list[dict[str, str]]
    detail: str = ""
    timestamp_utc: str = ""
    tenant_id: str = ""
    team_id: str = ""
    workflow_id: str = ""
    seed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
