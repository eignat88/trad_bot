"""FVG Production Structural Parity Audit.

Reads production scanner_setup records and verifies that frozen
production parameters are internally consistent with the research
configuration (entry, SL, TP, frozen features, eligibility thresholds).

This tool does NOT replay candles through the research detector.
It performs structural/parameter-level consistency checks only.

This is a READ-ONLY diagnostic tool.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def run_parity_audit(
    repository: Any,
    scanner_name: str = "FVG_REACTION_LONG_LOCAL_STRUCT_V1",
    limit: int = 100,
) -> dict[str, Any]:
    """Audit parity between production setups and research logic.

    Returns a dict with:
      - setups: list of per-setup parity records
      - summary: aggregate counts
    """
    # Pull production setups
    cursor = repository._conn.cursor()
    cursor.execute(
        """
        SELECT s.setup_id, s.scanner_name, i.symbol, s.direction,
               s.setup_timeframe, s.entry_timeframe,
               s.detected_at, s.signal_candle_open_time,
               s.reference_price, s.entry_zone_low, s.entry_zone_high,
               s.invalidation_price, s.target_1, s.target_2,
               s.score, s.market_regime, s.reasons, s.features
        FROM dds.scanner_setup s
        JOIN dds.instrument i ON i.instrument_id = s.instrument_id
        WHERE s.scanner_name = %s
          AND s.entry_zone_low IS NOT NULL
          AND s.target_1 IS NOT NULL
        ORDER BY s.detected_at DESC
        LIMIT %s
        """,
        (scanner_name, limit),
    )

    rows = cursor.fetchall()
    logger.info("Loaded %d production setups for parity audit", len(rows))

    from app.db.repository import ScannerRepository
    records: list[dict[str, Any]] = []

    for row in rows:
        setup_id = str(row[0])
        symbol = row[2]
        direction = row[3]
        timeframe = row[5]  # entry_timeframe
        prod_signal_ts = int(row[7]) if row[7] else 0  # signal_candle_open_time
        prod_entry = float(row[8]) if row[8] else None  # reference_price
        prod_entry_low = float(row[9]) if row[9] else None  # entry_zone_low
        prod_entry_high = float(row[10]) if row[10] else None  # entry_zone_high
        prod_sl = float(row[11]) if row[11] else None  # invalidation_price
        prod_tp = float(row[12]) if row[12] else None  # target_1
        prod_features = row[17]
        if isinstance(prod_features, str):
            prod_features = json.loads(prod_features)

        prod_confirmation_at = prod_features.get("confirmation_at") if prod_features else None

        # Attempt to replay through research engine
        parity_status = "UNVERIFIED"
        mismatch_reason = None
        research_entry = None
        research_sl = None
        research_tp = None
        research_confirmation_at = None
        research_signal = None

        # We can't fully replay without candle data, so we verify parameters
        # and structural consistency instead
        checks: list[str] = []

        # 1. Check FIRST_TOUCH (entry = entry_zone_high for LONG)
        if direction == "LONG" and prod_entry_high is not None:
            expected_entry = prod_entry_high
            if prod_entry is not None and abs(prod_entry - expected_entry) > 0.01:
                checks.append(f"entry mismatch: prod={prod_entry} expected={expected_entry}")
            research_entry = expected_entry

        # 2. Check SL = C2_EXTREMUM
        if prod_sl is not None and prod_features:
            c2_low = prod_features.get("sl_price", prod_sl)
            # SL should be below entry for LONG
            if direction == "LONG" and prod_sl >= (prod_entry_high or 0):
                checks.append(f"SL not below entry: sl={prod_sl} entry={prod_entry_high}")

        # 3. Check TP = 3R
        if prod_sl is not None and prod_entry_high is not None and prod_tp is not None:
            risk = abs(prod_entry_high - prod_sl)
            if risk > 0:
                expected_tp = prod_entry_high + 3.0 * risk
                tp_diff = abs(prod_tp - expected_tp)
                if tp_diff > 0.01:
                    checks.append(f"TP mismatch: prod={prod_tp} expected_3R={expected_tp:.4f} diff={tp_diff:.4f}")
                research_tp = expected_tp

        # 4. Check frozen parameters present
        if prod_features is not None:
            if "fvg_created_at" not in prod_features:
                checks.append("missing fvg_created_at in features")
            if "confirmation_at" not in prod_features:
                checks.append("missing confirmation_at in features")
            if "bars_to_touch" not in prod_features:
                checks.append("missing bars_to_touch in features")
            if prod_features.get("rr") != 3.0:
                checks.append(f"rr={prod_features.get('rr')} expected 3.0")

        # 5. Check fvg_atr >= 0.05
        if prod_features is not None and "fvg_atr" in prod_features:
            fvg_atr = prod_features["fvg_atr"]
            if fvg_atr < 0.05:
                checks.append(f"fvg_atr={fvg_atr:.4f} < 0.05 minimum")

        # 6. Check c2_body_ratio >= 0.50
        if prod_features is not None and "c2_body_ratio" in prod_features:
            cbr = prod_features["c2_body_ratio"]
            if cbr < 0.50:
                checks.append(f"c2_body_ratio={cbr:.4f} < 0.50 minimum")

        # 7. Check MAX_BARS_TO_TOUCH
        if prod_features is not None and "bars_to_touch" in prod_features:
            btt = prod_features["bars_to_touch"]
            if btt is not None and btt > 48:
                checks.append(f"bars_to_touch={btt} > 48 max")

        if not checks:
            parity_status = "MATCHED"
        else:
            parity_status = "MISMATCH"
            mismatch_reason = "; ".join(checks)

        record = {
            "setup_id": setup_id,
            "symbol": symbol,
            "direction": direction,
            "timeframe": timeframe,
            "prod_signal_ts": prod_signal_ts,
            "prod_entry": prod_entry,
            "prod_entry_low": prod_entry_low,
            "prod_entry_high": prod_entry_high,
            "prod_sl": prod_sl,
            "prod_tp": prod_tp,
            "prod_confirmation_at": prod_confirmation_at,
            "prod_features_keys": list(prod_features.keys()) if prod_features else [],
            "research_entry": research_entry,
            "research_tp": research_tp,
            "parity_status": parity_status,
            "mismatch_reason": mismatch_reason,
            "fvg_atr": prod_features.get("fvg_atr") if prod_features else None,
            "c2_body_ratio": prod_features.get("c2_body_ratio") if prod_features else None,
            "bars_to_touch": prod_features.get("bars_to_touch") if prod_features else None,
        }
        records.append(record)

    # Summary
    total = len(records)
    matched = sum(1 for r in records if r["parity_status"] == "MATCHED")
    mismatched = sum(1 for r in records if r["parity_status"] == "MISMATCH")
    verified = sum(1 for r in records if r["parity_status"] == "UNVERIFIED")

    summary = {
        "total": total,
        "matched": matched,
        "mismatched": mismatched,
        "unverified": verified,
    }

    # Detailed mismatch breakdown
    mismatch_reasons: dict[str, int] = {}
    for r in records:
        if r["mismatch_reason"]:
            for part in r["mismatch_reason"].split("; "):
                key = part.split(":")[0] if ":" in part else part
                mismatch_reasons[key] = mismatch_reasons.get(key, 0) + 1

    summary["mismatch_breakdown"] = mismatch_reasons

    # Duplicate check: same (symbol, timeframe, fvg_created_at)
    if records:
        fvg_keys = []
        for r in records:
            fvg_at = r.get("prod_confirmation_at") or r.get("prod_signal_ts")
            fvg_keys.append((r["symbol"], r["timeframe"], fvg_at))
        from collections import Counter
        dupes = {k: v for k, v in Counter(fvg_keys).items() if v > 1}
        summary["duplicates"] = len(dupes)
        summary["duplicate_details"] = [
            {"symbol": k[0], "tf": k[1], "count": v} for k, v in dupes.items()
        ]

    logger.info(
        "Parity audit: total=%d matched=%d mismatched=%d duplicates=%d",
        total, matched, mismatched, summary.get("duplicates", 0),
    )

    return {"records": records, "summary": summary}


def print_parity_report(result: dict[str, Any]) -> None:
    """Print a compact parity audit report."""
    summary = result["summary"]
    records = result["records"]

    logger.info("=== FVG Parity Audit ===")
    logger.info(
        "  Total: %d | Matched: %d | Mismatched: %d | Unverified: %d",
        summary["total"], summary["matched"],
        summary["mismatched"], summary["unverified"],
    )
    logger.info("  Duplicates: %d", summary.get("duplicates", 0))

    if summary.get("mismatch_breakdown"):
        logger.info("  Mismatch reasons:")
        for reason, count in sorted(summary["mismatch_breakdown"].items(),
                                     key=lambda x: -x[1]):
            logger.info("    %s: %d", reason, count)

    # Print per-setup details for mismatches
    mismatches = [r for r in records if r["parity_status"] == "MISMATCH"]
    if mismatches:
        logger.info("  Mismatched setups:")
        for r in mismatches:
            logger.info(
                "    %s %s %s | prod_entry=%.4f sl=%.4f tp=%.4f | %s",
                r["symbol"], r["timeframe"], r["setup_id"][:8],
                r["prod_entry"] or 0, r["prod_sl"] or 0, r["prod_tp"] or 0,
                r["mismatch_reason"],
            )

    # Print parameter distribution
    if records:
        fvg_atrs = [r["fvg_atr"] for r in records if r.get("fvg_atr") is not None]
        cbrs = [r["c2_body_ratio"] for r in records if r.get("c2_body_ratio") is not None]
        btts = [r["bars_to_touch"] for r in records if r.get("bars_to_touch") is not None]

        if fvg_atrs:
            logger.info("  fvg_atr: min=%.4f median=%.4f max=%.4f",
                       min(fvg_atrs), sorted(fvg_atrs)[len(fvg_atrs)//2], max(fvg_atrs))
        if cbrs:
            logger.info("  c2_body_ratio: min=%.4f median=%.4f max=%.4f",
                       min(cbrs), sorted(cbrs)[len(cbrs)//2], max(cbrs))
        if btts:
            logger.info("  bars_to_touch: min=%d median=%d max=%d",
                       min(btts), sorted(btts)[len(btts)//2], max(btts))


def main() -> None:
    parser = argparse.ArgumentParser(description="FVG production structural parity audit")
    parser.add_argument("--scanner", type=str,
                        default="FVG_REACTION_LONG_LOCAL_STRUCT_V1",
                        help="Scanner name to audit")
    parser.add_argument("--limit", type=int, default=100,
                        help="Max setups to audit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from app.config import load_settings
    from app.db.repository import ScannerRepository

    settings = load_settings()
    repository = ScannerRepository(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        backend="postgres",
    )
    try:
        result = run_parity_audit(repository, args.scanner, args.limit)
        print_parity_report(result)
    finally:
        repository.close()


if __name__ == "__main__":
    main()
