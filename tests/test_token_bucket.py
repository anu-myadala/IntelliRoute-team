"""Unit tests for the token-bucket rate limiter."""
from __future__ import annotations

from intelliroute.rate_limiter.token_bucket import (
    BucketConfig,
    RateLimiterStore,
    TokenBucket,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_bucket_starts_full_on_first_use():
    bucket = TokenBucket(capacity=10, refill_rate=1)
    allowed, remaining, retry = bucket.try_consume(1, now=100.0)
    assert allowed is True
    assert remaining == 9
    assert retry == 0


def test_bucket_denies_when_empty_and_reports_retry_after():
    bucket = TokenBucket(capacity=2, refill_rate=1)  # 1 token/sec
    assert bucket.try_consume(1, now=0.0)[0] is True  # 1 remaining
    assert bucket.try_consume(1, now=0.0)[0] is True  # 0 remaining
    allowed, remaining, retry = bucket.try_consume(1, now=0.0)
    assert allowed is False
    assert remaining == 0
    # 1 token needed at 1 token/sec -> ~1000 ms
    assert 1000 <= retry <= 1100


def test_bucket_refills_linearly_over_time():
    bucket = TokenBucket(capacity=10, refill_rate=5)  # 5/sec
    # Drain it
    assert bucket.try_consume(10, now=0.0)[0] is True
    # 1 second later we should have 5 tokens.
    allowed, remaining, _ = bucket.try_consume(5, now=1.0)
    assert allowed is True
    assert abs(remaining - 0) < 1e-9


def test_bucket_capacity_cap():
    bucket = TokenBucket(capacity=10, refill_rate=5)
    assert bucket.try_consume(10, now=0.0)[0] is True
    # Wait way past the fill time; should cap at 10.
    allowed, remaining, _ = bucket.try_consume(0, now=100.0)
    assert allowed is True
    assert remaining == 10


def test_store_isolates_keys():
    clock = FakeClock()
    store = RateLimiterStore(
        default_config=BucketConfig(capacity=2, refill_rate=1),
        clock=clock,
    )
    # Tenant A drains its bucket
    assert store.try_consume("tenantA|p1")[0] is True
    assert store.try_consume("tenantA|p1")[0] is True
    assert store.try_consume("tenantA|p1")[0] is False
    # Tenant B is unaffected
    assert store.try_consume("tenantB|p1")[0] is True


def test_store_custom_config_per_key():
    clock = FakeClock()
    store = RateLimiterStore(
        default_config=BucketConfig(capacity=1, refill_rate=1),
        configs={"vip|p1": BucketConfig(capacity=100, refill_rate=50)},
        clock=clock,
    )
    # VIP can burst to 100
    for _ in range(100):
        assert store.try_consume("vip|p1")[0] is True
    assert store.try_consume("vip|p1")[0] is False


def test_store_logs_decisions_for_replication():
    clock = FakeClock()
    store = RateLimiterStore(
        default_config=BucketConfig(capacity=2, refill_rate=1),
        clock=clock,
    )
    store.try_consume("t|p")
    store.try_consume("t|p")
    store.try_consume("t|p")  # denied
    log = store.replication_log()
    assert len(log) == 3
    assert [entry[3] for entry in log] == [True, True, False]


def test_set_leader_changes_leader_id():
    """Test that set_leader updates the leader ID."""
    store = RateLimiterStore(default_config=BucketConfig(capacity=10, refill_rate=1))
    assert store.leader_id == "leader-0"
    store.set_leader("new-leader")
    assert store.leader_id == "new-leader"


def test_replay_log_entry_appends_to_log():
    """Test that replay_log_entry appends to the replication log."""
    store = RateLimiterStore(default_config=BucketConfig(capacity=10, refill_rate=1))
    initial_length = store.log_length()
    store.replay_log_entry(ts=100.0, key="t|p", amount=1.0, allowed=True)
    assert store.log_length() == initial_length + 1


def test_quota_hierarchy_exact_pair_wins():
    store = RateLimiterStore(default_config=BucketConfig(capacity=1, refill_rate=1))
    store.set_provider_default("p1", BucketConfig(capacity=5, refill_rate=1))
    store.set_tenant_default("t1", BucketConfig(capacity=10, refill_rate=1))
    store.set_tenant_provider_quota("t1", "p1", BucketConfig(capacity=99, refill_rate=1))
    cfg, source = store.resolve_config("t1|p1")
    assert cfg.capacity == 99
    assert source == "t1|p1"


def test_quota_hierarchy_tenant_default_beats_provider_default():
    store = RateLimiterStore(default_config=BucketConfig(capacity=1, refill_rate=1))
    store.set_provider_default("p1", BucketConfig(capacity=5, refill_rate=1))
    store.set_tenant_default("t1", BucketConfig(capacity=10, refill_rate=1))
    cfg, source = store.resolve_config("t1|p1")
    assert cfg.capacity == 10
    assert source == "t1|*"


def test_quota_hierarchy_provider_default_beats_global():
    store = RateLimiterStore(default_config=BucketConfig(capacity=1, refill_rate=1))
    store.set_provider_default("p1", BucketConfig(capacity=5, refill_rate=1))
    cfg, source = store.resolve_config("t-other|p1")
    assert cfg.capacity == 5
    assert source == "*|p1"


def test_quota_hierarchy_falls_through_to_default():
    store = RateLimiterStore(default_config=BucketConfig(capacity=3, refill_rate=1))
    cfg, source = store.resolve_config("anyone|anywhere")
    assert cfg.capacity == 3
    assert source == "*"


def test_set_tenant_default_invalidates_existing_buckets():
    """Bumping a tenant default rebuilds existing buckets under that tenant."""
    clock = FakeClock()
    store = RateLimiterStore(
        default_config=BucketConfig(capacity=2, refill_rate=1),
        clock=clock,
    )
    # Drain the bucket under the global default.
    assert store.try_consume("t1|p1")[0] is True
    assert store.try_consume("t1|p1")[0] is True
    assert store.try_consume("t1|p1")[0] is False
    # Raise the tenant default; the next call should see a fresh, larger bucket.
    store.set_tenant_default("t1", BucketConfig(capacity=10, refill_rate=1))
    allowed, remaining, _ = store.try_consume("t1|p1")
    assert allowed is True
    assert remaining == 9


def test_provider_default_does_not_leak_across_providers():
    store = RateLimiterStore(default_config=BucketConfig(capacity=1, refill_rate=1))
    store.set_provider_default("p1", BucketConfig(capacity=10, refill_rate=1))
    cfg_p1, _ = store.resolve_config("t1|p1")
    cfg_p2, src_p2 = store.resolve_config("t1|p2")
    assert cfg_p1.capacity == 10
    assert cfg_p2.capacity == 1
    assert src_p2 == "*"
