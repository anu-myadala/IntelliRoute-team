"""Heuristic prompt complexity for policy gating (no LLM calls)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ...common.models import CompletionRequest

_REASONING_PHRASES = (
    "explain",
    "reason",
    "prove",
    "derive",
    "analyze",
    "analyse",
    "compare",
    "step by step",
    "why ",
    "how does",
    "trade-off",
    "tradeoff",
    "think through",
    "chain of thought",
    "critique",
    "evaluate",
)


@dataclass(frozen=True)
class ComplexityResult:
    """Normalised complexity in ``[0, 1]`` plus human-readable signals."""

    score: float
    signals: tuple[str, ...]


def compute_complexity(request: CompletionRequest) -> ComplexityResult:
    text = "\n".join(m.content for m in request.messages)
    lower = text.lower()
    signals: list[str] = []

    char_score = min(1.0, len(text) / 6000.0)
    if len(text) > 0:
        signals.append(f"chars={len(text)}")

    code_blocks = lower.count("```")
    fence_boost = min(0.35, code_blocks * 0.12)
    if code_blocks:
        signals.append(f"code_fence_markers={code_blocks}")

    reasoning_hits = sum(1 for phrase in _REASONING_PHRASES if phrase in lower)
    reasoning_boost = min(0.35, reasoning_hits * 0.08)
    if reasoning_hits:
        signals.append(f"reasoning_phrases={reasoning_hits}")

    long_boost = 0.15 if len(text) > 1200 else 0.0
    if long_boost:
        signals.append("long_prompt")

    if re.search(r"\b(def |class |import |function |const |let )\b", text):
        signals.append("code_tokens")
        code_token_boost = 0.12
    else:
        code_token_boost = 0.0

    raw = char_score + fence_boost + reasoning_boost + long_boost + code_token_boost
    score = max(0.0, min(1.0, raw))
    return ComplexityResult(score=round(score, 4), signals=tuple(signals))
