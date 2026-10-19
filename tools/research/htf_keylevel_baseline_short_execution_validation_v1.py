"""Read-only Phase 1 integrity audit for prospective HTF key-level BASELINE SHORT.

No order placement, candle-path replay, protocol tuning or production writes.
The OOS source is distinct from a new, as-yet-unfrozen execution holdout.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXPERIMENT_ID = "HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE"
FREEZE_TS = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)

QUERY = """
SELECT p.observation_id, p.source_signal_id, p.symbol, p.direction,
       p.signal_time, p.reference_price::float8,
       p.invalidation_price::float8, p.target_1::float8,
       p.features, (o.observation_id IS NOT NULL) AS has_outcome,
       COALESCE(o.is_final, FALSE) AS is_final
FROM research.prospective_observation p
LEFT JOIN research.prospective_outcome o ON o.observation_id = p.observation_id
WHERE p.experiment_id = %s AND p.direction = 'SHORT'
ORDER BY p.signal_time, p.observation_id
"""


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("invalid signal_time")
    if dt.tzinfo is None:
        raise ValueError("naive signal_time")
    return dt.astimezone(timezone.utc)


def classify(row: tuple[Any, ...], seen_ids: set[Any], seen_sources: set[Any]) -> list[str]:
    """Return all blocking issues; do not normalize away malformed records."""
    oid, source, symbol, direction, ts, entry, stop, target, features, has_outcome, is_final = row
    problems: list[str] = []
    if oid is None or oid in seen_ids:
        problems.append("missing_or_duplicate_observation_id")
    if oid is not None:
        seen_ids.add(oid)
    if source is None or source in seen_sources:
        problems.append("missing_or_duplicate_source_signal_id")
    if source is not None:
        seen_sources.add(source)
    if not isinstance(symbol, str) or not symbol:
        problems.append("missing_symbol")
    if direction != "SHORT":
        problems.append("wrong_direction")
    try:
        if _utc(ts) <= FREEZE_TS:
            problems.append("pre_or_at_freeze")
    except (ValueError, TypeError, OverflowError):
        problems.append("invalid_signal_time")
    try:
        e, s, t = float(entry), float(stop), float(target)
        import math
        if not all(math.isfinite(v) and v > 0 for v in (e, s, t)):
            problems.append("invalid_or_nonpositive_price")
        elif not s > e > t:
            problems.append("invalid_short_geometry")
    except (TypeError, ValueError):
        problems.append("missing_or_invalid_price")
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except ValueError:
            features = None
    if not isinstance(features, dict):
        problems.append("missing_or_invalid_features")
    elif features.get("cohort") not in (None, "BASELINE"):
        problems.append("wrong_cohort")
    if not has_outcome:
        problems.append("missing_outcome")
    elif not is_final:
        problems.append("immature_outcome")
    return problems


def audit_rows(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    reasons: Counter[str] = Counter()
    seen_ids: set[Any] = set()
    seen_sources: set[Any] = set()
    dates: set[str] = set()
    symbols: set[str] = set()
    valid_final = 0
    for row in rows:
        issues = classify(row, seen_ids, seen_sources)
        if issues:
            reasons.update(issues)
        else:
            valid_final += 1
            dates.add(_utc(row[4]).date().isoformat())
            symbols.add(row[2])
    return {
        "experiment_id": EXPERIMENT_ID,
        "direction": "SHORT",
        "freeze_ts": FREEZE_TS.isoformat(),
        "observations": len(rows),
        "strict_valid_finalized": valid_final,
        "valid_symbols": len(symbols),
        "valid_oos_dates": len(dates),
        "issues": dict(sorted(reasons.items())),
        "execution_verdict": "NOT_EVALUATED",
        "database_mutations": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Local JSON output path (default: stdout)")
    args = parser.parse_args(argv)

    # Lazy imports keep pure checks unit-testable without DB setup.
    from app.config.settings import load_settings
    from app.db.repository import ScannerRepository

    root = Path(__file__).resolve().parents[2]
    s = load_settings(path=root / "config.yaml", env_file=root / ".env")
    repo = ScannerRepository(
        s.db_host, s.db_port, s.db_name, s.db_user, s.db_password,
        backend="postgres",
    )
    conn = repo._conn
    try:
        # Fail closed: transaction-level PostgreSQL read-only guard.
        conn.rollback()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN READ ONLY")
            cur.execute(QUERY, (EXPERIMENT_ID,))
            rows = cur.fetchall()
            report = audit_rows(rows)
            report["asof_utc"] = datetime.now(timezone.utc).isoformat()
        finally:
            conn.rollback()
            cur.close()
    finally:
        conn.close()

    payload = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
