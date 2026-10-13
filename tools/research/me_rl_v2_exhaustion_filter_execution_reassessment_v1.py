"""READ-ONLY historical execution reassessment for ME Reverse Long V2.

Two diagnostic entry models:
A: signal-derived entry, hypothetical next-boundary fill.
B: next 5m candle open, preserving original absolute SL/TP.

No production database writes or trading actions.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


from app.research.me_r_long_early_mae_v2.candles import (
    ClosedCandle,
    validate_window,
)
from app.research.me_r_long_early_mae_v2.outcomes import baseline_outcome

EXPERIMENT = "ME_RL_V2_GENERIC_V1"
PREFIX = "ME_RL_V2_EXHAUSTION_FILTER_EXECUTION_REASSESSMENT_V1"
DEFAULT_OUTDIR = Path("docs/research")
FEE = 0.00055
NORMAL_SLIPPAGE = 0.00050
ELEVATED_SLIPPAGE = 0.00100
HORIZON = timedelta(minutes=240)

SOURCE_SQL = """
SELECT
    r.observation_id, r.symbol, r.signal_time,
    r.entry_zone_low, r.invalidation_price, r.target_1,
    r.features->>'exhaustion_magnitude' AS exhaustion,
    i.instrument_id, o.is_final
FROM research.research_observation r
JOIN dds.instrument i ON i.symbol = r.symbol
JOIN research.research_signal s
  ON s.observation_id = r.observation_id
 AND s.experiment_id = r.experiment_id
LEFT JOIN research.research_outcome o
  ON o.signal_id = s.signal_id
 AND o.experiment_id = r.experiment_id
WHERE r.experiment_id = %s
ORDER BY r.observation_id
"""

CANDLE_SQL = """
SELECT open_time, close_time, open, high, low, close,
       quality_status, source_received_at
FROM market.candle
WHERE instrument_id = %s
  AND exchange = 'bybit'
  AND market_type = 'linear'
  AND timeframe = '5'
  AND is_closed = TRUE
  AND open_time >= %s
  AND close_time <= %s
