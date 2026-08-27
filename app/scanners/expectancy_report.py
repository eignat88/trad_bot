"""Display measured expectancy reports from signal outcomes."""
from __future__ import annotations

import argparse
import logging

from app.db.repository import ScannerRepository

DIVIDER = "-" * 80


def _print_table(title: str, columns: list[str], rows: list[tuple]) -> None:
    if not rows:
        print(f"\n  {title}: no data yet\n")
        return
    print(f"\n  {title} ({len(rows)} rows)")
    print(f"  {DIVIDER}")
    header = "  ".join(f"{c:>14}" if c not in ("scanner_name", "symbol", "direction", "market_regime", "score_bucket") else f"{c:<28}" for c in columns)
    print(f"  {header}")
    print(f"  {DIVIDER}")
    for row in rows:
        vals = []
        for col, val in zip(columns, row):
            if col in ("scanner_name", "symbol", "direction", "market_regime", "score_bucket"):
                vals.append(f"{str(val or '-'):<28}")
            elif val is None:
                vals.append(f"{'-':>14}")
            elif isinstance(val, float):
                vals.append(f"{val:>14.4f}")
            else:
                vals.append(f"{str(val):>14}")
        print(f"  {'  '.join(vals)}")
    print()


def show_reports(*, limit: int = 30, backend: str = "postgres") -> None:
    repository = ScannerRepository(backend=backend)
    repository.ensure_schema()
    try:
        cursor = repository._conn.cursor() if repository._use_pg else None
        if cursor is None:
            print("PostgreSQL required for expectancy reports.")
            return

        # 1. Scanner expectancy
        cursor.execute(
            """SELECT scanner_name, direction, samples, entries, wins, losses,
                      avg_r, avg_r_after_costs, win_rate_on_entries, profit_factor
               FROM dds.scanner_expectancy LIMIT %s""",
            (limit,),
        )
        _print_table(
            "Scanner Expectancy",
            ["scanner_name", "direction", "samples", "entries", "wins", "losses",
             "avg_r", "avg_r_after_costs", "win_rate_on_entries", "profit_factor"],
            cursor.fetchall(),
        )

        # 2. Symbol expectancy
        cursor.execute(
            """SELECT scanner_name, symbol, direction, samples, entries,
                      avg_r, avg_r_after_costs, win_rate_on_entries
               FROM dds.scanner_symbol_expectancy LIMIT %s""",
            (limit,),
        )
        _print_table(
            "Symbol Expectancy (min 3 samples)",
            ["scanner_name", "symbol", "direction", "samples", "entries",
             "avg_r", "avg_r_after_costs", "win_rate_on_entries"],
            cursor.fetchall(),
        )

        # 3. Regime expectancy
        cursor.execute(
            """SELECT scanner_name, direction, market_regime, samples, entries,
                      avg_r, avg_r_after_costs, win_rate_on_entries
               FROM dds.scanner_regime_expectancy LIMIT %s""",
            (limit,),
        )
        _print_table(
            "Regime Expectancy (min 3 samples)",
            ["scanner_name", "direction", "market_regime", "samples", "entries",
             "avg_r", "avg_r_after_costs", "win_rate_on_entries"],
            cursor.fetchall(),
        )

        # 4. Score bucket expectancy
        cursor.execute(
            """SELECT scanner_name, direction, score_bucket, samples, entries,
                      avg_r, avg_r_after_costs, win_rate_on_entries
               FROM dds.score_bucket_expectancy LIMIT %s""",
            (limit,),
        )
        _print_table(
            "Score Bucket Expectancy (min 3 samples)",
            ["scanner_name", "direction", "score_bucket", "samples", "entries",
             "avg_r", "avg_r_after_costs", "win_rate_on_entries"],
            cursor.fetchall(),
        )

        # 5. Confluence expectancy
        cursor.execute(
            """SELECT scanner_name, direction, confluence_count, samples, entries,
                      avg_r, avg_r_after_costs, win_rate_on_entries
               FROM dds.scanner_confluence_expectancy LIMIT %s""",
            (limit,),
        )
        _print_table(
            "Scanner Confluence Expectancy (min 3 samples)",
            ["scanner_name", "direction", "confluence_count", "samples", "entries",
             "avg_r", "avg_r_after_costs", "win_rate_on_entries"],
            cursor.fetchall(),
        )
    finally:
        repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Display scanner expectancy reports")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    show_reports(limit=args.limit)


if __name__ == "__main__":
    main()
