"""Unit tests for the feedback collector with EMA metrics."""
from __future__ import annotations

import time

from intelliroute.router.feedback import CompletionOutcome, FeedbackCollector


def test_feedback_initialization():
    """Test that feedback collector initializes correctly."""
    fc = FeedbackCollector(alpha=0.2)
    assert fc.all_metrics() == {}


def test_record_single_outcome_initializes_metrics():
    """Test that recording a single outcome initializes provider metrics."""
    fc = FeedbackCollector()
    outcome = CompletionOutcome(
        provider="test-provider",
        latency_ms=100.0,
        success=True,
    )
    fc.record(outcome)
    metrics = fc.get_metrics("test-provider")
    assert metrics is not None
    assert metrics.latency_ema == 100.0
    assert metrics.success_rate_ema == 1.0
    assert metrics.sample_count == 1


def test_latency_ema_convergence():
    """Test that latency EMA converges toward new values."""
    fc = FeedbackCollector(alpha=0.5)
    # Record several outcomes with increasing latency
    for i in range(5):
        outcome = CompletionOutcome(
            provider="test-provider",
            latency_ms=100.0 + i * 10.0,  # 100, 110, 120, ...
            success=True,
        )
        fc.record(outcome)

    metrics = fc.get_metrics("test-provider")
    assert metrics is not None
    # EMA should be somewhere between 100 and 140
    assert 100.0 < metrics.latency_ema < 140.0


def test_success_rate_ema():
    """Test success rate EMA calculation."""
    fc = FeedbackCollector(alpha=0.2)
    # Record 3 successes, then 1 failure
    for _ in range(3):
        fc.record(CompletionOutcome(provider="p1", latency_ms=100.0, success=True))

    metrics = fc.get_metrics("p1")
    assert metrics is not None
    assert metrics.success_rate_ema == 1.0

    # Record a failure
    fc.record(CompletionOutcome(provider="p1", latency_ms=100.0, success=False))
    metrics = fc.get_metrics("p1")
    # EMA with alpha=0.2: 0.2 * 0 + 0.8 * 1.0 = 0.8
    assert metrics.success_rate_ema < 1.0


def test_anomaly_detection_normal_latency():
    """Test that normal latency is not flagged as anomaly."""
    fc = FeedbackCollector()
    outcome = CompletionOutcome(
        provider="p1",
        latency_ms=50.0,  # Within [0.1*100, 10*100]
        success=True,
    )
    fc.record(outcome)
    metrics = fc.get_metrics("p1")
    assert metrics.anomaly_score == 0.0


def test_anomaly_detection_very_slow():
    """Test that very slow latency is flagged as anomaly."""
    fc = FeedbackCollector()
    outcome = CompletionOutcome(
        provider="p1",
        latency_ms=1500.0,  # Ratio 15.0, well outside [0.1, 10]
        success=True,
    )
    fc.record(outcome)
    metrics = fc.get_metrics("p1")
    # min(1.0, (1500/100 - 10.0) / 10.0) = min(1.0, 0.5) = 0.5, but first sample gives exact value
    assert metrics.anomaly_score > 0.0


def test_anomaly_detection_very_fast():
    """Test that very fast latency is flagged as anomaly."""
    fc = FeedbackCollector()
    outcome = CompletionOutcome(
        provider="p1",
        latency_ms=5.0,  # Ratio 0.05, outside [0.1, 10]
        success=True,
    )
    fc.record(outcome)
    metrics = fc.get_metrics("p1")
    assert metrics.anomaly_score > 0.0


def test_provider_isolation():
    """Test that metrics are isolated per provider."""
    fc = FeedbackCollector()
    fc.record(CompletionOutcome(provider="p1", latency_ms=100.0, success=True))
    fc.record(CompletionOutcome(provider="p2", latency_ms=200.0, success=False))

    m1 = fc.get_metrics("p1")
    m2 = fc.get_metrics("p2")
    assert m1 is not None
    assert m2 is not None
    assert m1.latency_ema == 100.0
    # Latency EMA only updates on success, so p2 (failed) has latency_ema = 0.0
    assert m2.latency_ema == 0.0
    assert m1.success_rate_ema == 1.0
    assert m2.success_rate_ema == 0.0


def test_get_metrics_returns_copy():
    """Test that get_metrics returns a copy, not a reference."""
    fc = FeedbackCollector()
    fc.record(CompletionOutcome(provider="p1", latency_ms=100.0, success=True))

    m1 = fc.get_metrics("p1")
    m2 = fc.get_metrics("p1")
    assert m1 == m2
    # They should be different objects
    assert m1 is not m2


def test_all_metrics_returns_snapshot():
    """Test that all_metrics returns a snapshot of all providers."""
    fc = FeedbackCollector()
    fc.record(CompletionOutcome(provider="p1", latency_ms=100.0, success=True))
    fc.record(CompletionOutcome(provider="p2", latency_ms=200.0, success=True))

    all_m = fc.all_metrics()
    assert len(all_m) == 2
    assert "p1" in all_m
    assert "p2" in all_m
