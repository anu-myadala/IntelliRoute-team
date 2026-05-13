"""Central configuration for all IntelliRoute services.

Each service reads the ports and peer URLs from environment variables with
sensible defaults so the whole system can be brought up on a single host
without any configuration files.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .env import load_dotenv_if_present

load_dotenv_if_present()

TRUTHY = {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUTHY


def _env_provider_mode() -> str:
    """``auto`` | ``mock_only`` | ``external_only`` | ``hybrid`` (invalid → ``auto``)."""
    raw = os.environ.get("INTELLIROUTE_PROVIDER_MODE", "auto")
    m = raw.strip().lower()
    if m in ("auto", "mock_only", "external_only", "hybrid"):
        return m
    return "auto"


@dataclass
class Settings:
    gateway_port: int = _env_int("INTELLIROUTE_GATEWAY_PORT", 8000)
    router_port: int = _env_int("INTELLIROUTE_ROUTER_PORT", 8001)
    rate_limiter_port: int = _env_int("INTELLIROUTE_RATE_LIMITER_PORT", 8002)
    cost_tracker_port: int = _env_int("INTELLIROUTE_COST_TRACKER_PORT", 8003)
    health_monitor_port: int = _env_int("INTELLIROUTE_HEALTH_MONITOR_PORT", 8004)

    mock_fast_port: int = _env_int("INTELLIROUTE_MOCK_FAST_PORT", 9001)
    mock_smart_port: int = _env_int("INTELLIROUTE_MOCK_SMART_PORT", 9002)
    mock_cheap_port: int = _env_int("INTELLIROUTE_MOCK_CHEAP_PORT", 9003)

    host: str = os.environ.get("INTELLIROUTE_HOST", "127.0.0.1")

    gemini_api_key: str = os.environ.get("GEMINI_API_KEY", "")
    groq_api_key: str = os.environ.get("GROQ_API_KEY", "")
    gemini_model: str = os.environ.get("INTELLIROUTE_GEMINI_MODEL", "gemini-2.5-flash")
    groq_model: str = os.environ.get("INTELLIROUTE_GROQ_MODEL", "llama-3.3-70b-versatile")
    provider_timeout_s: float = _env_float("INTELLIROUTE_PROVIDER_TIMEOUT_S", 30.0)
    use_mock_providers: bool = _env_bool("INTELLIROUTE_USE_MOCKS", False)
    provider_mode: str = field(default_factory=_env_provider_mode)

    # Per-provider UTC daily request caps (successful completions). Opt-in.
    enable_provider_daily_quotas: bool = _env_bool(
        "INTELLIROUTE_ENABLE_PROVIDER_DAILY_QUOTAS", False
    )
    gemini_daily_request_quota: int = _env_int("INTELLIROUTE_GEMINI_DAILY_REQUEST_QUOTA", 15)
    groq_daily_request_quota: int = _env_int("INTELLIROUTE_GROQ_DAILY_REQUEST_QUOTA", 700)
    mock_fast_daily_request_quota: int = _env_int(
        "INTELLIROUTE_MOCK_FAST_DAILY_REQUEST_QUOTA", 2000
    )
    mock_cheap_daily_request_quota: int = _env_int(
        "INTELLIROUTE_MOCK_CHEAP_DAILY_REQUEST_QUOTA", 3000
    )
    mock_smart_daily_request_quota: int = _env_int(
        "INTELLIROUTE_MOCK_SMART_DAILY_REQUEST_QUOTA", 1000
    )
    mock_fault_daily_request_quota: int = _env_int(
        "INTELLIROUTE_MOCK_FAULT_DAILY_REQUEST_QUOTA", 500
    )
    provider_quota_warn_ratio: float = _env_float("INTELLIROUTE_PROVIDER_QUOTA_WARN_RATIO", 0.7)

    user_feedback_db_path: str = os.environ.get(
        "INTELLIROUTE_USER_FEEDBACK_DB_PATH", "artifacts/user_feedback.sqlite3"
    )
    feedback_prompt_preview_chars: int = _env_int("INTELLIROUTE_FEEDBACK_PROMPT_PREVIEW_CHARS", 200)
    feedback_response_preview_chars: int = _env_int(
        "INTELLIROUTE_FEEDBACK_RESPONSE_PREVIEW_CHARS", 300
    )
    feedback_analysis_default_rows: int = _env_int(
        "INTELLIROUTE_FEEDBACK_ANALYSIS_DEFAULT_ROWS", 100
    )
    feedback_analysis_max_rows: int = _env_int("INTELLIROUTE_FEEDBACK_ANALYSIS_MAX_ROWS", 500)
    feedback_analysis_sample_pool_max: int = _env_int(
        "INTELLIROUTE_FEEDBACK_ANALYSIS_SAMPLE_POOL_MAX", 20000
    )

    @property
    def router_url(self) -> str:
        return f"http://{self.host}:{self.router_port}"

    @property
    def rate_limiter_url(self) -> str:
        return f"http://{self.host}:{self.rate_limiter_port}"

    @property
    def cost_tracker_url(self) -> str:
        return f"http://{self.host}:{self.cost_tracker_port}"

    @property
    def health_monitor_url(self) -> str:
        return f"http://{self.host}:{self.health_monitor_port}"

settings = Settings()
