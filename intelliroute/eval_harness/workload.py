from __future__ import annotations

import json
import random
from pathlib import Path

from .types import SCENARIOS, WorkloadRequest


def generate_workload(scenario: str, size: int, seed: int = 7) -> list[WorkloadRequest]:
    if scenario not in SCENARIOS:
        raise ValueError(f"unsupported scenario: {scenario}")
    rng = random.Random(seed)
    if scenario == "normal_mixed":
        return _normal_mixed(size, rng)
    if scenario == "degraded_provider":
        return _degraded_provider(size, rng)
    if scenario == "budget_pressure":
        return _budget_pressure(size, rng)
    return _overload_brownout(size, rng)


def write_workload_jsonl(path: Path, records: list[WorkloadRequest]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in records:
            f.write(json.dumps(row.to_dict()) + "\n")


def read_workload_jsonl(path: Path) -> list[WorkloadRequest]:
    rows: list[WorkloadRequest] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            raw = json.loads(line)
            rows.append(WorkloadRequest(**raw))
    return rows


def _normal_mixed(size: int, rng: random.Random) -> list[WorkloadRequest]:
    out: list[WorkloadRequest] = []
    for i in range(size):
        pick = rng.random()
        if pick < 0.45:
            intent, prompt, tokens, priority = (
                "interactive",
                "Answer briefly: what is eventual consistency?",
                64,
                "high",
            )
            confidence = 0.25
        elif pick < 0.80:
            intent, prompt, tokens, priority = (
                "reasoning",
                "Solve this distributed-systems tradeoff question with detailed reasoning and alternatives.",
                220,
                "medium",
            )
            confidence = 0.85
        else:
            intent, prompt, tokens, priority = (
                "batch",
                "Summarize this long report into ten bullets with action items.",
                140,
                "low",
            )
            confidence = 0.15
        out.append(
            WorkloadRequest(
                request_id=f"normal-{i}",
                tenant_id="demo-tenant",
                prompt=prompt,
                max_tokens=tokens,
                intent_hint=intent,
                priority=priority,
                confidence_hint=confidence,
            )
        )
    return out


def _degraded_provider(size: int, rng: random.Random) -> list[WorkloadRequest]:
    out: list[WorkloadRequest] = []
    for i in range(size):
        out.append(
            WorkloadRequest(
                request_id=f"degraded-{i}",
                tenant_id="demo-tenant",
                prompt=(
                    "Reason deeply and provide a step-by-step architecture diagnosis. "
                    "Include fallback choices and confidence."
                ),
                max_tokens=210,
                intent_hint="reasoning",
                priority="medium",
                confidence_hint=0.9 if i % 3 else rng.uniform(0.7, 0.95),
            )
        )
    return out


def _budget_pressure(size: int, rng: random.Random) -> list[WorkloadRequest]:
    out: list[WorkloadRequest] = []
    for i in range(size):
        workflow = "nightly-batch" if i % 2 == 0 else "ops-dashboard"
        intent = "batch" if i % 2 == 0 else "reasoning"
        prompt = (
            "Batch summarize customer logs."
            if intent == "batch"
            else "Provide a detailed incident triage recommendation with options."
        )
        out.append(
            WorkloadRequest(
                request_id=f"budget-{i}",
                tenant_id="demo-tenant",
                team_id="team-alpha",
                workflow_id=workflow,
                prompt=prompt,
                max_tokens=150 if intent == "batch" else 180,
                intent_hint=intent,
                priority="low" if intent == "batch" else "medium",
                confidence_hint=0.12 if intent == "batch" else rng.uniform(0.65, 0.9),
            )
        )
    return out


def _overload_brownout(size: int, rng: random.Random) -> list[WorkloadRequest]:
    out: list[WorkloadRequest] = []
    for i in range(size):
        # Burst medium/low priority traffic to push queue depth and trigger brownout behavior.
        if i % 3 == 0:
            intent = "batch"
            priority = "low"
            max_tokens = 512
            prompt = "Summarize this very long operational report into bullet points with details."
        elif i % 3 == 1:
            intent = "reasoning"
            priority = "medium"
            max_tokens = 420
            prompt = "Analyze system logs and propose root cause hypotheses with justification."
        else:
            intent = "interactive"
            priority = "high"
            max_tokens = 120
            prompt = "Give a concise status update and immediate next steps."
        out.append(
            WorkloadRequest(
                request_id=f"overload-{i}",
                tenant_id="demo-tenant",
                prompt=prompt,
                max_tokens=max_tokens,
                intent_hint=intent,
                priority=priority,
                confidence_hint=rng.uniform(0.2, 0.8),
            )
        )
    return out
