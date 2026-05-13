from __future__ import annotations

import pytest
from pydantic import ValidationError

from intelliroute.common.models import CompletionRequest, Intent


def test_completion_request_accepts_optional_team_and_workflow_ids():
    req = CompletionRequest(
        tenant_id="t1",
        team_id="team-a",
        workflow_id="wf-1",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert req.team_id == "team-a"
    assert req.workflow_id == "wf-1"


def test_completion_request_backwards_compatible_without_team_workflow():
    req = CompletionRequest(
        tenant_id="t1",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert req.team_id is None
    assert req.workflow_id is None


# ---------------------------------------------------------------------------
# Additional field-level and constraint tests for shared Pydantic models
# ---------------------------------------------------------------------------

def test_completion_request_default_max_tokens():
    """CompletionRequest.max_tokens should default to 256 when the caller omits it.

    The default is intentionally conservative — high enough for most chat
    turns but low enough to avoid unexpected cost spikes on misconfigured clients.
    """
    req = CompletionRequest(
        tenant_id="t1",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert req.max_tokens == 256


def test_completion_request_default_temperature():
    """CompletionRequest.temperature should default to 0.7 when the caller omits it."""
    req = CompletionRequest(
        tenant_id="t1",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert req.temperature == 0.7


def test_completion_request_confidence_hint_rejects_above_one():
    """confidence_hint values above 1.0 must raise a Pydantic ValidationError.

    The field carries a ``le=1.0`` constraint; the router relies on this
    invariant when comparing against the policy's premium threshold.
    """
    with pytest.raises(ValidationError):
        CompletionRequest(
            tenant_id="t1",
            messages=[{"role": "user", "content": "hi"}],
            confidence_hint=1.5,
        )


def test_completion_request_confidence_hint_rejects_below_zero():
    """confidence_hint values below 0.0 must raise a Pydantic ValidationError.

    Negative confidence has no semantic meaning in the routing policy and
    should be rejected at the model layer before reaching the router.
    """
    with pytest.raises(ValidationError):
        CompletionRequest(
            tenant_id="t1",
            messages=[{"role": "user", "content": "hi"}],
            confidence_hint=-0.1,
        )


def test_intent_enum_has_exactly_four_variants():
    """Intent must expose exactly the four routing classes the system was designed around.

    Adding or removing an Intent value is a breaking change to the routing
    policy, policy engine rules, and all intent-keyed capability dicts —
    this test acts as a canary for accidental enum edits.
    """
    values = {i.value for i in Intent}
    assert values == {"interactive", "reasoning", "batch", "code"}


def test_completion_request_no_intent_hint_defaults_to_none():
    """intent_hint should be None by default so the router runs its own classification.

    A None hint means 'let the classifier decide'; the router only skips
    classification when an explicit hint is present.
    """
    req = CompletionRequest(
        tenant_id="t1",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert req.intent_hint is None
