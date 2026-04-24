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
