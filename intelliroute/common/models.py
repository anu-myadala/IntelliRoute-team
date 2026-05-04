"""Shared Pydantic data models used across IntelliRoute services.

These are the canonical over-the-wire types. Every service imports from here
so that the gateway, router, rate limiter, cost tracker, and health monitor
all speak the same language.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Intent(str, Enum):
    """High-level classification of a prompt.

    The routing policy uses the intent to pick an appropriate provider/model.
    """

    INTERACTIVE = "interactive"  # low-latency, short responses (chat UI)
    REASONING = "reasoning"      # high-accuracy, longer chain-of-thought
    BATCH = "batch"              # offline/bulk, cost-sensitive
    CODE = "code"                # code generation / completion


class ProviderType(str, Enum):
    """Known upstream provider backend types.

    Stored in ``ProviderInfo.provider_type`` to let the provider-client
    adapter layer select the correct request/response transformation without
    string comparisons scattered across the codebase.

    The ``MOCK`` type is used in tests and demos; real deployments use one of
    the cloud or self-hosted variants.
    """

    MOCK = "mock"             # simulated backend (tests / demos)
    GROQ = "groq"             # Groq cloud inference API
    GEMINI = "gemini"         # Google Gemini API
    OPENAI = "openai"         # OpenAI Chat Completions API
    ANTHROPIC = "anthropic"   # Anthropic Messages API
    OLLAMA = "ollama"         # locally-hosted Ollama server
    VLLM = "vllm"             # vLLM OpenAI-compatible server


class CapabilityKey(str, Enum):
    """Keys used in ``ProviderInfo.capability`` score dicts.

    Each member value mirrors an ``Intent`` value so the routing policy can
    look up a provider's self-reported capability score for the current intent
    directly:

        score = provider.capability.get(CapabilityKey.REASONING.value, 0.5)

    Capability scores are floats in ``[0, 1]`` — higher means the provider is
    expected to perform better on that class of task. A missing key is treated
    as a neutral 0.5 by the multi-objective ranker.
    """

    INTERACTIVE = "interactive"  # speed-critical chat responses
    REASONING = "reasoning"      # deep chain-of-thought tasks
    BATCH = "batch"              # throughput-optimised offline work
    CODE = "code"                # code generation and debugging


class ChatMessage(BaseModel):
    role: str = Field(..., description="'system', 'user', or 'assistant'")
    content: str


class CompletionRequest(BaseModel):
    """A unified request format that mirrors the common shape of LLM APIs."""

    tenant_id: str = Field(
        default="",
        description="Tenant/team id. Clients may omit this; the gateway overrides it from the authenticated API key.",
    )
    team_id: Optional[str] = Field(
        default=None,
        description="Optional team scope for budget/cost governance.",
    )
    workflow_id: Optional[str] = Field(
        default=None,
        description="Optional workflow scope for budget/cost governance.",
    )
    messages: list[ChatMessage]
    max_tokens: int = 256
    temperature: float = 0.7
    # Optional explicit hint from the caller
    intent_hint: Optional[Intent] = None
    # Caller can set a hard latency budget in ms; router may use it to pick faster models
    latency_budget_ms: Optional[int] = None
    # Caller's self-assessed confidence that this request actually needs a
    # premium model, in [0, 1]. When below the policy's premium threshold
    # the router demotes tier-3 providers — this implements the spec's
    # "use premium model only above confidence threshold" rule without
    # requiring the policy_engine to know per-call.
    confidence_hint: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class PolicyEvaluationResult(BaseModel):
    """Control-plane policy output before multi-objective ranking."""

    complexity_score: float = 0.0
    complexity_signals: list[str] = Field(default_factory=list)
    allowed_providers: list[str] = Field(default_factory=list)
    blocked_providers: list[str] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    budget_actions: list[dict[str, str]] = Field(default_factory=list)
    downgrade_reason: Optional[str] = None
    fail_open: bool = False


class BrownoutStatus(BaseModel):
    """System-wide overload/brownout state snapshot."""

    is_degraded: bool = False
    reason: str = "healthy"
    entered_at_unix: Optional[float] = None
    queue_depth: int = 0
    p95_latency_ms: float = 0.0
    error_rate: float = 0.0
    timeout_rate: float = 0.0


class CompletionResponse(BaseModel):
    request_id: str
    provider: str
    model: str
    content: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    estimated_cost_usd: float
    # Set when the router had to fall back from the preferred provider
    fallback_used: bool = False
    degraded: bool = False
    policy_evaluation: Optional[PolicyEvaluationResult] = None
    brownout_status: Optional[BrownoutStatus] = None


class ProviderInfo(BaseModel):
    """A registered LLM backend."""

    name: str                    # e.g. "mock-openai"
    url: str                     # base URL of the provider service
    model: str                   # model identifier
    provider_type: str = "mock" # mock / groq / gemini
    # Capability scores in [0, 1]. Higher = better at that intent.
    capability: dict[str, float] = Field(default_factory=dict)
    # Cost in USD per 1K tokens (input + output averaged for simplicity)
    cost_per_1k_tokens: float = 0.0
    # Expected latency for a short completion, milliseconds
    typical_latency_ms: float = 500.0
    # Max concurrent in-flight requests recommended
    max_concurrency: int = 32
    # Capability tier: 1 = small/cheap, 2 = standard, 3 = premium. Used by the
    # graceful-degradation failover ladder: when a primary fails due to
    # overload/timeout, the router prefers a same-or-lower-tier sibling
    # rather than retrying another premium model.
    capability_tier: int = Field(default=2, ge=1, le=3)
    # Per-intent SLA: declared p95 latency the operator considers acceptable
    # for this provider on a given intent class (ms). Keys match Intent.value.
    # Empty dict means "no SLA declared" — the ranker treats it as no constraint.
    sla_p95_latency_ms: dict[str, float] = Field(default_factory=dict)
    # Per-provider retry budget used by the fallback loop's backoff calculation.
    max_retries: int = 3


class ProviderRegisterRequest(BaseModel):
    """Dynamic provider registration (requires heartbeats within ``lease_ttl_seconds``)."""

    provider_id: str = Field(
        default="",
        description="Stable id for heartbeats; defaults to provider.name when empty.",
    )
    provider: ProviderInfo
    lease_ttl_seconds: float = Field(
        ...,
        gt=0,
        description="Seconds allowed since last heartbeat before exclusion from routing.",
    )
    registration_source: str = Field(default="api", max_length=64)
    model_tier: str = Field(
        default="",
        description="Optional tier label (e.g. standard, premium); defaults to provider_type.",
        max_length=64,
    )


class ProviderHeartbeatRequest(BaseModel):
    """Refresh liveness lease for a dynamically registered provider."""

    provider_id: str = Field(..., min_length=1, max_length=256)


class ProviderHealth(BaseModel):
    name: str
    healthy: bool
    error_rate: float = 0.0
    avg_latency_ms: float = 0.0
    circuit_state: str = "closed"  # closed / open / half_open
    consecutive_failures: int = 0
    last_checked_unix: float = 0.0


class RateLimitCheck(BaseModel):
    tenant_id: str
    provider: str
    tokens_requested: int = 1


class RateLimitResult(BaseModel):
    allowed: bool
    remaining: float
    retry_after_ms: int = 0
    leader_replica: Optional[str] = None


class CostEvent(BaseModel):
    request_id: str
    tenant_id: str
    team_id: Optional[str] = None
    workflow_id: Optional[str] = None
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    unix_ts: float


class CostSummary(BaseModel):
    tenant_id: str
    total_requests: int
    total_tokens: int
    total_cost_usd: float
    by_provider: dict[str, float]
