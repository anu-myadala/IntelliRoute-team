# Provider Quality Score

IntelliRoute exposes a lightweight derived `quality_score` per provider
for observability.

## What it is

- A heuristic score in `[0.0, 1.0]`
- Higher is better
- Intended for dashboards / debugging / provider comparison
- Not consumed by the routing policy directly — the policy uses the
  underlying `success_rate_ema` sub-score

## Formula

`quality_score = 0.75 * success_rate_ema + 0.25 * (1 - anomaly_score)`

Where:

- `success_rate_ema` is the provider's exponential moving average of
  successful completions
- `anomaly_score` is the EMA of the per-attempt anomaly signal
  (`0` good, `1` bad). It combines:
  - **Latency-band anomaly** — outside `[0.1×, 10×]` of the
    interactive baseline (100 ms)
  - **Hallucination signal** (`feedback.compute_hallucination_signal`):
    empty / near-empty responses, canned-refusal phrases (e.g.
    *"as an AI language model…"*, *"I cannot…"*, knowledge-cutoff
    disclaimers, excessive uncertainty hedges), and JSON-parse failures
    when the request expected JSON

The result is clamped to `[0, 1]`.

## Where it lives

- Implementation: `_compute_quality_score` in
  [`intelliroute/router/feedback.py`](../intelliroute/router/feedback.py)
- Refusal-phrase / anomaly heuristics: `_REFUSAL_PATTERNS` and
  `compute_hallucination_signal` in the same file
- Surfaced via `GET http://127.0.0.1:8001/feedback` alongside the rest
  of the EMA metrics

## Notes

- This is **not** a ground-truth quality evaluator or ML model.
- It is a simple, explainable control-plane metric for observability.
- The existing routing logic remains unchanged; this score is for
  clarity and diagnostics.
