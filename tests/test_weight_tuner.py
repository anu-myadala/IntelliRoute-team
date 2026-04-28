"""Unit tests for the closed-loop weight tuner."""
from __future__ import annotations

from intelliroute.common.models import Intent
from intelliroute.router.policy import RoutingPolicy, INTENT_WEIGHTS, Weights
from intelliroute.router.weight_tuner import WeightTuner


def _fresh_policy() -> RoutingPolicy:
    # Build a policy with a deep-copied weight table so cross-test mutation
    # cannot leak.
    weights = {
        intent: Weights(
            latency=w.latency,
            cost=w.cost,
            capability=w.capability,
            success=w.success,
        )
        for intent, w in INTENT_WEIGHTS.items()
    }
    return RoutingPolicy(intent_weights=weights)


def test_observe_accumulates_credit():
    policy = _fresh_policy()
    tuner = WeightTuner(policy, min_samples=5)
    sub = {"latency": 0.9, "cost": 0.1, "capability": 0.5, "success": 0.7}
    for _ in range(3):
        tuner.observe(Intent.INTERACTIVE, sub, success=True)
    snap = tuner.snapshot(Intent.INTERACTIVE)
    assert snap.samples == 3
    # Latency had the highest sub-score and all calls succeeded → it
    # accrues the most positive credit.
    assert snap.net_credit["latency"] > snap.net_credit["cost"]


def test_rebalance_below_min_samples_is_noop():
    policy = _fresh_policy()
    tuner = WeightTuner(policy, min_samples=10)
    tuner.observe(Intent.INTERACTIVE, {"latency": 1.0, "cost": 0, "capability": 0, "success": 0}, True)
    assert tuner.maybe_rebalance(Intent.INTERACTIVE) is False


def test_rebalance_shifts_weight_toward_winning_dimension():
    policy = _fresh_policy()
    before = Weights(
        latency=policy._weights[Intent.REASONING].latency,
        cost=policy._weights[Intent.REASONING].cost,
        capability=policy._weights[Intent.REASONING].capability,
        success=policy._weights[Intent.REASONING].success,
    )
    tuner = WeightTuner(policy, step=0.05, min_samples=5)
    # Consistent pattern: capability differentiated successful REASONING calls;
    # latency was the worst predictor.
    sub = {"latency": 0.1, "cost": 0.5, "capability": 0.9, "success": 0.5}
    for _ in range(6):
        tuner.observe(Intent.REASONING, sub, success=True)
    assert tuner.maybe_rebalance(Intent.REASONING) is True
    after = policy._weights[Intent.REASONING]
    assert after.capability > before.capability
    assert after.latency < before.latency
    # Step is bounded.
    assert abs((after.capability - before.capability) - 0.05) < 1e-9


def test_failure_blames_chosen_dimension():
    policy = _fresh_policy()
    tuner = WeightTuner(policy, min_samples=4)
    sub = {"latency": 0.9, "cost": 0.1, "capability": 0.1, "success": 0.5}
    for _ in range(5):
        tuner.observe(Intent.INTERACTIVE, sub, success=False)
    snap = tuner.snapshot(Intent.INTERACTIVE)
    # Latency was high in chosen provider but call failed → it should have
    # the most-negative credit.
    assert snap.net_credit["latency"] < snap.net_credit["cost"]


def test_rebalance_resets_window_after_application():
    policy = _fresh_policy()
    tuner = WeightTuner(policy, step=0.05, min_samples=3)
    sub = {"latency": 0.1, "cost": 0.9, "capability": 0.1, "success": 0.1}
    for _ in range(4):
        tuner.observe(Intent.BATCH, sub, success=True)
    assert tuner.maybe_rebalance(Intent.BATCH) is True
    snap = tuner.snapshot(Intent.BATCH)
    assert snap.samples == 0
    assert all(v == 0.0 for v in snap.net_credit.values())


def test_rebalance_respects_weight_floor():
    """Weights cannot drop below the per-dimension floor of 0.05."""
    policy = _fresh_policy()
    # Manually drive the cost weight down to the floor first.
    weights = policy._weights[Intent.INTERACTIVE]
    weights.cost = 0.05
    weights.latency = INTENT_WEIGHTS[Intent.INTERACTIVE].latency + (
        INTENT_WEIGHTS[Intent.INTERACTIVE].cost - 0.05
    )
    tuner = WeightTuner(policy, step=0.05, min_samples=3)
    sub = {"latency": 0.9, "cost": 0.1, "capability": 0.1, "success": 0.1}
    for _ in range(4):
        tuner.observe(Intent.INTERACTIVE, sub, success=True)
    # Worst dim is capability/success/cost (all near zero credit). Cost is
    # already at floor, so the tuner cannot draw from it further but may
    # still draw from another dim. The point is no weight dips below floor.
    tuner.maybe_rebalance(Intent.INTERACTIVE)
    w = policy._weights[Intent.INTERACTIVE]
    for v in (w.latency, w.cost, w.capability, w.success):
        assert v >= 0.05 - 1e-9
