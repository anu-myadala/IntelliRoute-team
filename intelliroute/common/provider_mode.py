"""Provider registration mode: mock vs external (Gemini/Groq) vs both.

``INTELLIROUTE_PROVIDER_MODE`` controls how the router bootstraps the registry.
``INTELLIROUTE_USE_MOCKS`` takes priority and behaves like ``mock_only``.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .models import ProviderInfo

_VALID_MODES = frozenset({"auto", "mock_only", "external_only", "hybrid"})


def normalize_provider_mode(raw: str | None) -> str:
    """Return a valid mode string; unknown values fall back to ``auto``."""
    if raw is None:
        return "auto"
    m = raw.strip().lower()
    return m if m in _VALID_MODES else "auto"


def effective_provider_mode(settings: Settings) -> str:
    """Resolved mode used for bootstrap (``use_mock_providers`` forces ``mock_only``)."""
    if settings.use_mock_providers:
        return "mock_only"
    return normalize_provider_mode(settings.provider_mode)


def should_skip_mock_uvicorn_subprocesses(settings: Settings) -> bool:
    """When True, ``start_stack`` does not spawn the three mock provider processes."""
    eff = effective_provider_mode(settings)
    if eff in ("mock_only", "hybrid"):
        return False
    if eff == "external_only":
        return True
    # auto
    return bool(settings.groq_api_key or settings.gemini_api_key)


@dataclass(frozen=True)
class BootstrapResult:
    """Outcome of registry bootstrap for :func:`build_bootstrap_result`."""

    providers: list[ProviderInfo]
    external_only_no_keys: bool
    skipped_mock_bootstrap: bool
    log_mode: str


def build_bootstrap_result(
    settings: Settings,
    external: list[ProviderInfo],
    mocks: list[ProviderInfo],
    mock_registration_mode: str,
) -> BootstrapResult:
    """Plan which bootstrap providers to register (order: external, then mocks)."""
    eff = effective_provider_mode(settings)
    dynamic = mock_registration_mode == "dynamic"

    if eff == "mock_only":
        if dynamic:
            return BootstrapResult([], False, True, mock_registration_mode)
        return BootstrapResult(mocks, False, False, mock_registration_mode)

    if eff == "external_only":
        if not external:
            return BootstrapResult([], True, False, "external_only")
        return BootstrapResult(external, False, False, "external_only")

    if eff == "hybrid":
        out: list[ProviderInfo] = []
        out.extend(external)
        if dynamic:
            return BootstrapResult(out, False, True, "hybrid")
        out.extend(mocks)
        return BootstrapResult(out, False, False, "hybrid")

    # auto
    if external:
        return BootstrapResult(external, False, False, "external")
    if dynamic:
        return BootstrapResult([], False, True, "dynamic")
    return BootstrapResult(mocks, False, False, mock_registration_mode)
