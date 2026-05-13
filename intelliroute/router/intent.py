"""Intent classification.

The goal of this module is to turn an unstructured ``CompletionRequest``
into an :class:`Intent` that the routing policy can consume.

We deliberately keep this *simple and deterministic* rather than calling
an ML model: the distributed-systems focus of the project is not in the
classifier, and a heuristic classifier is easy to unit test.
"""
from __future__ import annotations

import re

from ..common.models import ChatMessage, CompletionRequest, Intent


# ---------------------------------------------------------------------------
# Keyword hint sets used by the heuristic classifier.
#
# Design notes:
#   - Strings are matched case-sensitively by default after lowercasing the
#     joined prompt text, so all entries here must be lowercase.
#   - Err on the side of recall over precision within each bucket: a
#     misclassified intent degrades routing quality slightly; a missed
#     classification falls through to INTERACTIVE which is safe.
#   - Do NOT add generic English words (e.g. "how") — they produce too many
#     false positives across intents.
# ---------------------------------------------------------------------------

_CODE_HINTS = (
    # Fenced code blocks and common language constructs.
    "```",
    "def ",        # Python function definition
    "class ",      # class declaration (Python, Java, JS, TS, C++)
    "function ",   # JavaScript / TypeScript function keyword
    "import ",     # module import in Python, Java, JS/TS, etc.
    "const ",      # JS/TS constant declaration
    "let ",        # JS/TS block-scoped variable
    "var ",        # JS legacy variable declaration
    "<html",       # HTML markup fragments
    "SELECT ",     # SQL uppercase convention
    "select ",     # SQL (lowercase variant)
    "stack trace", # error output preamble
    "traceback",   # Python exception traceback
    # Error-related keywords that almost exclusively appear in debugging prompts.
    "syntax error",
    "runtime error",
    "null pointer",
    "segmentation fault",
    "compile error",
    "linker error",
    # Tool / package management — indicates a dev environment context.
    "npm install",
    "pip install",
    "yarn add",
    "cargo build",
    # Common programming tasks.
    "refactor",
    "unit test",
    "write a function",
    "write a script",
    "fix this code",
    "debug this",
)

_REASONING_HINTS = (
    # Core analytical verbs.
    "explain",
    "reason",
    "prove",
    "derive",
    "analyze",
    "analyse",
    "compare",
    "evaluate",
    "critique",
    "assess",
    "infer",
    "deduce",
    "conclude",
    # Multi-step reasoning phrases.
    "step by step",
    "chain of thought",
    "think through",
    "walk me through",
    "break down",
    # Causal / comparative constructs.
    "why ",
    "how does",
    "what causes",
    "what are the implications",
    "pros and cons",
    "advantages and disadvantages",
    "compare and contrast",
    "trade-off",
    "tradeoff",
    # Argumentative / academic framing.
    "argue",
    "justify",
    "make the case",
    "counter-argument",
)

_BATCH_HINTS = (
    # Explicit bulk-processing prefixes.
    "summarize the following",
    "translate the following",
    "extract",
    "generate a list of",
    "batch",
    # Additional bulk-task patterns.
    "process all",
    "for each of the following",
    "classify each",
    "tag each",
    "convert the following",
    "parse the following",
    "create a report",
    "generate n ",    # e.g. "generate 10 examples of"
    "bulk",
    "transform the following",
)


def _joined_text(messages: list[ChatMessage]) -> str:
    return "\n".join(m.content for m in messages).lower()


def classify(request: CompletionRequest) -> Intent:
    """Return the inferred intent for ``request``.

    Priority order:

    1. An explicit ``intent_hint`` always wins.
    2. Long prompts with reasoning keywords are REASONING.
    3. Presence of code hints flags CODE.
    4. Explicit batch markers flag BATCH.
    5. Short prompts default to INTERACTIVE.
    """
    if request.intent_hint is not None:
        return request.intent_hint

    text = _joined_text(request.messages)
    total_chars = len(text)

    # Batch first: an explicit batch prefix should outrank anything else.
    if any(h in text for h in _BATCH_HINTS):
        return Intent.BATCH

    if any(h in text for h in _CODE_HINTS) or re.search(r"\b(bug|error|exception)\b", text):
        return Intent.CODE

    reasoning_hits = sum(1 for h in _REASONING_HINTS if h in text)
    if reasoning_hits >= 1 and total_chars > 200:
        return Intent.REASONING
    if reasoning_hits >= 2:
        return Intent.REASONING

    return Intent.INTERACTIVE
