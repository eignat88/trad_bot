from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tools.research.srr_short_r_expansion_candle_path_integrity_audit_v1 import (
    bybit_historical_klines,
)

CANDLE_MS = 300_000
SOURCE = Path("data/research/me_rl_v1_geometry/observations_802.csv")
OUTPUT = Path("data/research/me_rl_v1_geometry/bybit_5m_802.csv")
REPORT = Path("data/research/me_rl_v1_geometry/bybit_download_report.json")


def main() -> None:
    with SOURCE.open(encoding="utf-8-sig", newline="") as fh:
        observations = list(csv.DictReader(fh))

    if len(observations) != 802:
        raise RuntimeError(f"Unexpected cohort: {len(observations)}")

    windows = defaultdict(list)

    for row in observations:
        signal = datetime.fromisoformat(
            row["signal_time"].replace("Z", "+00:00")
        ).astimezone(timezone.utc)

        signal_ms = int(signal.timestamp() * 1000)
        current_open = signal_ms // CANDLE_MS * CANDLE_MS

        start = current_open - 2 * CANDLE_MS
        end = current_open + 49 * CANDLE_MS

        windows[row["symbol"]].append((start, end))

    merged_windows = []

    for symbol, intervals in sorted(windows.items()):
        intervals.sort()
        merged = []

        for start, end in intervals:
            if merged and start <= merged[-1][1] + CANDLE_MS:
                merged[-1] = (
                    merged[-1][0],
                    max(merged[-1][1], end),
                )
            else:
                merged.append((start, end))

        for start, end in merged:
            merged_windows.append((symbol, start, end))

    print("OBSERVATIONS:", len(observations))
    print("SYMBOLS:", len(windows))
    print("MERGED_WINDOWS:", len(merged_windows))

    # Guard against accidental overwrites of an existing dataset.
    if OUTPUT.exists() or REPORT.exists():
        raise RuntimeError(
            "Output already exists. Preserve prior dataset; "
            "do not overwrite automatically."
        )

    candles_by_key = {}
    errors = []
    warnings = []
    window_records = []

    for idx, (symbol, start, end) in enumerate(merged_windows, 1):
        print(
            f"[{idx}/{len(merged_windows)}] "
            f"{symbol} {start} -> {end}",
            flush=True,
        )

        try:
            candles, notes = bybit_historical_klines(
                symbol,
                start,
                end,
                category="linear",
                interval="5",
            )

            received = {
                int(c["open_time_ms"]): c
                for c in candles
                if start <= int(c["open_time_ms"]) <= end
            }

            expected = set(range(start, end + CANDLE_MS, CANDLE_MS))
            missing = expected - set(received)

            status = (
                "COMPLETE"
                if not missing and not notes
                else "SOURCE_REVIEW_REQUIRED"
            )

            window_records.append({
                "symbol": symbol,
                "start_ms": start,
                "end_ms": end,
                "expected_count": len(expected),
                "received_count": len(received),
                "missing_count": len(missing),
                "status": status,
                "notes": notes,
            })

            for candle in received.values():
                key = (symbol, int(candle["open_time_ms"]))
                if key in candles_by_key:
                    warnings.append(
                        f"OVERLAPPING_DUPLICATE:{symbol}:{key[1]}"
                    )
                candles_by_key[key] = candle

            for note in notes:
                warnings.append(f"{symbol}:{note}")

        except Exception as exc:
            errors.append({
                "symbol": symbol,
                "start_ms": start,
                "end_ms": end,
                "error": f"{type(exc).__name__}:{exc}",
            })

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "symbol", "open_ms", "open", "high",
            "low", "close", "volume",
            "source", "market_type", "interval",
        ])

        for (symbol, open_ms), candle in sorted(candles_by_key.items()):
            writer.writerow([
                symbol,
                open_ms,
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                candle["volume"],
                "BYBIT_V5_HISTORICAL",
                "linear",
                "5",
            ])

    checksum = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()

    report = {
        "experiment": "ME_RL_V1_ENTRY_GEOMETRY_COUNTERFACTUAL_AUDIT_V1",
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "cohort_n": len(observations),
        "symbols": len(windows),
        "merged_windows": len(merged_windows),
        "unique_candles": len(candles_by_key),
        "dataset_sha256": checksum,
        "window_records": window_records,
        "errors": errors,
        "warnings": warnings,
    }

    REPORT.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("UNIQUE_CANDLES:", len(candles_by_key))
    print("INCOMPLETE_WINDOWS:",
          sum(w["status"] != "COMPLETE" for w in window_records))
    print("ERRORS:", len(errors))
    print("WARNINGS:", len(warnings))
    print("SHA256:", checksum)
    print("OUTPUT:", OUTPUT)
    print("REPORT:", REPORT)


if __name__ == "__main__":
    main()
