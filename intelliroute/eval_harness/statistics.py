from __future__ import annotations

import math
from statistics import mean, median


def mean_value(values: list[float]) -> float:
    return float(mean(values)) if values else 0.0


def median_value(values: list[float]) -> float:
    return float(median(values)) if values else 0.0


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    q = max(0.0, min(1.0, q))
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * q))
    idx = max(0, min(len(ordered) - 1, idx))
    return float(ordered[idx])


def p50(values: list[float]) -> float:
    return percentile(values, 0.50)


def p95(values: list[float]) -> float:
    return percentile(values, 0.95)


def p99(values: list[float]) -> float:
    return percentile(values, 0.99)


def std_dev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean_value(values)
    variance = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)
