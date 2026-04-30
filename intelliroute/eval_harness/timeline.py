from __future__ import annotations

import threading
import time
from typing import Callable


class TimelineSampler:
    def __init__(
        self,
        *,
        interval_s: float,
        progress_fn: Callable[[], dict[str, float]],
        status_fn: Callable[[], dict[str, float]],
    ) -> None:
        self._interval_s = max(0.2, interval_s)
        self._progress_fn = progress_fn
        self._status_fn = status_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = time.monotonic()
        self._points: list[dict[str, float]] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.capture()

    def capture(self) -> None:
        p = self._progress_fn()
        s = self._status_fn()
        self._points.append(
            {
                "timestamp_sec": round(time.monotonic() - self._started, 3),
                "requests_completed": int(p.get("requests_completed", 0.0)),
                "avg_latency_ms": float(p.get("avg_latency_ms", 0.0)),
                "p95_latency_ms": float(p.get("p95_latency_ms", 0.0)),
                "error_rate": float(p.get("error_rate", 0.0)),
                "queue_depth": int(s.get("queue_depth", 0.0)),
                "brownout_active": int(bool(s.get("brownout_active", 0.0))),
                "active_breakers": int(s.get("active_breakers", 0.0)),
                "total_cost": float(p.get("total_cost", 0.0)),
                "requests_shed": int(s.get("requests_shed", 0.0)),
            }
        )

    def points(self) -> list[dict[str, float]]:
        return list(self._points)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.capture()
            self._stop.wait(self._interval_s)
