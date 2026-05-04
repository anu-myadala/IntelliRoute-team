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
    """Score the structural complexity of a prompt in [0, 1].

    Each signal contributes a small additive boost that is individually capped
    so no single feature can dominate. The final score is clamped to [0, 1].
    Signals are also collected as human-readable strings for audit output.
    """
    text = "\n".join(m.content for m in request.messages)
    lower = text.lower()
    signals: list[str] = []

    # ------------------------------------------------------------------
    # Base score: raw character count normalised to a 6 000-char ceiling.
    # Longer prompts are almost always more complex regardless of content.
    # ------------------------------------------------------------------
    char_score = min(1.0, len(text) / 6000.0)
    if len(text) > 0:
        signals.append(f"chars={len(text)}")

    # ------------------------------------------------------------------
    # Fenced code blocks: each ``` marker pair indicates embedded code,
    # which typically requires a capable model to reason about correctly.
    # ------------------------------------------------------------------
    code_blocks = lower.count("```")
    fence_boost = min(0.35, code_blocks * 0.12)
    if code_blocks:
        signals.append(f"code_fence_markers={code_blocks}")

    # ------------------------------------------------------------------
    # Reasoning phrases: multi-step analytical language is a strong proxy
    # for the depth of inference the model will need to perform.
    # ------------------------------------------------------------------
    reasoning_hits = sum(1 for phrase in _REASONING_PHRASES if phrase in lower)
    reasoning_boost = min(0.35, reasoning_hits * 0.08)
    if reasoning_hits:
        signals.append(f"reasoning_phrases={reasoning_hits}")

    # ------------------------------------------------------------------
    # Long-prompt bonus: prompts beyond 1 200 chars almost always carry
    # enough context to warrant a capable model even if other signals are low.
    # ------------------------------------------------------------------
    long_boost = 0.15 if len(text) > 1200 else 0.0
    if long_boost:
        signals.append("long_prompt")

    # ------------------------------------------------------------------
    # Inline code tokens: bare keywords without fences still indicate code.
    # ------------------------------------------------------------------
    if re.search(r"\b(def |class |import |function |const |let )\b", text):
        signals.append("code_tokens")
        code_token_boost = 0.12
    else:
        code_token_boost = 0.0

    # ------------------------------------------------------------------
    # Question density: multiple distinct questions in one prompt suggest
    # the user wants a multi-part answer, which is harder to get right.
    # ------------------------------------------------------------------
    question_count = text.count("?")
    question_boost = min(0.10, question_count * 0.03)
    if question_count > 1:
        signals.append(f"questions={question_count}")

    # ------------------------------------------------------------------
    # Paragraph density: multiple blank-line-separated sections indicate
    # a structured, multi-topic prompt that benefits from chain-of-thought.
    # ------------------------------------------------------------------
    paragraph_count = len([p for p in text.split("\n\n") if p.strip()])
    paragraph_boost = min(0.10, max(0, paragraph_count - 1) * 0.04)
    if paragraph_count > 2:
        signals.append(f"paragraphs={paragraph_count}")

    # ------------------------------------------------------------------
    # Markdown table rows: tabular data usually requires the model to
    # compare or aggregate values across multiple dimensions.
    # ------------------------------------------------------------------
    table_rows = sum(1 for line in text.splitlines() if "|" in line and line.strip().startswith("|"))
    table_boost = min(0.10, table_rows * 0.05)
    if table_rows:
        signals.append(f"table_rows={table_rows}")

    # ------------------------------------------------------------------
    # Bullet / ordered lists: structured list prompts often enumerate
    # sub-tasks, each of which needs an accurate individual response.
    # ------------------------------------------------------------------
    bullet_lines = sum(
        1 for line in text.splitlines()
        if re.match(r"^\s*[-*•]\s+", line) or re.match(r"^\s*\d+\.\s+", line)
    )
    bullet_boost = min(0.10, bullet_lines * 0.025)
    if bullet_lines > 2:
        signals.append(f"list_items={bullet_lines}")

    # ------------------------------------------------------------------
    # Math / formula indicators: LaTeX-style notation, Greek letters, or
    # common operator symbols suggest quantitative reasoning tasks.
    # ------------------------------------------------------------------
    math_hit = bool(
        re.search(r"\\[a-zA-Z]+\{", text)          # LaTeX command e.g. \frac{
        or re.search(r"\$[^$]+\$", text)             # inline LaTeX $...$
        or re.search(r"\b(?:∑|∫|∂|∇|≈|≤|≥|→|⟹)\b", text)  # Unicode math symbols
        or re.search(r"\b(?:sigma|theta|lambda|alpha|beta|gamma)\b", lower)  # Greek names
    )
    math_boost = 0.08 if math_hit else 0.0
    if math_hit:
        signals.append("math_notation")

    # ------------------------------------------------------------------
    # Aggregate and clamp to [0, 1].
    # ------------------------------------------------------------------
    raw = (
        char_score
        + fence_boost
        + reasoning_boost
        + long_boost
        + code_token_boost
        + question_boost
        + paragraph_boost
        + table_boost
        + bullet_boost
        + math_boost
    )
    score = max(0.0, min(1.0, raw))
    return ComplexityResult(score=round(score, 4), signals=tuple(signals))
