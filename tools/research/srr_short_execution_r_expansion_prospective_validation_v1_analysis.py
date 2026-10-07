"""Read-only checkpoint analysis CLI for SRR SHORT execution-R expansion.

This script does not connect to PostgreSQL, does not write production DB, and
does not mutate registry, scanner, paper, or live state. It reads prospective
rows from an optional JSON export and delegates execution-R normalization and
path classification to ``app.research.prospective_execution_analysis``.

Expected input shape:

[
  {
    "observation_id": 1,
    "symbol": "BTCUSDT",
    "signal_time": "2026-10-07T01:00:00Z",
    "day": "2026-10-07",
    "variant_entry": 100.0,
    "invalidation_price": 100.5,
    "outcome": {
      "ambiguous_intrabar": false,
      "tp_before_sl": true,
      "sl_before_tp": false,
      "return_at_120m": 0.0
    }
  }
]

Only already-mature prospective observations should be exported. The evaluator
continues to own maturity and outcome creation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.research.prospective_execution_analysis import analyze_observations


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only SRR SHORT execution-R expansion prospective analysis",
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="JSON export of mature prospective_observation/prospective_outcome rows",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional local JSON output path; defaults to stdout",
    )
    parser.add_argument("--bootstrap-n", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20261007)
    args = parser.parse_args()

    rows = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise SystemExit("input must be a JSON list")

    result = analyze_observations(
        rows,
        bootstrap_n=args.bootstrap_n,
        seed=args.seed,
    )
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"WROTE: {args.output}")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
