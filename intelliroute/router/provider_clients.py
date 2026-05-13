"""Provider-specific request/response adapters for external LLM APIs."""
from __future__ import annotations

from typing import Any

import httpx

from ..common.config import settings
from ..common.models import CompletionRequest, ProviderInfo


class ProviderCallError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        status_code: int | None = None,
        retry_after_ms: int = 0,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retry_after_ms = retry_after_ms
        self.retryable = retryable


def _parse_retry_after_ms(headers: httpx.Headers) -> int:
    raw = headers.get("retry-after")
    if not raw:
        return 0
    try:
        return max(0, int(float(raw) * 1000))
    except (TypeError, ValueError):
        return 0


def _http_error(provider: str, response: httpx.Response) -> ProviderCallError:
    status = response.status_code
    retry_after_ms = _parse_retry_after_ms(response.headers)
    if status == 429:
        return ProviderCallError(
            f"{provider} rate limited",
            kind="rate_limited",
            status_code=status,
            retry_after_ms=retry_after_ms,
            retryable=True,
        )
    if 500 <= status < 600:
        return ProviderCallError(
            f"{provider} server error {status}",
            kind="server_error",
            status_code=status,
            retry_after_ms=retry_after_ms,
            retryable=True,
        )
    if 400 <= status < 500:
        return ProviderCallError(
            f"{provider} client error {status}",
            kind="client_error",
            status_code=status,
            retry_after_ms=0,
            retryable=False,
        )
    return ProviderCallError(
        f"{provider} http error {status}",
        kind="http_error",
        status_code=status,
        retry_after_ms=retry_after_ms,
        retryable=True,
    )


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
    timeout_s: float | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    provider_type = (info.provider_type or "mock").lower()
    timeout = settings.provider_timeout_s if timeout_s is None else timeout_s

    if provider_type == "mock":
        try:
            response = await http.post(
                f"{info.url}/v1/chat",
                json={
                    "messages": [m.model_dump() for m in req.messages],
                    "max_tokens": req.max_tokens,
                },
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderCallError(
                f"{info.name} timeout",
                kind="timeout",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderCallError(
                f"{info.name} transport error",
                kind="transport_error",
                retryable=True,
            ) from exc
        if response.status_code != 200:
            raise _http_error(info.name, response)
        return True, response.json()

    if provider_type == "groq":
        if not settings.groq_api_key:
            raise ProviderCallError("GROQ_API_KEY is not set")
        try:
            response = await http.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type": "application/json",
                },
                json=_groq_payload(info, req),
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderCallError(
                f"{info.name} timeout",
                kind="timeout",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderCallError(
                f"{info.name} transport error",
                kind="transport_error",
                retryable=True,
            ) from exc
        if response.status_code != 200:
            raise _http_error(info.name, response)
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
        try:
            response = await http.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{info.model}:generateContent",
                headers={
                    "x-goog-api-key": settings.gemini_api_key,
                    "Content-Type": "application/json",
                },
                json=_gemini_payload(req),
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderCallError(
                f"{info.name} timeout",
                kind="timeout",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderCallError(
                f"{info.name} transport error",
                kind="transport_error",
                retryable=True,
            ) from exc
        if response.status_code != 200:
            raise _http_error(info.name, response)
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
