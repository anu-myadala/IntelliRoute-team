"""Feedback collector with EMA metrics per provider.

Records completion outcomes and maintains exponential moving average (EMA)
metrics for each provider: latency, success rate, token efficiency, and
anomaly score.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


Clock = Callable[[], float]


# Canned-refusal phrases. Match anywhere, case-insensitively. The list is
# intentionally short — false positives here just nudge the anomaly EMA, they
# don't block routing — and skewed toward strings that almost never appear
# inside a legitimate substantive answer.
_REFUSAL_PATTERNS = (
    re.compile(r"\bi (?:cannot|can't|am unable to)\b", re.IGNORECASE),
    re.compile(r"\bas an ai (?:language )?model\b", re.IGNORECASE),
    re.compile(r"\bi'?m sorry,? but\b", re.IGNORECASE),
    re.compile(r"\bi (?:don'?t|do not) have (?:access|the ability)\b", re.IGNORECASE),
)


def compute_hallucination_signal(
    response_text: str,
    *,
    prompt_char_count: int = 1,
    expects_json: bool = False,
) -> float:
    """Heuristic hallucination/anomaly score in [0, 1] from a response.

    Returns 0.0 for healthy responses and approaches 1.0 as more red flags
    fire. Combined with the latency-anomaly signal in the feedback collector
    so that bad-output bursts pull the provider's ranking down without
    requiring an explicit user thumbs-down.

    Signals
    -------
    * Empty/near-empty response for a non-trivial prompt (length anomaly).
    * Canned refusal phrases ("I cannot...", "as an AI model", ...).
    * Declared JSON intent but the response does not parse.
    """
    if response_text is None:
        return 1.0
    text = response_text.strip()
    score = 0.0

    # Length anomaly: tiny output for a non-trivial prompt.
    if not text:
        score = max(score, 1.0)
    elif prompt_char_count >= 40 and len(text) < 5:
        score = max(score, 0.7)

    for pattern in _REFUSAL_PATTERNS:
        if pattern.search(text):
            score = max(score, 0.5)
            break

    if expects_json and text:
        try:
            json.loads(text)
        except (ValueError, TypeError):
            score = max(score, 0.6)

    return min(1.0, score)


@dataclass
class CompletionOutcome:
    """Result from a single completion attempt."""

    provider: str
    latency_ms: float
    success: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_char_count: int = 1
    response_char_count: int = 0
    # Pre-computed hallucination/output-shape score in [0, 1]. The router
    # passes the response text through ``compute_hallucination_signal`` and
    # stores the result here; the EMA collector folds it into anomaly_score.
    hallucination_signal: float = 0.0


@dataclass
class ProviderMetrics:
    """EMA metrics snapshot for a provider."""

    latency_ema: float = 0.0
    success_rate_ema: float = 1.0
    token_efficiency_ema: float = 1.0
    anomaly_score: float = 0.0
    sample_count: int = 0


class FeedbackCollector:
    """Thread-safe collector of completion outcomes with EMA metric tracking.

    Parameters
    ----------
    alpha : float
        EMA smoothing factor [0, 1]. Higher = more weight to recent samples.
    clock : Optional[Clock]
        Clock function for testing; defaults to time.monotonic.
    """

    def __init__(self, alpha: float = 0.2, clock: Optional[Clock] = None) -> None:
        self._alpha = alpha
        self._clock = clock or time.monotonic
        self._metrics: dict[str, ProviderMetrics] = {}
        self._lock = threading.Lock()

    def record(self, outcome: CompletionOutcome) -> None:
        """Record a completion outcome and update EMA metrics."""
        with self._lock:
            provider = outcome.provider
            if provider not in self._metrics:
                self._metrics[provider] = ProviderMetrics()

            metrics = self._metrics[provider]
            metrics.sample_count += 1

            # Update latency EMA (only on success)
            if outcome.success:
                if metrics.latency_ema == 0.0:
                    metrics.latency_ema = outcome.latency_ms
                else:
                    metrics.latency_ema = (
                        self._alpha * outcome.latency_ms
                        + (1 - self._alpha) * metrics.latency_ema
                    )

            # Update success rate EMA
            success_value = 1.0 if outcome.success else 0.0
            if metrics.success_rate_ema == 1.0 and metrics.sample_count == 1:
                metrics.success_rate_ema = success_value
            else:
                metrics.success_rate_ema = (
                    self._alpha * success_value
                    + (1 - self._alpha) * metrics.success_rate_ema
                )

            # Update token efficiency EMA
            if (
                outcome.prompt_tokens > 0
                and outcome.completion_tokens > 0
                and outcome.prompt_char_count > 0
                and outcome.response_char_count > 0
            ):
                prompt_efficiency = outcome.prompt_tokens / max(
                    1, outcome.prompt_char_count
                )
                response_efficiency = outcome.completion_tokens / max(
                    1, outcome.response_char_count
                )
                current_efficiency = (prompt_efficiency + response_efficiency) / 2.0
                if metrics.token_efficiency_ema == 1.0 and metrics.sample_count == 1:
                    metrics.token_efficiency_ema = current_efficiency
                else:
                    metrics.token_efficiency_ema = (
                        self._alpha * current_efficiency
                        + (1 - self._alpha) * metrics.token_efficiency_ema
                    )

            # Update anomaly score (latency-shape + hallucination proxy).
            anomaly = max(
                self._detect_anomaly(outcome),
                max(0.0, min(1.0, outcome.hallucination_signal)),
            )
            metrics.anomaly_score = (
                self._alpha * anomaly + (1 - self._alpha) * metrics.anomaly_score
            )

    @staticmethod
    def _detect_anomaly(outcome: CompletionOutcome) -> float:
        """Detect anomaly based on latency ratio.

        Returns a score in [0, 1] where 0 = normal, 1 = severe anomaly.
        """
        if outcome.latency_ms <= 0:
            return 0.0

        # Normal band is [0.1x, 10x] of expected for interactive (100ms)
        # If outside band, compute anomaly score
        ratio = outcome.latency_ms / 100.0
        if 0.1 <= ratio <= 10.0:
            return 0.0

        # Outside band: scale to [0, 1]
        if ratio < 0.1:
            return (0.1 - ratio) / 0.1
        else:
            return min(1.0, (ratio - 10.0) / 10.0)

    def get_metrics(self, provider: str) -> Optional[ProviderMetrics]:
        """Get the current metrics snapshot for a provider."""
        with self._lock:
            metrics = self._metrics.get(provider)
            if metrics is None:
                return None
            # Return a shallow copy to avoid mutations
            return ProviderMetrics(
                latency_ema=metrics.latency_ema,
                success_rate_ema=metrics.success_rate_ema,
                token_efficiency_ema=metrics.token_efficiency_ema,
                anomaly_score=metrics.anomaly_score,
                sample_count=metrics.sample_count,
            )

    def all_metrics(self) -> dict[str, ProviderMetrics]:
        """Return a copy of all provider metrics."""
        with self._lock:
            return {
                name: ProviderMetrics(
                    latency_ema=m.latency_ema,
                    success_rate_ema=m.success_rate_ema,
                    token_efficiency_ema=m.token_efficiency_ema,
                    anomaly_score=m.anomaly_score,
                    sample_count=m.sample_count,
                )
                for name, m in self._metrics.items()
            }
