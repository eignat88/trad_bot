from __future__ import annotations

import csv
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tools.research.me_rl_v1_entry_geometry_counterfactual_audit_v1 import (
    CANDLE_MS,
    _costs,
    load_observations,
)
from tools.research.me_rl_v1_diagnostic_replay_runner import load_candles

ROOT = Path("data/research/me_rl_v1_geometry")
OUTPUT = ROOT / "execution_sensitivity_v1.csv"
REPORT = ROOT / "execution_sensitivity_v1.json"

STOP_PCT = 0.025
TARGET_PCT = 0.03

def main():
    if OUTPUT.exists() or REPORT.exists():
        raise RuntimeError("Output exists; refusing overwrite")

    observations = load_observations(ROOT / "observations_802.csv")
    by_symbol = load_candles()
    records = []

    for obs in observations:
        signal_ms = int(
            obs.signal_time.astimezone(timezone.utc).timestamp() * 1000
        )
        bucket = signal_ms // CANDLE_MS * CANDLE_MS
        candles = {
            c.timestamp: c for c in by_symbol.get(obs.symbol, [])
        }

        source_price = obs.target_1 / 1.03
        first_open_ms = bucket + CANDLE_MS

        path = [
            candles.get(first_open_ms + i * CANDLE_MS)
            for i in range(48)
        ]

        record = {
            "observation_id": obs.observation_id,
            "symbol": obs.symbol,
            "signal_time": obs.signal_time.isoformat(),
            "source_price": source_price,
            "next_open": None,
            "entry_gap_pct": None,
            "status": "INCOMPLETE",
            "exit_type": None,
            "gross_r": None,
            "net_r": None,
            "elevated_net_r": None,
            "reason_code": None,
            "execution_status": "NEXT_BAR_OPEN_PROXY",
        }

        if any(c is None for c in path):
            record["reason_code"] = "MISSING_48_CANDLE_PATH"
            records.append(record)
            continue

        if any(c.validate() is not None for c in path):
            record["reason_code"] = "INVALID_PATH_OHLC"
            records.append(record)
            continue

        entry = path[0].open
        stop = entry * (1 - STOP_PCT)
        target = entry * (1 + TARGET_PCT)

        record["next_open"] = entry
        record["entry_gap_pct"] = (entry / source_price - 1) * 100

        exit_type = "TIMEOUT"
        exit_price = path[-1].close

        for candle in path:
            tp_hit = candle.high >= target
            sl_hit = candle.low <= stop

            if tp_hit and sl_hit:
                exit_type = "INTRABAR_AMBIGUOUS"
                exit_price = stop
                break

            if sl_hit:
                exit_type = "SL_FIRST"
                exit_price = stop
                break

            if tp_hit:
                exit_type = "TP_FIRST"
                exit_price = target
                break

        gross_return_pct = (exit_price / entry - 1) * 100
        gross_r = gross_return_pct / (STOP_PCT * 100)

        costs = _costs(
            gross_return_pct,
            STOP_PCT * 100,
        )

        record.update({
            "status": "FINALIZED",
            "exit_type": exit_type,
            "gross_r": gross_r,
            "net_r": costs["fully_net_r"],
            "elevated_net_r": costs["elevated_net_r"],
        })
        records.append(record)

    finalized = [r for r in records if r["status"] == "FINALIZED"]
    values = [r["net_r"] for r in finalized]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]

    report = {
        "experiment": "ME_RL_V1_EXECUTION_SENSITIVITY_V1",
        "model": "V1_NEXT_BAR_OPEN_CF",
        "cohort_n": len(records),
        "finalized_n": len(finalized),
        "incomplete_n": len(records) - len(finalized),
        "net_e_r": statistics.mean(values) if values else None,
        "gross_e_r": statistics.mean(
            r["gross_r"] for r in finalized
        ) if finalized else None,
        "elevated_net_e_r": statistics.mean(
            r["elevated_net_r"] for r in finalized
        ) if finalized else None,
        "profit_factor": (
            sum(wins) / abs(sum(losses))
            if wins and losses and sum(losses) != 0
            else None
        ),
        "win_rate": len(wins) / len(values) if values else None,
        "exit_types": dict(Counter(r["exit_type"] for r in finalized)),
        "reason_codes": dict(Counter(
            r["reason_code"] for r in records if r["reason_code"]
        )),
        "execution_verdict": "DIAGNOSTIC_ONLY",
        "limitation": (
            "Next-bar open is a historical execution proxy, "
            "not evidence of achievable real-time fill."
        ),
    }

    with OUTPUT.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=list(records[0].keys())
        )
        writer.writeheader()
        writer.writerows(records)

    REPORT.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(report, indent=2))
    print("OUTPUT:", OUTPUT)
    print("REPORT:", REPORT)

if __name__ == "__main__":
    main()
