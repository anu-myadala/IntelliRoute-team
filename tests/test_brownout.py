"""Unit tests for brownout overload detection/state transitions."""
from __future__ import annotations

from intelliroute.router.brownout import BrownoutConfig, BrownoutManager


def _cfg() -> BrownoutConfig:
    return BrownoutConfig(
        enabled=True,
        queue_depth_enter=5,
        queue_depth_exit=2,
        p95_latency_ms_enter=300.0,
        p95_latency_ms_exit=120.0,
        error_rate_enter=0.5,
        error_rate_exit=0.2,
        timeout_rate_enter=0.5,
        timeout_rate_exit=0.2,
        sample_window=20,
        enter_consecutive=2,
        exit_consecutive=2,
        block_premium_for_medium_and_low=True,
        prefer_low_latency_for_medium_and_low=True,
        low_latency_max_ms=500,
        reduce_max_tokens_for_medium_and_low=True,
        degraded_max_tokens=120,
        drop_low_priority_when_degraded=False,
        delay_low_priority_ms=0,
    )


def test_enters_and_exits_with_hysteresis_on_queue_depth() -> None:
    m = BrownoutManager(config=_cfg())
    s, changed = m.evaluate(queue_depth=6)
    assert s.is_degraded is False
    assert changed is False
    s, changed = m.evaluate(queue_depth=6)
    assert s.is_degraded is True
    assert changed is True
    assert s.reason == "queue_depth"

    s, changed = m.evaluate(queue_depth=1)
    assert s.is_degraded is True
    assert changed is False
    s, changed = m.evaluate(queue_depth=1)
    assert s.is_degraded is False
    assert changed is True


def test_latency_can_trigger_degraded() -> None:
    m = BrownoutManager(config=_cfg())
    # p95 > enter threshold
    for _ in range(10):
        m.record_request_result(latency_ms=400, success=True)
    s, _ = m.evaluate(queue_depth=0)
    assert s.is_degraded is False
    s, changed = m.evaluate(queue_depth=0)
    assert changed is True
    assert s.is_degraded is True
    assert s.reason == "latency_p95"


def test_error_and_timeout_rates_recorded() -> None:
    m = BrownoutManager(config=_cfg())
    for _ in range(8):
        m.record_request_result(latency_ms=80, success=False, timed_out=True)
    for _ in range(2):
        m.record_request_result(latency_ms=80, success=True, timed_out=False)
    s, _ = m.evaluate(queue_depth=0)
    assert s.error_rate >= 0.7
    assert s.timeout_rate >= 0.7
