"""Brownout mode manager for graceful degradation under overload."""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class BrownoutConfig:
    enabled: bool
    queue_depth_enter: int
    queue_depth_exit: int
    p95_latency_ms_enter: float
    p95_latency_ms_exit: float
    error_rate_enter: float
    error_rate_exit: float
    timeout_rate_enter: float
    timeout_rate_exit: float
    sample_window: int
    enter_consecutive: int
    exit_consecutive: int
    block_premium_for_medium_and_low: bool
    prefer_low_latency_for_medium_and_low: bool
    low_latency_max_ms: int
    reduce_max_tokens_for_medium_and_low: bool
    degraded_max_tokens: int
    drop_low_priority_when_degraded: bool
    delay_low_priority_ms: int

    @classmethod
    def from_env(cls) -> "BrownoutConfig":
        return cls(
            enabled=_env_bool("INTELLIROUTE_BROWNOUT_ENABLED", True),
            queue_depth_enter=_env_int("INTELLIROUTE_BROWNOUT_QUEUE_ENTER", 60),
            queue_depth_exit=_env_int("INTELLIROUTE_BROWNOUT_QUEUE_EXIT", 30),
            p95_latency_ms_enter=_env_float("INTELLIROUTE_BROWNOUT_P95_ENTER_MS", 1400.0),
            p95_latency_ms_exit=_env_float("INTELLIROUTE_BROWNOUT_P95_EXIT_MS", 900.0),
            error_rate_enter=_env_float("INTELLIROUTE_BROWNOUT_ERROR_ENTER", 0.25),
            error_rate_exit=_env_float("INTELLIROUTE_BROWNOUT_ERROR_EXIT", 0.10),
            timeout_rate_enter=_env_float("INTELLIROUTE_BROWNOUT_TIMEOUT_ENTER", 0.20),
            timeout_rate_exit=_env_float("INTELLIROUTE_BROWNOUT_TIMEOUT_EXIT", 0.08),
            sample_window=_env_int("INTELLIROUTE_BROWNOUT_SAMPLE_WINDOW", 50),
            enter_consecutive=_env_int("INTELLIROUTE_BROWNOUT_ENTER_CONSEC", 2),
            exit_consecutive=_env_int("INTELLIROUTE_BROWNOUT_EXIT_CONSEC", 3),
            block_premium_for_medium_and_low=_env_bool(
                "INTELLIROUTE_BROWNOUT_BLOCK_PREMIUM", True
            ),
            prefer_low_latency_for_medium_and_low=_env_bool(
                "INTELLIROUTE_BROWNOUT_PREFER_LOW_LATENCY", True
            ),
            low_latency_max_ms=_env_int("INTELLIROUTE_BROWNOUT_LOW_LATENCY_MAX_MS", 700),
            reduce_max_tokens_for_medium_and_low=_env_bool(
                "INTELLIROUTE_BROWNOUT_REDUCE_MAX_TOKENS", True
            ),
            degraded_max_tokens=_env_int("INTELLIROUTE_BROWNOUT_MAX_TOKENS", 120),
            drop_low_priority_when_degraded=_env_bool(
                "INTELLIROUTE_BROWNOUT_DROP_LOW_PRIORITY", False
            ),
            delay_low_priority_ms=_env_int("INTELLIROUTE_BROWNOUT_DELAY_LOW_MS", 0),
        )


@dataclass(frozen=True)
class BrownoutSnapshot:
    is_degraded: bool
    reason: str
    entered_at_unix: float | None
    queue_depth: int
    p95_latency_ms: float
    error_rate: float
    timeout_rate: float


