"""Unit tests for the priority request queue."""
from __future__ import annotations

import asyncio

import pytest

from intelliroute.common.models import ChatMessage, CompletionRequest, Intent
from intelliroute.router.queue import (
    INTENT_PRIORITY,
    Priority,
    QueueConfig,
    RequestQueue,
)


def test_intent_priority_mapping():
    """Test that intent correctly maps to priority."""
    assert INTENT_PRIORITY[Intent.INTERACTIVE] == Priority.HIGH
    assert INTENT_PRIORITY[Intent.CODE] == Priority.HIGH
    assert INTENT_PRIORITY[Intent.REASONING] == Priority.MEDIUM
    assert INTENT_PRIORITY[Intent.BATCH] == Priority.LOW


def test_queue_enqueue_high_priority():
    """Test enqueuing a high-priority request."""
    q = RequestQueue()
    req = CompletionRequest(
        tenant_id="t1",
        messages=[ChatMessage(role="user", content="hi")],
    )
    success, queued, error = q.try_enqueue("req-1", req, Priority.HIGH)
    assert success is True
    assert queued is not None
    assert error == ""
    assert queued.priority == Priority.HIGH


def test_queue_enqueue_respects_max_depth():
    """Test that queue respects maximum depth limit."""
    config = QueueConfig(max_depth=2)
    q = RequestQueue(config)
    req = CompletionRequest(
        tenant_id="t1",
        messages=[ChatMessage(role="user", content="hi")],
    )

    # Enqueue 2 requests (HIGH doesn't go through queue)
    for i in range(2):
        success, _, _ = q.try_enqueue(f"req-{i}", req, Priority.MEDIUM)
        assert success is True

    # Third should fail
    success, _, error = q.try_enqueue("req-2", req, Priority.MEDIUM)
    assert success is False
    assert "full" in error.lower()


def test_queue_sheds_low_priority_at_threshold():
    """Test that low-priority requests are shed at shedding threshold."""
    config = QueueConfig(max_depth=100, shed_threshold=2)
    q = RequestQueue(config)
    req = CompletionRequest(
        tenant_id="t1",
        messages=[ChatMessage(role="user", content="hi")],
    )

    # Fill to shed_threshold with MEDIUM priority
    for i in range(2):
        success, _, _ = q.try_enqueue(f"req-{i}", req, Priority.MEDIUM)
        assert success is True

    # LOW priority should be shed
    success, _, error = q.try_enqueue("req-shed", req, Priority.LOW)
    assert success is False
    assert "full" in error.lower()


def test_queue_respects_max_low_priority_cap():
    """Test that low-priority requests are capped."""
    config = QueueConfig(max_depth=100, max_low_priority=2)
    q = RequestQueue(config)
    req = CompletionRequest(
        tenant_id="t1",
        messages=[ChatMessage(role="user", content="hi")],
    )

    # Enqueue 2 LOW priority
    for i in range(2):
        success, _, _ = q.try_enqueue(f"req-{i}", req, Priority.LOW)
        assert success is True

    # Third LOW priority should fail
    success, _, error = q.try_enqueue("req-2", req, Priority.LOW)
    assert success is False
    assert "low_priority_cap" in error.lower()


@pytest.mark.asyncio
async def test_queue_dequeue_respects_priority():
    """Test that dequeue pops highest priority first."""
    q = RequestQueue()
    req = CompletionRequest(
        tenant_id="t1",
        messages=[ChatMessage(role="user", content="hi")],
    )

    # Enqueue in reverse priority order
    _, low, _ = q.try_enqueue("low", req, Priority.LOW)
    _, med, _ = q.try_enqueue("med", req, Priority.MEDIUM)
    _, high, _ = q.try_enqueue("high", req, Priority.HIGH)

    # Should dequeue in priority order: HIGH first
    dequeued = await q.dequeue()
    assert dequeued is not None
    assert dequeued.request_id == "high"


@pytest.mark.asyncio
async def test_queue_dequeue_fifo_within_priority():
    """Test FIFO ordering within same priority level."""
    q = RequestQueue()
    req = CompletionRequest(
        tenant_id="t1",
        messages=[ChatMessage(role="user", content="hi")],
    )

    # Enqueue multiple MEDIUM priority
    _, first, _ = q.try_enqueue("first", req, Priority.MEDIUM)
    _, second, _ = q.try_enqueue("second", req, Priority.MEDIUM)
    _, third, _ = q.try_enqueue("third", req, Priority.MEDIUM)

    # Should dequeue in FIFO order
    d1 = await q.dequeue()
    assert d1.request_id == "first"
    d2 = await q.dequeue()
    assert d2.request_id == "second"
    d3 = await q.dequeue()
    assert d3.request_id == "third"


def test_queue_stats_initial_empty():
    """Test stats on empty queue."""
    q = RequestQueue()
    stats = q.stats()
    assert stats.total_depth == 0
    assert stats.by_priority == {"high": 0, "medium": 0, "low": 0}
    assert stats.shed_count == 0
    assert stats.timeout_count == 0


def test_queue_stats_tracks_by_priority():
    """Test that stats tracks queue depth by priority."""
    q = RequestQueue()
    req = CompletionRequest(
        tenant_id="t1",
        messages=[ChatMessage(role="user", content="hi")],
    )

    q.try_enqueue("m1", req, Priority.MEDIUM)
    q.try_enqueue("m2", req, Priority.MEDIUM)
    q.try_enqueue("l1", req, Priority.LOW)

    stats = q.stats()
    assert stats.total_depth == 3
    assert stats.by_priority["medium"] == 2
    assert stats.by_priority["low"] == 1


def test_queue_stats_tracks_shed_count():
    """Test that shed_count is incremented correctly."""
    config = QueueConfig(max_depth=2)
    q = RequestQueue(config)
    req = CompletionRequest(
        tenant_id="t1",
        messages=[ChatMessage(role="user", content="hi")],
    )

    # Fill and trigger shed
    q.try_enqueue("1", req, Priority.MEDIUM)
    q.try_enqueue("2", req, Priority.MEDIUM)
    q.try_enqueue("3", req, Priority.MEDIUM)  # Shed

    stats = q.stats()
    assert stats.shed_count >= 1


def test_queue_record_timeout():
    """Test that timeout is recorded."""
    q = RequestQueue()
    stats_before = q.stats()
    q.record_timeout("req-1")
    stats_after = q.stats()
    assert stats_after.timeout_count == stats_before.timeout_count + 1
