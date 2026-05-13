"""Closed-loop tuner for the multi-objective intent weights.

The static weights in :mod:`intelliroute.router.policy` encode a guess at
how much each intent should care about latency vs cost vs capability vs
success. In practice the right answer drifts as providers change pricing,
add capacity, or develop new failure modes.

``WeightTuner`` watches completed routing decisions and nudges the weights
toward whichever sub-score actually predicted success. Each observation
attributes credit (on success) or blame (on failure) to each dimension in
proportion to how much it differentiated the chosen provider. After enough
samples accumulate for an intent, the tuner shifts a small bounded ``step``
of weight from the worst-performing dimension to the best-performing one
and renormalises.

The tuner mutates the policy's weight table in place. The policy itself
remains static and side-effect free; the tuner is the only writer.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass

from ..common.models import Intent
from .policy import RoutingPolicy, Weights


_DIMENSIONS = ("latency", "cost", "capability", "success")
_MIN_WEIGHT = 0.05
_MAX_WEIGHT = 0.85


@dataclass
class TunerSnapshot:
    samples: int
    net_credit: dict[str, float]


class WeightTuner:
    """Adjusts ``RoutingPolicy`` intent weights based on observed outcomes.

    Parameters
    ----------
    policy
        The policy whose weights table will be rebalanced in place.
    step
        Maximum amount of weight shifted between dimensions per rebalance.
    min_samples
        Number of observations per intent required before the first
        rebalance fires for that intent.
    """

    def __init__(
        self,
        policy: RoutingPolicy,
        step: float = 0.05,
        min_samples: int = 20,
    ) -> None:
        self._policy = policy
        self._step = step
        self._min_samples = min_samples
        self._credit: dict[Intent, dict[str, float]] = defaultdict(
            lambda: {d: 0.0 for d in _DIMENSIONS}
        )
        self._samples: dict[Intent, int] = defaultdict(int)
        self._lock = threading.Lock()
        self._baseline_weights: dict[Intent, Weights] = {
            intent: Weights(
                latency=weights.latency,
                cost=weights.cost,
                capability=weights.capability,
                success=weights.success,
            )
            for intent, weights in self._policy._weights.items()
        }

    def observe(
        self,
        intent: Intent,
        chosen_sub_scores: dict[str, float],
        success: bool,
    ) -> None:
        """Attribute the outcome to each dimension proportional to its sub-score."""
        sign = 1.0 if success else -1.0
        with self._lock:
            credit = self._credit[intent]
            for dim in _DIMENSIONS:
                credit[dim] += sign * float(chosen_sub_scores.get(dim, 0.0))
            self._samples[intent] += 1

    def maybe_rebalance(self, intent: Intent) -> bool:
        """If enough samples have accumulated, shift weight toward the best dim.

        Returns True if a rebalance was applied.
        """
        with self._lock:
            if self._samples[intent] < self._min_samples:
                return False
            credit = dict(self._credit[intent])
            # Reset accumulators after consuming the window.
            self._credit[intent] = {d: 0.0 for d in _DIMENSIONS}
            self._samples[intent] = 0

        weights = self._policy._weights.get(intent)
        if weights is None:
            return False

        # Try to shift toward the highest-credit dim. If the lowest-credit dim
        # is already at the floor, fall back to the next-lowest dim that has
        # headroom; otherwise the rebalance is a no-op.
        ranked_dims = sorted(_DIMENSIONS, key=lambda d: credit[d])
        best_dim = ranked_dims[-1]
        if all(credit[d] == credit[best_dim] for d in _DIMENSIONS):
            return False
        for worst_dim in ranked_dims:
            if worst_dim == best_dim or credit[best_dim] <= credit[worst_dim]:
                continue
            if self._shift_weights(weights, best_dim, worst_dim, self._step):
                return True
        return False

    @staticmethod
    def _shift_weights(weights: Weights, best: str, worst: str, step: float) -> bool:
        current = {
            "latency": weights.latency,
            "cost": weights.cost,
            "capability": weights.capability,
            "success": weights.success,
        }
        # How much we can actually move without violating the per-dimension floor/ceiling.
        movable = min(step, current[worst] - _MIN_WEIGHT, _MAX_WEIGHT - current[best])
        if movable <= 0:
            return False
        current[worst] -= movable
        current[best] += movable
        weights.latency = current["latency"]
        weights.cost = current["cost"]
        weights.capability = current["capability"]
        weights.success = current["success"]
        return True

    def snapshot(self, intent: Intent) -> TunerSnapshot:
        with self._lock:
            return TunerSnapshot(
                samples=self._samples[intent],
                net_credit=dict(self._credit[intent]),
            )

    def reset(self, *, reset_policy_weights: bool = True) -> None:
        with self._lock:
            self._credit.clear()
            self._samples.clear()
            if reset_policy_weights:
                for intent, baseline in self._baseline_weights.items():
                    if intent in self._policy._weights:
                        target = self._policy._weights[intent]
                        target.latency = baseline.latency
                        target.cost = baseline.cost
                        target.capability = baseline.capability
                        target.success = baseline.success
