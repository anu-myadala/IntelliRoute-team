"""Environment-driven defaults for the control-plane policy engine."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _parse_name_list(raw: str | None, default: str) -> frozenset[str]:
    text = (raw if raw is not None else default).strip()
    if not text:
        return frozenset()
    return frozenset(p.strip() for p in text.split(",") if p.strip())


@dataclass(frozen=True)
class PolicyEngineConfig:
    """Tunable routing policy parameters (no core logic edits required)."""

    enabled: bool
    premium_provider_names: frozenset[str]
    complexity_threshold_premium: float
    budget_utilization_downgrade: float
    interactive_max_latency_ms: int
    apply_interactive_latency_gate: bool

    @classmethod
    def from_env(cls) -> PolicyEngineConfig:
        return cls(
            enabled=_env_bool("INTELLIROUTE_POLICY_ENGINE_ENABLED", True),
            premium_provider_names=_parse_name_list(
                os.environ.get("INTELLIROUTE_POLICY_PREMIUM_PROVIDERS"),
                "mock-smart,gemini",
            ),
            complexity_threshold_premium=_env_float(
                "INTELLIROUTE_POLICY_COMPLEXITY_FOR_PREMIUM", 0.5
            ),
            budget_utilization_downgrade=_env_float(
                "INTELLIROUTE_POLICY_BUDGET_UTIL_DOWNGRADE", 0.85
            ),
            interactive_max_latency_ms=_env_int(
                "INTELLIROUTE_POLICY_INTERACTIVE_MAX_LATENCY_MS", 650
            ),
            apply_interactive_latency_gate=_env_bool(
                "INTELLIROUTE_POLICY_INTERACTIVE_LATENCY_GATE", True
            ),
        )


DEFAULT_POLICY_CONFIG = PolicyEngineConfig.from_env()
