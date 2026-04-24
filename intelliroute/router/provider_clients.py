"""Provider-specific request/response adapters for external LLM APIs."""
from __future__ import annotations

from typing import Any

import httpx

from ..common.config import settings
from ..common.models import CompletionRequest, ProviderInfo


class ProviderCallError(RuntimeError):
    pass


def _message_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(p for p in parts if p)
    return ""


def _extract_gemini_text(body: dict[str, Any]) -> str:
    candidates = body.get("candidates") or []
    if not candidates:
        return ""
    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    texts = [part.get("text", "") for part in parts if isinstance(part.get("text"), str)]
    return "\n".join(t for t in texts if t).strip()


def _extract_groq_text(body: dict[str, Any]) -> str:
    choices = body.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return _message_text_content(message.get("content")).strip()


def _gemini_payload(req: CompletionRequest) -> dict[str, Any]:
    system_messages = [m.content for m in req.messages if m.role == "system" and m.content]
    contents = []
    for message in req.messages:
        if message.role == "system":
            continue
        role = "model" if message.role == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": message.content}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": ""}]}]
    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": req.temperature,
            "maxOutputTokens": req.max_tokens,
        },
    }
    if system_messages:
        payload["system_instruction"] = {
            "parts": [{"text": "\n\n".join(system_messages)}]
        }
    return payload


def _groq_payload(info: ProviderInfo, req: CompletionRequest) -> dict[str, Any]:
    return {
        "model": info.model,
        "messages": [m.model_dump() for m in req.messages],
        "temperature": req.temperature,
        "max_completion_tokens": req.max_tokens,
        "user": req.tenant_id,
    }


async def call_provider(
    http: httpx.AsyncClient,
    info: ProviderInfo,
    req: CompletionRequest,
) -> tuple[bool, dict[str, Any] | None]:
    provider_type = (info.provider_type or "mock").lower()

    if provider_type == "mock":
        response = await http.post(
            f"{info.url}/v1/chat",
            json={
                "messages": [m.model_dump() for m in req.messages],
                "max_tokens": req.max_tokens,
            },
            timeout=settings.provider_timeout_s,
        )
        if response.status_code != 200:
            return False, None
        return True, response.json()

    if provider_type == "groq":
        if not settings.groq_api_key:
            raise ProviderCallError("GROQ_API_KEY is not set")
        response = await http.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type": "application/json",
            },
            json=_groq_payload(info, req),
            timeout=settings.provider_timeout_s,
        )
        if response.status_code != 200:
            return False, None
        body = response.json()
        content = _extract_groq_text(body)
        if not content:
            return False, None
        usage = body.get("usage") or {}
        return True, {
            "id": body.get("id", ""),
            "provider": info.name,
            "model": body.get("model", info.model),
            "content": content,
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        }

    if provider_type == "gemini":
        if not settings.gemini_api_key:
            raise ProviderCallError("GEMINI_API_KEY is not set")
        response = await http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{info.model}:generateContent",
            headers={
                "x-goog-api-key": settings.gemini_api_key,
                "Content-Type": "application/json",
            },
            json=_gemini_payload(req),
            timeout=settings.provider_timeout_s,
        )
        if response.status_code != 200:
            return False, None
        body = response.json()
        content = _extract_gemini_text(body)
        if not content:
            return False, None
        usage = body.get("usageMetadata") or {}
        return True, {
            "id": body.get("responseId", ""),
            "provider": info.name,
            "model": body.get("modelVersion", info.model),
            "content": content,
            "prompt_tokens": int(usage.get("promptTokenCount", 0) or 0),
            "completion_tokens": int(usage.get("candidatesTokenCount", 0) or 0),
        }

    raise ProviderCallError(f"unsupported provider_type={info.provider_type!r}")
