from __future__ import annotations

import csv
import json
from collections import defaultdict, Counter
from dataclasses import asdict, replace
from datetime import timezone
from pathlib import Path

from tools.research.me_rl_v1_entry_geometry_counterfactual_audit_v1 import (
    Candle,
    CANDLE_MS,
    _evaluate_path,
    _future_window,
    _geometry,
    load_observations,
    summarize,
)

ROOT = Path("data/research/me_rl_v1_geometry")
OBS_FILE = ROOT / "observations_802.csv"
CANDLE_FILE = ROOT / "bybit_5m_802.csv"
OUTCOMES_FILE = ROOT / "diagnostic_replay_outcomes.csv"
REPORT_FILE = ROOT / "diagnostic_replay_summary.json"

MODELS = (
    "V1_AS_RECORDED",
    "V1_CLOSE_ANCHORED_CF",
    "V1_SOURCE_PRICE_ANCHORED_CF",
)


def load_candles():
    result = defaultdict(list)
    with CANDLE_FILE.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            result[row["symbol"]].append(
                Candle(
                    timestamp=int(row["open_ms"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            )
    for values in result.values():
        values.sort(key=lambda c: c.timestamp)
    return result


def calculate(obs, model, candles):
    signal_ms = int(obs.signal_time.astimezone(timezone.utc).timestamp() * 1000)
    bucket = signal_ms // CANDLE_MS * CANDLE_MS

    candle_by_time = {c.timestamp: c for c in candles}
    current = candle_by_time.get(bucket)

    source_price = (
        obs.target_1 / 1.03
        if obs.target_1 is not None and obs.target_1 > 0
        else None
    )

    if model == "V1_AS_RECORDED":
        entry, stop, target = (
            obs.reference_price,
            obs.invalidation_price,
            obs.target_1,
        )
        execution_status = "FILL_UNVERIFIED"

    elif model == "V1_CLOSE_ANCHORED_CF":
        entry = current.close if current else None
        stop = entry * 0.975 if entry else None
        target = entry * 1.03 if entry else None
        execution_status = "TIMING_UNVERIFIED"

    else:
        entry = source_price
        stop = entry * 0.975 if entry else None
        target = entry * 1.03 if entry else None
        execution_status = "FILL_UNVERIFIED"

    base = {
        "model": model,
        "observation_id": obs.observation_id,
        "setup_id": obs.setup_id,
        "symbol": obs.symbol,
        "signal_time": obs.signal_time.isoformat(),
        "signal_bucket_open_ms": bucket,
        "execution_status": execution_status,
        "source_price": source_price,
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "status": "INVALID",
        "reason_code": None,
        "coverage_complete": False,
        "exit_type": None,
        "gross_r": None,
        "fully_net_r": None,
        "elevated_net_r": None,
        "fee_adjusted_r": None,
    }

    if current is None or current.validate() is not None:
        base["reason_code"] = "CURRENT_CANDLE_SOURCE_INVALID"
        return base, None

    if not all(
        x is not None and x > 0 for x in (entry, stop, target)
    ):
        base["reason_code"] = "INVALID_ENTRY_LEVEL"
        return base, None

    if not stop < entry < target:
        base["reason_code"] = "INVALID_RECORDED_LONG_GEOMETRY"
        return base, None

    # Use the current candle as the anchor only.
    # Trading-path evaluation starts from bucket + 5 minutes.
    selected = [
        c for c in candles
        if bucket <= c.timestamp <= bucket + 48 * CANDLE_MS
    ]
    path, complete, reason = _future_window(selected, bucket)

    geometry = _geometry(entry, stop, target, current.close)
    geometry["signal_close"] = current.close

    anchored_obs = replace(obs, signal_candle_open_time=bucket)

    outcome = _evaluate_path(
        model=model,
        obs=anchored_obs,
        geometry=geometry,
        path=path,
        complete=complete,
        reason_code=reason,
    )

    base.update({
        "status": outcome.status,
        "reason_code": outcome.reason_code,
        "coverage_complete": outcome.coverage_complete,
        "exit_type": outcome.exit_type,
        "gross_r": outcome.gross_r,
        "fully_net_r": outcome.fully_net_r,
        "elevated_net_r": outcome.elevated_net_r,
        "fee_adjusted_r": outcome.fee_adjusted_r,
    })
    return base, outcome


def main():
    if OUTCOMES_FILE.exists() or REPORT_FILE.exists():
        raise RuntimeError("Existing results must not be overwritten")

    observations = load_observations(OBS_FILE)
    if len(observations) != 802:
        raise RuntimeError(f"Unexpected cohort: {len(observations)}")

    by_symbol = load_candles()
    records = []
    summaries = {}

    for model in MODELS:
        valid_outcomes = []
        model_records = []

        for obs in observations:
            row, outcome = calculate(
                obs, model, by_symbol.get(obs.symbol, [])
            )
            model_records.append(row)
            if outcome is not None:
                valid_outcomes.append(outcome)

        records.extend(model_records)
        metrics = summarize(valid_outcomes)

        metrics["cohort_n"] = len(observations)
        metrics["invalid_n_all"] = sum(
            r["status"] == "INVALID" for r in model_records
        )
        metrics["incomplete_n_all"] = sum(
            r["status"] == "INCOMPLETE" for r in model_records
        )
        metrics["finalized_n_all"] = sum(
            r["status"] == "FINALIZED" for r in model_records
        )
        metrics["reason_codes_all"] = dict(Counter(
            r["reason_code"]
            for r in model_records
            if r["reason_code"]
        ))
        metrics["execution_verdict"] = "DIAGNOSTIC_ONLY"

        summaries[model] = metrics

    with OUTCOMES_FILE.open(
        "w", encoding="utf-8", newline=""
    ) as fh:
        writer = csv.DictWriter(
            fh, fieldnames=list(records[0].keys())
        )
        writer.writeheader()
        writer.writerows(records)

    REPORT_FILE.write_text(
        json.dumps(
            {
                "experiment": "ME_RL_V1_ENTRY_GEOMETRY_COUNTERFACTUAL_AUDIT_V1",
                "source_cohort": 802,
                "path_policy": "48_CANDLES_AFTER_SIGNAL_BUCKET",
                "execution_verdict": "NOT_EXECUTION_VERIFIED",
                "ci_note": "IID diagnostic bootstrap; not symbol/day clustered",
                "model_summaries": summaries,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    for model, s in summaries.items():
        print("\nMODEL:", model)
        print("RAW:", s["cohort_n"])
        print("FINALIZED:", s["finalized_n_all"])
        print("INVALID:", s["invalid_n_all"])
        print("INCOMPLETE:", s["incomplete_n_all"])
        print("NET_E_R:", s["net_e_r"])
        print("PF:", s["profit_factor"])
        print("WR:", s["win_rate"])
        print("EXIT_TYPES:", s["exit_types"])
        print("REASON_CODES:", s["reason_codes_all"])

    print("\nEXECUTION VERDICT: DIAGNOSTIC_ONLY")
    print("OUTCOMES:", OUTCOMES_FILE)
    print("REPORT:", REPORT_FILE)


if __name__ == "__main__":
    main()
