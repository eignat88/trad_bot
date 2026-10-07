"""Deterministic, completed-event-only recovery for HTF Point-B observations.

This utility repairs missed ``research.prospective_observation`` rows solely
from immutable ``research.prospective_completed_event`` snapshots.  It is not
historical detector backfill: every source row was already prospectively
frozen by the runtime Point-B lifecycle.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.research.prospective_observer import ProspectiveOOSObserver

logger = logging.getLogger(__name__)

EXPERIMENT_ID = "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE"
FREEZE_TS = "2026-10-06T14:00:00Z"
COMPLETED_STATUS = "COMPLETED_IMMUTABLE"
VALID_DIRECTIONS = {"LONG", "SHORT"}
VALID_COHORTS = {"POINT_B"}

SELECT_COMPLETED_EVENTS_SQL = """
    SELECT experiment_id, setup_event_id, symbol, direction, status, snapshot, frozen_at, created_at
    FROM research.prospective_completed_event
    WHERE experiment_id = %s
    ORDER BY setup_event_id
"""
SELECT_EXISTING_OBSERVATION_SQL = """
    SELECT observation_id
    FROM research.prospective_observation
    WHERE experiment_id = %s AND source_signal_id = %s
"""
INSERT_OBSERVATION_SQL = """
    INSERT INTO research.prospective_observation (
        experiment_id, source_signal_id, source_observation_id,
        symbol, direction, signal_time,
        reference_price, invalidation_price, target_1, target_2,
        score, rule_passed, filter_reason,
        features, parameters, market_regime
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (experiment_id, source_signal_id) DO NOTHING
    RETURNING observation_id
"""

_REPORT_KEYS = (
    "scanned",
    "eligible",
    "already_observed",
    "would_insert",
    "inserted",
    "prefreeze",
    "experiment_mismatch",
    "snapshot_experiment_mismatch",
    "setup_id_mismatch",
    "symbol_mismatch",
    "direction_mismatch",
    "invalid_signal_time",
    "invalid_entry",
    "invalid_stop",
    "invalid_risk",
    "wrong_cohort",
    "malformed",
    "identity_collision",
)


def _parse_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decode_snapshot(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8")
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _completed_row_to_dict(row: Sequence[Any]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {
        "experiment_id": row[0],
        "setup_event_id": row[1],
        "symbol": row[2],
        "direction": row[3],
        "status": row[4],
        "snapshot": _decode_snapshot(row[5]),
        "frozen_at": row[6],
        "created_at": row[7],
    }


def recovery_source_signal_id(experiment_id: str, setup_event_id: str) -> int:
    return ProspectiveOOSObserver._make_htf_source_key(experiment_id, setup_event_id)


def _finite_positive(value: Any) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(numeric) and numeric > 0.0


def classify_completed_event(
    row: Mapping[str, Any],
    *,
    experiment_id: str = EXPERIMENT_ID,
    freeze_ts: str = FREEZE_TS,
) -> dict[str, Any]:
    """Classify one immutable completed event with exactly one final verdict."""
    freeze_dt = _parse_utc(freeze_ts)
    verdict: dict[str, Any] = {
        "row": row,
        "verdict": "WOULD_INSERT",
        "source_signal_id": None,
        "payload": None,
    }
    try:
        row_experiment = row.get("experiment_id")
        row_setup = row.get("setup_event_id")
        row_symbol = row.get("symbol")
        row_direction = row.get("direction")
        row_status = row.get("status")
        snapshot = row.get("snapshot")
        if not isinstance(snapshot, Mapping):
            snapshot = _decode_snapshot(snapshot)
        frozen_at = _parse_utc(row.get("frozen_at"))
        snapshot_experiment = snapshot.get("experiment_id")
        snapshot_setup = snapshot.get("setup_event_id")
        snapshot_symbol = snapshot.get("symbol")
        snapshot_direction = snapshot.get("direction")
        cohort = snapshot.get("cohort")
        signal_time_raw = snapshot.get("signal_time")
        try:
            signal_dt = _parse_utc(signal_time_raw)
        except (TypeError, ValueError, OverflowError):
            verdict["verdict"] = "invalid_signal_time"
            return verdict
        entry = snapshot.get("entry_reference_price")
        stop = snapshot.get("structural_stop_price")
        risk = snapshot.get("risk_abs")

        if row_experiment != experiment_id:
            verdict["verdict"] = "experiment_mismatch"
        elif row_status != COMPLETED_STATUS:
            verdict["verdict"] = "malformed"
        elif frozen_at <= freeze_dt:
            verdict["verdict"] = "prefreeze"
        elif snapshot_experiment != experiment_id:
            verdict["verdict"] = "snapshot_experiment_mismatch"
        elif snapshot_setup != row_setup or not snapshot_setup:
            verdict["verdict"] = "setup_id_mismatch"
        elif snapshot_symbol != row_symbol or not snapshot_symbol:
            verdict["verdict"] = "symbol_mismatch"
        elif snapshot_direction != row_direction or row_direction not in VALID_DIRECTIONS:
            verdict["verdict"] = "direction_mismatch"
        elif signal_dt <= freeze_dt:
            verdict["verdict"] = "prefreeze"
        elif cohort is not None and cohort not in VALID_COHORTS:
            verdict["verdict"] = "wrong_cohort"
        elif not _finite_positive(entry):
            verdict["verdict"] = "invalid_entry"
        elif not _finite_positive(stop):
            verdict["verdict"] = "invalid_stop"
        elif not _finite_positive(risk):
            verdict["verdict"] = "invalid_risk"
        elif snapshot_direction == "LONG" and float(entry) <= float(stop):
            verdict["verdict"] = "invalid_risk"
        elif snapshot_direction == "SHORT" and float(stop) <= float(entry):
            verdict["verdict"] = "invalid_risk"

        if verdict["verdict"] != "WOULD_INSERT":
            return verdict

        source_signal_id = recovery_source_signal_id(experiment_id, row_setup)
        authoritative = {
            "experiment_id": experiment_id,
            "setup_event_id": row_setup,
            "snapshot": dict(snapshot),
        }
        payload = ProspectiveOOSObserver._build_htf_point_b_observation_payload(
            exp_id=experiment_id,
            authoritative=authoritative,
            setup_event_id=row_setup,
            default_symbol=str(row_symbol),
            default_direction=str(row_direction),
            default_signal_time=signal_time_raw,
            default_reference_price=float(entry),
            default_invalidation_price=float(stop),
            default_target_1=snapshot.get("target_1"),
            default_target_2=snapshot.get("target_2"),
            parameters={},
            market_regime=None,
            source_key=source_signal_id,
        )
        if payload is None:
            verdict["verdict"] = "malformed"
        else:
            verdict["source_signal_id"] = source_signal_id
            verdict["payload"] = payload
        return verdict
    except Exception:
        verdict["verdict"] = "malformed"
        return verdict


def _new_report() -> dict[str, int]:
    return {key: 0 for key in _REPORT_KEYS}


def _insert_observation(cursor, payload: Mapping[str, Any]) -> bool:
    cursor.execute(
        INSERT_OBSERVATION_SQL,
        (
            payload["experiment_id"],
            payload["source_signal_id"],
            None,
            payload["symbol"],
            payload["direction"],
            payload["signal_time"],
            payload["reference_price"],
            payload["invalidation_price"],
            payload["target_1"],
            payload["target_2"],
            payload["score"],
            payload["rule_passed"],
            payload["filter_reason"],
            payload["features"],
            payload["parameters"],
            payload["market_regime"],
        ),
    )
    row = cursor.fetchone()
    return row is not None


def recover_completed_events(
    observer: ProspectiveOOSObserver | None,
    conn: Any,
    rows: Iterable[Mapping[str, Any] | Sequence[Any]],
    *,
    experiment_id: str = EXPERIMENT_ID,
    freeze_ts: str = FREEZE_TS,
    apply: bool = False,
) -> dict[str, int]:
    """Classify and optionally insert missing observations from immutable rows."""
    report = _new_report()
    cursor = conn.cursor()
    seen: set[tuple[str, int]] = set()
    try:
        for raw_row in rows:
            report["scanned"] += 1
            row = _completed_row_to_dict(raw_row)
            verdict = classify_completed_event(
                row,
                experiment_id=experiment_id,
                freeze_ts=freeze_ts,
            )
            final_verdict = verdict["verdict"]
            if final_verdict == "WOULD_INSERT":
                identity = (experiment_id, verdict["source_signal_id"])
                if identity in seen:
                    final_verdict = "identity_collision"
                else:
                    seen.add(identity)
                    cursor.execute(
                        SELECT_EXISTING_OBSERVATION_SQL,
                        identity,
                    )
                    if cursor.fetchone() is not None:
                        final_verdict = "already_observed"
                    else:
                        report["would_insert"] += 1
                        inserted = apply and _insert_observation(cursor, verdict["payload"])
                        if inserted:
                            report["inserted"] += 1
                        elif apply:
                            final_verdict = "already_observed"
                            report["would_insert"] -= 1
            if final_verdict != "WOULD_INSERT":
                report[final_verdict] = report.get(final_verdict, 0) + 1
            if final_verdict in {"WOULD_INSERT", "already_observed"}:
                report["eligible"] += 1
        if apply:
            conn.commit()
        else:
            conn.rollback()
    finally:
        cursor.close()
    return report


def fetch_completed_events(
    conn: Any,
    *,
    experiment_id: str = EXPERIMENT_ID,
) -> list[dict[str, Any]]:
    cursor = conn.cursor()
    try:
        cursor.execute(SELECT_COMPLETED_EVENTS_SQL, (experiment_id,))
        return [_completed_row_to_dict(row) for row in cursor.fetchall()]
    finally:
        cursor.close()


def _load_connection(args: argparse.Namespace):
    if args.host:
        import pg8000

        return pg8000.connect(
            host=args.host,
            port=args.port,
            database=args.database,
            user=args.user,
            password=args.password,
        )

    from app.config.settings import load_settings
    from app.db.repository import ScannerRepository

    root = Path(__file__).resolve().parents[2]
    settings = load_settings(
        path=root / "config.yaml",
        env_file=root / ".env",
    )
    return ScannerRepository(
        settings.db_host,
        settings.db_port,
        settings.db_name,
        settings.db_user,
        settings.db_password,
    )._conn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recover HTF Point-B prospective observations from immutable completed events.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Classify only; perform zero writes (default).")
    mode.add_argument("--apply", action="store_true", help="Insert missing eligible observations idempotently.")
    parser.add_argument("--experiment-id", default=EXPERIMENT_ID)
    parser.add_argument("--freeze-ts", default=FREEZE_TS)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--database")
    parser.add_argument("--user")
    parser.add_argument("--password")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    apply = args.apply
    conn = _load_connection(args)
    try:
        rows = fetch_completed_events(conn, experiment_id=args.experiment_id)
        report = recover_completed_events(
            None,
            conn,
            rows,
            experiment_id=args.experiment_id,
            freeze_ts=args.freeze_ts,
            apply=apply,
        )
        print(json.dumps({"mode": "apply" if apply else "dry-run", **report}, indent=2, sort_keys=True))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
