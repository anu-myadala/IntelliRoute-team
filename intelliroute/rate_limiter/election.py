"""Bully algorithm leader election for rate limiter replicas.

Implements a simple bully algorithm where the replica with the highest ID
always wins the election. Used to elect a single leader that owns the
authoritative rate limit state.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional


@dataclass
class Peer:
    """A peer replica in the cluster."""

    replica_id: str
    url: str


class ElectionState(str, Enum):
    """Leader election state machine."""

    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"


@dataclass
class ElectionConfig:
    """Configuration for leader election."""

    election_timeout_s: float = 2.0
    heartbeat_interval_s: float = 1.0
    heartbeat_timeout_s: float = 3.0


class LeaderElection:
    """Bully algorithm leader election.

    The replica with the highest replica_id always wins. Each replica
    periodically checks for a leader and initiates an election if the
    current leader becomes unavailable.
    """

    def __init__(
        self,
        replica_id: str,
        peers: list[Peer],
        config: Optional[ElectionConfig] = None,
    ) -> None:
        self._replica_id = replica_id
        self._peers = {p.replica_id: p for p in peers}
        self._config = config or ElectionConfig()
        self._state = ElectionState.FOLLOWER
        self._current_leader: Optional[str] = None
        self._last_heartbeat = 0.0
        self._election_start_time = 0.0

    def start_election(self) -> None:
        """Initiate an election if not already a leader.

        Compares IDs and declares victory if this replica has the highest ID.
        """
        self._state = ElectionState.CANDIDATE
        self._election_start_time = time.time()

        # Highest ID wins (bully algorithm)
        my_id = self._replica_id
        if all(my_id > peer_id for peer_id in self._peers.keys()):
            self.declare_victory()
        else:
            # Would send challenge to higher peers in real implementation
            pass

    def receive_challenge(self, challenger_id: str) -> None:
        """Handle an election challenge from another replica."""
        if self._state != ElectionState.LEADER:
            # Acknowledge and defer to challenger if it's higher
            if challenger_id > self._replica_id:
                self._state = ElectionState.FOLLOWER
                self._current_leader = challenger_id

    def receive_victory(self, leader_id: str) -> None:
        """Handle a victory announcement from a leader."""
        self._state = ElectionState.FOLLOWER
        self._current_leader = leader_id
        self._last_heartbeat = time.time()

    def declare_victory(self) -> None:
        """Declare this replica as the new leader."""
        self._state = ElectionState.LEADER
        self._current_leader = self._replica_id
        self._last_heartbeat = time.time()

    def receive_heartbeat(self, leader_id: str) -> None:
        """Handle a heartbeat from the current leader."""
        if leader_id == self._current_leader:
            self._last_heartbeat = time.time()
            self._state = ElectionState.FOLLOWER

    def check_leader_timeout(self) -> bool:
        """Check if the current leader has timed out.

        Returns True if a new election should be started.
        """
        if self._state == ElectionState.LEADER:
            return False

        elapsed = time.time() - self._last_heartbeat
        if elapsed > self._config.heartbeat_timeout_s:
            return True

        return False

    def has_valid_lease(self) -> bool:
        """Strong-consistency check: True if this replica may safely serve writes.

        Leaders always hold a valid lease. Followers are valid only while a
        fresh heartbeat from the leader is on file (within
        ``heartbeat_timeout_s``). Single-replica deployments — no peers
        configured — are treated as always-valid since there is no one to
        coordinate with. Used by the rate limiter to fail closed when
        ``RATE_LIMITER_STRONG_CONSISTENCY`` is enabled and the lease has
        expired, instead of silently falling back to local-only decisions.
        """
        if self._state == ElectionState.LEADER:
            return True
        if not self._peers:
            return True
        if self._last_heartbeat == 0.0:
            # We have peers but have never heard from a leader → no lease.
            return False
        elapsed = time.time() - self._last_heartbeat
        return elapsed < self._config.heartbeat_timeout_s

    @property
    def is_leader(self) -> bool:
        """Whether this replica is the current leader."""
        return self._state == ElectionState.LEADER

    @property
    def state(self) -> ElectionState:
        """Current election state."""
        return self._state

    @property
    def current_leader(self) -> Optional[str]:
        """Current leader replica ID, or None if unknown."""
        return self._current_leader
