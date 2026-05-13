from __future__ import annotations

from intelliroute.common.models import ChatMessage, CompletionRequest, ProviderInfo
from intelliroute.router.provider_clients import _extract_gemini_text, _extract_groq_text, _gemini_payload


def _req() -> CompletionRequest:
    return CompletionRequest(
        tenant_id="demo-tenant",
        messages=[
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="hello"),
            ChatMessage(role="assistant", content="hi there"),
            ChatMessage(role="user", content="explain CAP theorem"),
        ],
        max_tokens=123,
        temperature=0.4,
    )


def test_gemini_payload_maps_roles_and_config():
    payload = _gemini_payload(_req())
    assert payload["system_instruction"]["parts"][0]["text"] == "You are helpful."
    assert payload["contents"][0]["role"] == "user"
    assert payload["contents"][1]["role"] == "model"
    assert payload["contents"][2]["role"] == "user"
    assert payload["generationConfig"]["maxOutputTokens"] == 123
    assert payload["generationConfig"]["temperature"] == 0.4


def test_extract_gemini_text_joins_parts():
    text = _extract_gemini_text({"candidates": [{"content": {"parts": [{"text": "first line"}, {"text": "second line"}]}}]})
    assert text == "first line\nsecond line"


def test_extract_groq_text_handles_string_content():
    text = _extract_groq_text({"choices": [{"message": {"content": "hello from groq"}}]})
    assert text == "hello from groq"


def test_provider_info_defaults_to_mock_type():
    info = ProviderInfo(name="p1", url="http://example.com", model="m1")
    assert info.provider_type == "mock"


# ---------------------------------------------------------------------------
# Additional edge cases for provider payload builders and extractors
# ---------------------------------------------------------------------------

def test_gemini_payload_user_only_no_system_message():
    """When the request has no system-role message, the contents list must
    contain exactly the user turn.

    The Gemini payload builder should not inject empty system_instruction
    blocks that would confuse the upstream API on system-message-free requests.
    """
    req = CompletionRequest(
        tenant_id="t",
        messages=[ChatMessage(role="user", content="hi there")],
    )
    payload = _gemini_payload(req)
    # Only one message in, so only one content entry out.
    assert len(payload["contents"]) == 1
    assert payload["contents"][0]["role"] == "user"


def test_extract_gemini_text_single_part_returned_verbatim():
    """A single-part Gemini candidate should return the part text unchanged."""
    text = _extract_gemini_text({
        "candidates": [{"content": {"parts": [{"text": "single part answer"}]}}]
    })
    assert text == "single part answer"


def test_extract_groq_text_empty_string_content():
    """Groq extraction must handle an empty-string message content without error.

    A provider returning an empty string is a valid (if anomalous) response;
    the extractor must not raise or substitute a placeholder value.
    """
    result = _extract_groq_text({"choices": [{"message": {"content": ""}}]})
    assert result == ""


def test_provider_info_capability_tier_defaults_to_standard():
    """ProviderInfo.capability_tier should default to 2 (standard tier).

    Tier 2 represents the mid-range default — tier 1 is small/cheap and
    tier 3 is premium. A provider registered without an explicit tier is
    treated as standard for graceful-degradation ladder purposes.
    """
    info = ProviderInfo(name="p", url="http://p", model="m")
    assert info.capability_tier == 2


def test_provider_info_sla_latency_map_defaults_empty():
    """ProviderInfo.sla_p95_latency_ms should be an empty dict when not supplied.

    An empty SLA map means 'no SLA declared' — the policy ranker treats this
    as no latency constraint rather than treating it as a zero-millisecond SLA.
    """
    info = ProviderInfo(name="p", url="http://p", model="m")
    assert info.sla_p95_latency_ms == {}
