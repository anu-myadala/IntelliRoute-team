"""Unit tests for the circuit breaker."""
from __future__ import annotations

from intelliroute.health_monitor.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)


class _Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _make_breaker(**kwargs) -> tuple[CircuitBreaker, _Clock]:
    clock = _Clock()
    breaker = CircuitBreaker(
        config=CircuitBreakerConfig(**kwargs),
        _clock=clock,  # type: ignore[arg-type]
    )
    return breaker, clock


def test_breaker_starts_closed_and_allows_requests():
    breaker, _ = _make_breaker()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.allow_request() is True


def test_breaker_opens_after_threshold_failures():
    breaker, clock = _make_breaker(failure_threshold=3, open_duration_s=5)
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.allow_request() is False


def test_breaker_transitions_to_half_open_after_cooldown():
    breaker, clock = _make_breaker(failure_threshold=2, open_duration_s=5)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN

    # Still within cooldown
    clock.now = 1.0
    assert breaker.allow_request() is False

    # Cooldown elapsed -> half open
    clock.now = 10.0
    assert breaker.allow_request() is True
    assert breaker.state == CircuitState.HALF_OPEN


def test_breaker_closes_after_successful_trials():
    breaker, clock = _make_breaker(
        failure_threshold=2, open_duration_s=5, half_open_success_required=2
    )
    breaker.record_failure()
    breaker.record_failure()
    clock.now = 10.0
    breaker.allow_request()  # transitions to HALF_OPEN
    breaker.record_success()
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED


def test_breaker_reopens_on_failure_during_half_open():
    breaker, clock = _make_breaker(
        failure_threshold=2, open_duration_s=5, half_open_success_required=3
    )
    breaker.record_failure()
    breaker.record_failure()
    clock.now = 10.0
    breaker.allow_request()  # HALF_OPEN
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN


def test_error_rate_reflects_sliding_window():
    breaker, _ = _make_breaker(failure_threshold=1000, window_size=4)
    breaker.record_success()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.error_rate() == 0.5
    breaker.record_failure()
    # Window dropped the oldest success; now 3 failures / 4.
    assert breaker.error_rate() == 0.75


# ---------------------------------------------------------------------------
# Additional edge-case and boundary tests
# ---------------------------------------------------------------------------

def test_breaker_stays_closed_one_below_threshold():
    """Exactly (failure_threshold - 1) failures must NOT open the circuit.

    The threshold is a strict minimum: the breaker trips only on the Nth
    failure, not the (N-1)th. Off-by-one errors in the comparison operator
    would cause premature circuit opens and unnecessary provider exclusions.
    """
    breaker, _ = _make_breaker(failure_threshold=4)
    for _ in range(3):  # one short of the threshold
        breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED
    # Confirm the breaker still grants access in the CLOSED state.
    assert breaker.allow_request() is True


def test_half_open_does_not_close_with_partial_successes():
    """With half_open_success_required=3, recording only 2 successes must keep
    the breaker in HALF_OPEN.

    Closing too early on partial evidence would re-expose a still-degraded
    provider to full traffic, defeating the purpose of the half-open probe.
    """
    breaker, clock = _make_breaker(
        failure_threshold=2, open_duration_s=5, half_open_success_required=3
    )
    breaker.record_failure()
    breaker.record_failure()
    clock.now = 10.0
    breaker.allow_request()  # transitions from OPEN to HALF_OPEN
    breaker.record_success()
    breaker.record_success()
    # Still one success short of the required 3 — must remain HALF_OPEN.
    assert breaker.state == CircuitState.HALF_OPEN


def test_failure_in_half_open_blocks_immediately():
    """A failure during HALF_OPEN must re-open the circuit and block the very
    next allow_request() call, even at the same clock timestamp.

    Without this guarantee a misbehaving provider could stay in HALF_OPEN
    indefinitely by interleaving one success with each failure.
    """
    breaker, clock = _make_breaker(
        failure_threshold=2, open_duration_s=10, half_open_success_required=2
    )
    breaker.record_failure()
    breaker.record_failure()
    clock.now = 15.0  # past the original cooldown
    breaker.allow_request()  # → HALF_OPEN
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    # The cooldown restarted at clock.now=15; the circuit must block at that same time.
    assert breaker.allow_request() is False


