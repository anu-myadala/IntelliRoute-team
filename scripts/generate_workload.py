from __future__ import annotations

import argparse
from pathlib import Path

from intelliroute.eval_harness import SCENARIOS, generate_workload, write_workload_jsonl


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate deterministic IntelliRoute replay workloads.")
    p.add_argument("--scenario", choices=SCENARIOS, required=True)
    p.add_argument("--size", type=int, default=60)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", type=Path, default=Path("eval_results/workload.jsonl"))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    rows = generate_workload(args.scenario, args.size, seed=args.seed)
    write_workload_jsonl(args.out, rows)
    print(f"generated {len(rows)} requests -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