ORDER BY open_time, close_time
"""



def has_invalid_candles(rows):
    return any(
        str(r["quality_status"]).strip().lower() == "invalid"
        for r in rows
    )


def next_boundary(t: datetime) -> datetime:
    if t.tzinfo is None:
        raise ValueError("Naive signal timestamp")
    t = t.astimezone(timezone.utc)
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (t - midnight).total_seconds()
    intervals = int(elapsed // 300)
    boundary = midnight + timedelta(minutes=5 * intervals)
    if boundary < t:
        boundary += timedelta(minutes=5)
    return boundary


def classify(value):
    if value is None:
        return "MISSING"
    return "PASS" if float(value) <= 0.4 else "REJECT"


def net_r(gross, entry, exit_price, risk, slippage):
    cost = (FEE + slippage) * (entry + exit_price) / risk
    return gross - cost


def summarize(rows):
    values = [r["net_r_normal"] for r in rows]
    wins = sum(x for x in values if x > 0)
    losses = -sum(x for x in values if x < 0)
    return {
        "n": len(rows),
        "symbols": len({r["symbol"] for r in rows}),
        "days": len({r["signal_day"] for r in rows}),
        "gross_e_r": round(sum(r["gross_r"] for r in rows) / len(rows), 6) if rows else None,
        "net_e_r_normal": round(sum(values) / len(values), 6) if values else None,
        "net_e_r_elevated": round(sum(r["net_r_elevated"] for r in rows) / len(rows), 6) if rows else None,
        "net_pf_normal": round(wins / losses, 6) if losses else (None if not rows else "NO_LOSSES"),
        "total_net_r_normal": round(sum(values), 6),
        "win_rate_net": round(sum(v > 0 for v in values) / len(values), 6) if values else None,
        "exit_reasons": dict(Counter(r["exit_reason"] for r in rows)),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    outdir = args.outdir

    output_paths = [
        outdir / (PREFIX + suffix)
        for suffix in ("_TRADES.csv", "_EXCLUSIONS.csv", "_SUMMARY.json")
    ]

    if not args.overwrite:
        existing = [str(p) for p in output_paths if p.exists()]
        if existing:
            parser.error(
                "Existing research artifacts are protected: "
                + ", ".join(existing)
                + ". Use a new --outdir for reproduction."
            )

    import psycopg2
    from psycopg2.extras import RealDictCursor

    conn = psycopg2.connect(dbname="trad_bot")
    conn.set_session(readonly=True, autocommit=True)
    records = []
    exclusions = []
    counts = Counter()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(SOURCE_SQL, (EXPERIMENT,))
            sources = cur.fetchall()
            counts["source_n"] = len(sources)

            for source in sources:
                oid = source["observation_id"]
                group = classify(source["exhaustion"])
                start = next_boundary(source["signal_time"])
                end = start + HORIZON

                def exclude(reason, model="SOURCE"):
                    exclusions.append({
                        "observation_id": oid,
                        "symbol": source["symbol"],
                        "filter_group": group,
                        "model": model,
                        "reason": reason,
                    })
                    counts[reason] += 1

                if group == "MISSING":
                    exclude("MISSING_FEATURE")
                    continue
                if source["is_final"] is not True:
                    exclude("NOT_FINALIZED")
                    continue
                if any(source[k] is None for k in
                       ("entry_zone_low", "invalidation_price", "target_1")):
                    exclude("MISSING_GEOMETRY")
                    continue

                original = float(source["entry_zone_low"]) / 0.998
                stop = float(source["invalidation_price"])
                target = float(source["target_1"])
                if not (0 < stop < original < target):
                    exclude("INVALID_SOURCE_GEOMETRY")
                    continue

                sl_pct = (original - stop) / original
                tp_pct = (target - original) / original
                if abs(sl_pct - 0.025) > 1e-6 or abs(tp_pct - 0.03) > 1e-6:
                    exclude("SOURCE_GEOMETRY_MISMATCH")
                    continue

                cur.execute(CANDLE_SQL, (source["instrument_id"], start, end))
                raw = cur.fetchall()
                if has_invalid_candles(raw):
                    exclude("COVERAGE_INVALID_CANDLE_QUALITY")
                    continue

                candles = [
                    ClosedCandle(
                        open_at=r["open_time"],
                        close_at=r["close_time"],
                        open=float(r["open"]),
                        high=float(r["high"]),
                        low=float(r["low"]),
                        close=float(r["close"]),
                    )
                    for r in raw
                ]
                window, error = validate_window(
                    candles, start, end, expected_minutes=240
                )
                if error:
                    exclude("COVERAGE_" + error)
                    continue
                if len(window) != 48:
                    exclude("COVERAGE_NOT_48")
                    continue
                counts["full_windows"] += 1

                for model, entry in (
                    ("A_SIGNAL_REFERENCE", original),
                    ("B_NEXT_OPEN", window[0].open),
                ):
                    if not (0 < stop < entry < target):
                        exclude("ENTRY_OUTSIDE_LEVELS", model)
                        continue

                    outcome = baseline_outcome(
                        window, start, end, entry, stop, target
                    )
                    if outcome is None:
                        exclude("OUTCOME_UNAVAILABLE", model)
                        continue

                    risk = entry - stop
                    records.append({
                        "observation_id": oid,
                        "symbol": source["symbol"],
                        "signal_day": source["signal_time"].date().isoformat(),
                        "filter_group": group,
                        "model": model,
                        "entry": round(entry, 10),
                        "source_entry": round(original, 10),
                        "stop": round(stop, 10),
                        "target": round(target, 10),
                        "exit_price": round(outcome.exit_price, 10),
                        "exit_reason": outcome.exit_reason,
                        "ambiguous": outcome.ambiguous,
                        "gross_r": round(outcome.gross_r, 8),
                        "net_r_normal": round(net_r(
                            outcome.gross_r, entry, outcome.exit_price,
                            risk, NORMAL_SLIPPAGE
                        ), 8),
                        "net_r_elevated": round(net_r(
                            outcome.gross_r, entry, outcome.exit_price,
                            risk, ELEVATED_SLIPPAGE
                        ), 8),
                        "next_open_gap_pct": round(
                            100 * (window[0].open - original) / original, 6
                        ),
                    })
    finally:
        conn.close()

    outdir.mkdir(parents=True, exist_ok=True)
    data_path = outdir / (PREFIX + "_TRADES.csv")
    exclude_path = outdir / (PREFIX + "_EXCLUSIONS.csv")
    report_path = outdir / (PREFIX + "_SUMMARY.json")

    trade_fields = [
        "observation_id",
        "symbol",
        "signal_day",
        "filter_group",
        "model",
        "entry",
        "source_entry",
        "stop",
        "target",
        "exit_price",
        "exit_reason",
        "ambiguous",
        "gross_r",
        "net_r_normal",
        "net_r_elevated",
        "next_open_gap_pct",
    ]

    with data_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=trade_fields,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(records)

    with exclude_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["observation_id", "symbol", "filter_group", "model", "reason"], lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(exclusions)

    summary = {
        "experiment": PREFIX,
        "source_experiment": EXPERIMENT,
        "research_only": True,
        "historical_not_prospective": True,
        "source_n": counts["source_n"],
        "full_windows_after_finalization_filter": counts["full_windows"],
        "exclusion_counts": dict(Counter(e["reason"] for e in exclusions)),
        "costs": {
            "fee_per_side": FEE,
            "slippage_normal_per_side": NORMAL_SLIPPAGE,
            "slippage_elevated_per_side": ELEVATED_SLIPPAGE,
        },
        "limitations": [
            "Historical selection, not clean prospective OOS",
            "No proof of real next-open fill availability",
            "Full 240m coverage selection can bias the cohort",
            "No confidence intervals or cluster-robust uncertainty yet",
            "Backfill of missing historical candles not performed",
        ],
        "models": {},
    }
    for model in ("A_SIGNAL_REFERENCE", "B_NEXT_OPEN"):
        selected = [r for r in records if r["model"] == model]
        summary["models"][model] = {
            "CONTROL": summarize(selected),
            "PASS": summarize([r for r in selected if r["filter_group"] == "PASS"]),
            "REJECT": summarize([r for r in selected if r["filter_group"] == "REJECT"]),
        }

    report_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("TRADES:", data_path)
    print("EXCLUSIONS:", exclude_path)
    print("SUMMARY:", report_path)


if __name__ == "__main__":
    main()
