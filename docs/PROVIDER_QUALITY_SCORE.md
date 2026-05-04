# Provider Quality Score

IntelliRoute exposes a lightweight derived `quality_score` per provider for observability.

## What it is

- A heuristic score in `[0.0, 1.0]`
- Higher is better
- Intended for dashboards/debugging and provider comparison

## Formula

`quality_score = 0.75 * success_rate_ema + 0.25 * (1 - anomaly_score)`

Where:

- `success_rate_ema` is the provider's exponential moving average success signal
- `anomaly_score` is the EMA anomaly/hallucination proxy (`0` good, `1` bad)

The result is clamped to `[0, 1]`.

## Notes

- This is **not** a ground-truth quality evaluator or ML model.
- It is a simple, explainable control-plane metric for observability.
- Existing routing logic remains unchanged; this score is primarily for clarity and diagnostics.
