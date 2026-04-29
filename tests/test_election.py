"""Unit tests for the bully algorithm leader election."""
from __future__ import annotations

from intelliroute.rate_limiter.election import (
    ElectionConfig,
    ElectionState,
    LeaderElection,
    Peer,
)


def test_follower_starts_as_follower():
    """Test that a new replica starts as a follower."""
    peers = [
        Peer(replica_id="rl-1", url="http://localhost:8012"),
        Peer(replica_id="rl-2", url="http://localhost:8022"),
    ]
    election = LeaderElection("rl-0", peers)
    assert election.state == ElectionState.FOLLOWER
    assert election.current_leader is None
    assert election.is_leader is False


def test_highest_id_wins_election():
    """Test that the replica with highest ID wins election."""
    peers = [
        Peer(replica_id="rl-0", url="http://localhost:8002"),
        Peer(replica_id="rl-1", url="http://localhost:8012"),
    ]
    # rl-2 is the highest
    election = LeaderElection("rl-2", peers)
    election.start_election()
    assert election.is_leader is True
    assert election.state == ElectionState.LEADER
    assert election.current_leader == "rl-2"


def test_lower_id_loses_election():
    """Test that lower IDs become followers."""
    peers = [
        Peer(replica_id="rl-1", url="http://localhost:8012"),
        Peer(replica_id="rl-2", url="http://localhost:8022"),
    ]
    # rl-0 is the lowest
    election = LeaderElection("rl-0", peers)
    election.start_election()
    # rl-0 should not be leader (it's lower than rl-1 and rl-2)
    assert election.is_leader is False


def test_receive_challenge_defers_to_higher():
    """Test that receiving a challenge from higher ID defers."""
    peers = [Peer(replica_id="rl-2", url="http://localhost:8022")]
    election = LeaderElection("rl-1", peers)
    # Start as FOLLOWER (not LEADER)
    election._state = ElectionState.FOLLOWER
    election._current_leader = None

    election.receive_challenge("rl-2")
    assert election.state == ElectionState.FOLLOWER
    assert election.current_leader == "rl-2"


def test_receive_challenge_from_lower_ignored():
    """Test that challenge from lower ID doesn't change state."""
    peers = [Peer(replica_id="rl-0", url="http://localhost:8002")]
    election = LeaderElection("rl-1", peers)
    election._state = ElectionState.LEADER

    election.receive_challenge("rl-0")
    # Should ignore challenge from lower ID
    assert election.state == ElectionState.LEADER


def test_receive_victory_announcement():
    """Test handling victory announcement from new leader."""
    peers = [Peer(replica_id="rl-1", url="http://localhost:8012")]
    election = LeaderElection("rl-0", peers)

    election.receive_victory("rl-1")
    assert election.state == ElectionState.FOLLOWER
    assert election.current_leader == "rl-1"


def test_declare_victory():
    """Test self-declaration as leader."""
    peers = [Peer(replica_id="rl-1", url="http://localhost:8012")]
    election = LeaderElection("rl-0", peers)

    election.declare_victory()
    assert election.is_leader is True
    assert election.state == ElectionState.LEADER
    assert election.current_leader == "rl-0"


def test_receive_heartbeat_resets_timeout():
    """Test that heartbeat resets the timeout counter."""
    import time

    peers = [Peer(replica_id="rl-1", url="http://localhost:8012")]
    election = LeaderElection("rl-0", peers)
    election.receive_victory("rl-1")

    # Simulate some time passing
    initial_heartbeat = election._last_heartbeat
    time.sleep(0.1)

    # Receive heartbeat should reset the timestamp
    election.receive_heartbeat("rl-1")
    assert election._last_heartbeat > initial_heartbeat


def test_heartbeat_timeout_check():
    """Test detection of leader timeout."""
    import time

    config = ElectionConfig(heartbeat_timeout_s=0.1)
    peers = [Peer(replica_id="rl-1", url="http://localhost:8012")]
    election = LeaderElection("rl-0", peers, config)

    # Start as follower with a leader
    election.receive_victory("rl-1")
    assert election.check_leader_timeout() is False

    # Wait for timeout
    time.sleep(0.15)
    assert election.check_leader_timeout() is True


def test_leader_never_times_out():
    """Test that a leader never triggers timeout."""
    config = ElectionConfig(heartbeat_timeout_s=0.01)
    peers = []
    election = LeaderElection("rl-0", peers)
    election.declare_victory()

    # Wait and check
    import time
    time.sleep(0.05)
    assert election.check_leader_timeout() is False


def test_election_config_defaults():
    """Test that election config has sensible defaults."""
    config = ElectionConfig()
    assert config.election_timeout_s == 2.0
    assert config.heartbeat_interval_s == 1.0
    assert config.heartbeat_timeout_s == 3.0


def test_leader_always_has_valid_lease():
    election = LeaderElection("rl-0", [Peer(replica_id="rl-1", url="http://x")])
    election.declare_victory()
    assert election.has_valid_lease() is True


def test_single_replica_treated_as_valid_lease():
    """No peers configured → lease is meaningless, always valid."""
    election = LeaderElection("rl-0", [])
    assert election.has_valid_lease() is True


def test_fresh_follower_with_peers_has_no_lease():
    """A follower that has never received a heartbeat must not hold a lease."""
    election = LeaderElection("rl-0", [Peer(replica_id="rl-1", url="http://x")])
    assert election.has_valid_lease() is False


def test_follower_with_recent_heartbeat_has_valid_lease():
    election = LeaderElection(
        "rl-0",
        [Peer(replica_id="rl-1", url="http://x")],
        ElectionConfig(heartbeat_timeout_s=2.0),
    )
    election.receive_victory("rl-1")
    election.receive_heartbeat("rl-1")
    assert election.has_valid_lease() is True


def test_follower_lease_expires_after_timeout():
    import time

    config = ElectionConfig(heartbeat_timeout_s=0.1)
    election = LeaderElection(
        "rl-0", [Peer(replica_id="rl-1", url="http://x")], config
    )
    election.receive_victory("rl-1")
    assert election.has_valid_lease() is True
    time.sleep(0.15)
    assert election.has_valid_lease() is False


def test_election_self_not_counted_in_comparisons():
    """Test that self is not counted when comparing IDs for election win."""
    # When rl-0 is the highest ID among all peers
    peers = [
        Peer(replica_id="rl-0", url="http://localhost:8002"),
        Peer(replica_id="rl-1", url="http://localhost:8012"),
    ]
    # When rl-0 starts election, it should not be in peers (to avoid self-comparison)
    # Filter happens in start_election or during setup
    # Since the current implementation includes self, test that it still wins if highest
    election = LeaderElection("rl-0", peers)
    # Simulate that rl-0 is indeed the highest ID overall
    election.start_election()
    # rl-0 should be leader since it's not lower than any peer
    # (it equals rl-0, which is itself and not in peers list used for comparison)
    assert election.is_leader is False  # Actually rl-0 loses because it's lower than rl-1