def test_error_rate_is_zero_on_fresh_breaker():
    """A brand-new breaker with no observations must report exactly 0.0 error rate.

    Division-by-zero guards must be in place before any events are recorded.
    """
    breaker, _ = _make_breaker()
    assert breaker.error_rate() == 0.0


def test_error_rate_is_zero_after_single_success():
    """One success with no failures must yield exactly 0.0 error rate."""
    breaker, _ = _make_breaker()
    breaker.record_success()
    assert breaker.error_rate() == 0.0


def test_consecutive_failures_resets_to_zero_after_close():
    """After the full open → half-open → closed lifecycle, consecutive_failures
    must be reset to 0 so a fresh failure window starts from scratch.

    Residual failure counts carried across state transitions would cause the
    breaker to re-open immediately on the very first post-recovery failure,
    making it impossible for a recovered provider to accumulate any credit.
    """
    breaker, clock = _make_breaker(
        failure_threshold=2, open_duration_s=5, half_open_success_required=1
    )
    breaker.record_failure()
    breaker.record_failure()  # breaker opens
    clock.now = 10.0
    breaker.allow_request()   # → HALF_OPEN
    breaker.record_success()  # → CLOSED
    assert breaker.state == CircuitState.CLOSED
    assert breaker.consecutive_failures == 0


# ---------------------------------------------------------------------------
# Further behavioral and window-semantic tests
# ---------------------------------------------------------------------------

def test_open_circuit_ignores_success_signals():
    """Recording successes while the circuit is OPEN must not transition it to
    HALF_OPEN or CLOSED; state changes are only triggered by allow_request()
    once the cooldown has elapsed.

    A buggy implementation that watches for successes in OPEN state could be
    tricked into closing prematurely by a provider that happens to serve one
    healthy response while still overall degraded.
    """
    breaker, clock = _make_breaker(failure_threshold=2, open_duration_s=30)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    # Push in several successes — none of these should flip the state.
    for _ in range(10):
        breaker.record_success()
    assert breaker.state == CircuitState.OPEN
    # Confirm that the cooldown still governs the transition.
    clock.now = 5.0
    assert breaker.allow_request() is False
    clock.now = 35.0
    assert breaker.allow_request() is True
    assert breaker.state == CircuitState.HALF_OPEN


def test_allow_request_stays_false_across_multiple_calls_when_open():
    """allow_request() must return False on every call while OPEN and within
    the cooldown window — not just the first call.

    Implementations that mutate state on the first False return and then
    allow subsequent callers through would bypass the circuit-breaker contract.
    """
    breaker, clock = _make_breaker(failure_threshold=1, open_duration_s=10)
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    clock.now = 0.0
    # Ten consecutive allow_request() calls must all be denied.
    for _ in range(10):
        assert breaker.allow_request() is False


def test_window_size_one_only_last_event_counts():
    """With window_size=1, the error_rate() should reflect only the most recent
    signal, discarding all earlier history.

    This validates the sliding-window eviction logic: after recording a failure
    then a success, the window holds only the success → error rate is 0.0.
    """
    breaker, _ = _make_breaker(failure_threshold=1000, window_size=1)
    breaker.record_failure()
    assert breaker.error_rate() == 1.0   # window = [failure]
    breaker.record_success()
    assert breaker.error_rate() == 0.0   # window = [success] — failure evicted


def test_error_rate_full_window_of_failures_is_one():
    """Filling the entire sliding window with failures must produce exactly 1.0."""
    window = 6
    breaker, _ = _make_breaker(failure_threshold=1000, window_size=window)
    for _ in range(window):
        breaker.record_failure()
    assert breaker.error_rate() == 1.0


def test_fresh_failures_reopen_after_full_recovery():
    """After a complete open → half-open → closed recovery, a new burst of
    failures must reopen the circuit just as it would from the initial state.

    This guards against implementations that permanently lower the effective
    threshold after a recovery, making the breaker harder to trip the second
    time around and masking a recurring provider instability.
    """
    breaker, clock = _make_breaker(
        failure_threshold=2, open_duration_s=5, half_open_success_required=1
    )
    # First trip
    breaker.record_failure()
    breaker.record_failure()
    clock.now = 10.0
    breaker.allow_request()   # → HALF_OPEN
    breaker.record_success()  # → CLOSED
    assert breaker.state == CircuitState.CLOSED

    # Second burst — must trip again with the same threshold.
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.allow_request() is False