@dataclass
class BrownoutManager:
    config: BrownoutConfig = field(default_factory=BrownoutConfig.from_env)
    _degraded: bool = False
    _reason: str = ""
    _entered_at_unix: float | None = None
    _enter_streak: int = 0
    _exit_streak: int = 0
    _latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=50))
    _successes: deque[bool] = field(default_factory=lambda: deque(maxlen=50))
    _timeouts: deque[bool] = field(default_factory=lambda: deque(maxlen=50))
    _queue_depth: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        maxlen = max(5, self.config.sample_window)
        self._latencies_ms = deque(maxlen=maxlen)
        self._successes = deque(maxlen=maxlen)
        self._timeouts = deque(maxlen=maxlen)

    def record_request_result(
        self, *, latency_ms: float, success: bool, timed_out: bool = False
    ) -> None:
        with self._lock:
            if latency_ms > 0:
                self._latencies_ms.append(float(latency_ms))
            self._successes.append(bool(success))
            self._timeouts.append(bool(timed_out))

    def evaluate(self, queue_depth: int) -> tuple[BrownoutSnapshot, bool]:
        """Update state machine and return (snapshot, transition_occurred)."""
        with self._lock:
            self._queue_depth = max(0, int(queue_depth))
            p95 = self._p95_latency_locked()
            err = self._error_rate_locked()
            to_rate = self._timeout_rate_locked()

            overloaded, reason = self._is_overloaded_locked(p95, err, to_rate)
            changed = False
            now = time.time()

            if overloaded:
                self._enter_streak += 1
                self._exit_streak = 0
                if (
                    not self._degraded
                    and self._enter_streak >= max(1, self.config.enter_consecutive)
                ):
                    self._degraded = True
                    self._reason = reason
                    self._entered_at_unix = now
                    changed = True
                elif self._degraded:
                    self._reason = reason
            else:
                self._exit_streak += 1
                self._enter_streak = 0
                if (
                    self._degraded
                    and self._exit_streak >= max(1, self.config.exit_consecutive)
                ):
                    self._degraded = False
                    self._reason = "healthy"
                    self._entered_at_unix = None
                    changed = True

            return (
                BrownoutSnapshot(
                    is_degraded=self._degraded,
                    reason=self._reason if self._degraded else "healthy",
                    entered_at_unix=self._entered_at_unix,
                    queue_depth=self._queue_depth,
                    p95_latency_ms=round(p95, 2),
                    error_rate=round(err, 4),
                    timeout_rate=round(to_rate, 4),
                ),
                changed,
            )

    def snapshot(self) -> BrownoutSnapshot:
        with self._lock:
            p95 = self._p95_latency_locked()
            err = self._error_rate_locked()
            to_rate = self._timeout_rate_locked()
            return BrownoutSnapshot(
                is_degraded=self._degraded,
                reason=self._reason if self._degraded else "healthy",
                entered_at_unix=self._entered_at_unix,
                queue_depth=self._queue_depth,
                p95_latency_ms=round(p95, 2),
                error_rate=round(err, 4),
                timeout_rate=round(to_rate, 4),
            )

    def metrics(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "sample_count": len(self._latencies_ms),
                "p50_latency_ms": round(self._percentile_locked(0.50), 2),
                "p95_latency_ms": round(self._percentile_locked(0.95), 2),
                "error_rate": round(self._error_rate_locked(), 4),
                "timeout_rate": round(self._timeout_rate_locked(), 4),
            }

    def _is_overloaded_locked(
        self, p95_latency_ms: float, error_rate: float, timeout_rate: float
    ) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "disabled"
        if self._degraded:
            if self._queue_depth > self.config.queue_depth_exit:
                return True, "queue_depth"
            if p95_latency_ms > self.config.p95_latency_ms_exit:
                return True, "latency_p95"
            if error_rate > self.config.error_rate_exit:
                return True, "error_rate"
            if timeout_rate > self.config.timeout_rate_exit:
                return True, "timeout_rate"
            return False, "healthy"

        if self._queue_depth >= self.config.queue_depth_enter:
            return True, "queue_depth"
        if p95_latency_ms >= self.config.p95_latency_ms_enter:
            return True, "latency_p95"
        if error_rate >= self.config.error_rate_enter:
            return True, "error_rate"
        if timeout_rate >= self.config.timeout_rate_enter:
            return True, "timeout_rate"
        return False, "healthy"

    def _p95_latency_locked(self) -> float:
        return self._percentile_locked(0.95)

    def _percentile_locked(self, q: float) -> float:
        if not self._latencies_ms:
            return 0.0
        vals = sorted(self._latencies_ms)
        q = min(1.0, max(0.0, q))
        idx = max(0, int(round(q * (len(vals) - 1))))
        return vals[idx]

    def _error_rate_locked(self) -> float:
        if not self._successes:
            return 0.0
        failures = sum(1 for ok in self._successes if not ok)
        return failures / len(self._successes)

    def _timeout_rate_locked(self) -> float:
        if not self._timeouts:
            return 0.0
        count = sum(1 for t in self._timeouts if t)
        return count / len(self._timeouts)
