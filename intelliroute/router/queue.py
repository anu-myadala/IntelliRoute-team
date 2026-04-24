"""Priority request queue with backpressure.

Manages enqueued completion requests with priority levels, shedding, and
timeout handling for burst load management.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

from ..common.models import CompletionRequest, Intent


class Priority(IntEnum):
    """Request priority levels (lower number = higher priority)."""

    HIGH = 0
    MEDIUM = 1
    LOW = 2


@dataclass
class QueueConfig:
    """Configuration for the request queue."""

    max_depth: int = 100
    max_low_priority: int = 50
    shed_threshold: int = 80
    timeout_ms: int = 30000


INTENT_PRIORITY: dict[Intent, Priority] = {
    Intent.INTERACTIVE: Priority.HIGH,
    Intent.CODE: Priority.HIGH,
    Intent.REASONING: Priority.MEDIUM,
    Intent.BATCH: Priority.LOW,
}


@dataclass
class QueuedRequest:
    """A request in the queue."""

    priority: Priority
    enqueued_at: float
    request_id: str
    request: CompletionRequest
    future: Optional[asyncio.Future[Any]] = None


@dataclass
class QueueStats:
    """Current queue statistics."""

    total_depth: int
    by_priority: dict[str, int] = field(default_factory=dict)
    shed_count: int = 0
    timeout_count: int = 0


class RequestQueue:
    """Thread-safe priority request queue with backpressure.

    HIGH priority requests bypass the queue. MEDIUM/LOW requests are enqueued
    with shedding and timeout handling.
    """

    def __init__(self, config: Optional[QueueConfig] = None) -> None:
        self._config = config or QueueConfig()
        self._queues: dict[Priority, list[QueuedRequest]] = {
            Priority.HIGH: [],
            Priority.MEDIUM: [],
            Priority.LOW: [],
        }
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._shed_count = 0
        self._timeout_count = 0

    @staticmethod
    def _maybe_create_future() -> Optional[asyncio.Future[Any]]:
        """Create a Future only when running inside an active event loop.

        Python 3.14 no longer provides an implicit current event loop in plain
        synchronous contexts. Queue tests construct and enqueue requests from
        synchronous code, so future creation must be deferred unless the caller
        is already inside an asyncio loop.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        return loop.create_future()

    def try_enqueue(
        self, request_id: str, req: CompletionRequest, priority: Priority
    ) -> tuple[bool, Optional[QueuedRequest], str]:
        """Attempt to enqueue a request.

        Returns
        -------
        tuple[bool, Optional[QueuedRequest], str]
            (success, queued_request if success else None, error_message)
        """
        with self._lock:
            total_depth = sum(len(q) for q in self._queues.values())
            low_count = len(self._queues[Priority.LOW])

            # Check shedding threshold
            if total_depth >= self._config.shed_threshold:
                # Shed low-priority requests
                if priority == Priority.LOW:
                    self._shed_count += 1
                    return (
                        False,
                        None,
                        f"queue_full (depth={total_depth}, threshold={self._config.shed_threshold})",
                    )

            # Check max low-priority cap
            if priority == Priority.LOW and low_count >= self._config.max_low_priority:
                self._shed_count += 1
                return (
                    False,
                    None,
                    f"low_priority_cap_exceeded ({low_count} >= {self._config.max_low_priority})",
                )

            # Check absolute max depth
            if total_depth >= self._config.max_depth:
                self._shed_count += 1
                return (
                    False,
                    None,
                    f"queue_full (depth={total_depth}, max={self._config.max_depth})",
                )

            # Enqueue
            queued = QueuedRequest(
                priority=priority,
                enqueued_at=time.monotonic(),
                request_id=request_id,
                request=req,
                future=self._maybe_create_future(),
            )
            self._queues[priority].append(queued)
            self._event.set()
            return True, queued, ""

    async def dequeue(self) -> Optional[QueuedRequest]:
        """Dequeue the next highest-priority request.

        Returns None if the queue is empty after a 1-second wait.
        """
        for _ in range(10):
            with self._lock:
                for priority in [Priority.HIGH, Priority.MEDIUM, Priority.LOW]:
                    q = self._queues[priority]
                    if q:
                        item = q.pop(0)
                        if not any(self._queues[p] for p in Priority):
                            self._event.clear()
                        return item
            if self._event.is_set():
                continue
            await asyncio.sleep(0.1)
        return None

    def record_timeout(self, request_id: str) -> None:
        """Record a timeout event."""
        with self._lock:
            self._timeout_count += 1

    def stats(self) -> QueueStats:
        """Return current queue statistics."""
        with self._lock:
            return QueueStats(
                total_depth=sum(len(q) for q in self._queues.values()),
                by_priority={
                    "high": len(self._queues[Priority.HIGH]),
                    "medium": len(self._queues[Priority.MEDIUM]),
                    "low": len(self._queues[Priority.LOW]),
                },
                shed_count=self._shed_count,
                timeout_count=self._timeout_count,
            )
