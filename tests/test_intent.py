"""Unit tests for the intent classifier."""
from __future__ import annotations

from intelliroute.common.models import ChatMessage, CompletionRequest, Intent
from intelliroute.router.intent import classify


def _req(text: str, **kwargs) -> CompletionRequest:
    return CompletionRequest(
        tenant_id="t1",
        messages=[ChatMessage(role="user", content=text)],
        **kwargs,
    )


def test_explicit_hint_wins():
    req = _req("anything", intent_hint=Intent.BATCH)
    assert classify(req) == Intent.BATCH


def test_short_small_talk_is_interactive():
    assert classify(_req("Hi, what's up?")) == Intent.INTERACTIVE


def test_code_block_is_code():
    req = _req("Here is some code: ```def foo(): return 1```")
    assert classify(req) == Intent.CODE


def test_error_keywords_are_code():
    assert classify(_req("I got an exception in my loop")) == Intent.CODE


def test_batch_prefix_is_batch():
    assert classify(_req("Summarize the following document into bullet points")) == Intent.BATCH


def test_long_reasoning_prompt_is_reasoning():
    text = (
        "Explain step by step why the CAP theorem implies that a distributed "
        "system cannot simultaneously offer consistency, availability, and "
        "partition tolerance, and analyze how real systems trade off these "
        "properties in practice."
    )
    assert classify(_req(text)) == Intent.REASONING


def test_multiple_reasoning_keywords_short_prompt_is_reasoning():
    assert classify(_req("Explain and analyze this")) == Intent.REASONING


# ---------------------------------------------------------------------------
# Edge-case and boundary tests for the intent classifier
# ---------------------------------------------------------------------------

def test_empty_prompt_defaults_to_interactive():
    """A request whose message content is empty should resolve to INTERACTIVE.

    The classifier must not raise on an empty string — falling through all
    keyword checks with no match correctly maps to the interactive baseline.
    """
    assert classify(_req("")) == Intent.INTERACTIVE


def test_translate_keyword_triggers_batch():
    """'translate the following' is an explicit BATCH prefix in the keyword list."""
    assert classify(_req("translate the following paragraph into French")) == Intent.BATCH


def test_extract_keyword_triggers_batch():
    """'extract' as a standalone verb prefix should resolve to BATCH.

    Extraction tasks (entities, emails, structured fields) are offline/bulk
    workloads that don't require low-latency interactive routing.
    """
    assert classify(_req("extract all proper nouns from the document below")) == Intent.BATCH


def test_import_statement_triggers_code():
    """A prompt containing 'import ' (with trailing space) is unambiguously CODE."""
    assert classify(_req("import pandas as pd; df.head()")) == Intent.CODE


def test_single_reasoning_keyword_short_prompt_stays_interactive():
    """One reasoning keyword in a short prompt (under 200 chars) must NOT become REASONING.

    The classifier requires either two or more keyword hits, or one hit on a
    long prompt (> 200 chars). A single hit on a short prompt falls through
    to the INTERACTIVE baseline — this avoids false promotions to expensive
    reasoning-class providers on trivial one-word explain queries.
    """
    # 'explain this' alone is just 12 chars — well under the 200-char threshold.
    assert classify(_req("explain this")) == Intent.INTERACTIVE


def test_single_reasoning_keyword_long_prompt_is_reasoning():
    """One reasoning keyword with a prompt longer than 200 chars must be REASONING.

    Even a single keyword trigger justifies the routing upgrade when the
    prompt is long enough to indicate a substantive task.
    """
    # Pad far past the 200-char threshold to reliably cross the boundary.
    long_text = "explain " + ("the architecture of a distributed lock manager " * 5)
    assert classify(_req(long_text)) == Intent.REASONING


def test_explicit_hint_beats_batch_content():
    """An explicit intent_hint must override even a strong BATCH content signal.

    Callers know their workload better than the heuristic classifier; the
    hint is the escape hatch for cases where the keyword match would be wrong.
    """
    req = _req(
        "summarize the following document into bullet points",
        intent_hint=Intent.REASONING,
    )
    assert classify(req) == Intent.REASONING
