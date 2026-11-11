#!/usr/bin/env python3
"""Non-mutating pre-activation audit for ME_LONG_V2_INVERSE_SHORT_V2.

This tool NEVER activates the contour. It uses the dedicated systemd
EnvironmentFile explicitly; it does not silently fall back to config.yaml
or the scanner's process environment.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.paper.me_long_v2_inverse_short_v2.protocol import (
    EXPERIMENT_ID, PAPER_ONLY, PROSPECTIVE_ONLY, HISTORICAL_BACKFILL,
    SOURCE_SCANNER, SOURCE_DIRECTION, TRADE_DIRECTION, protocol_fingerprint,
)

ENV_KEYS = frozenset({
    "ME_INVERSE_SHORT_V2_ENABLED", "DB_HOST", "DB_PORT", "DB_NAME",
    "DB_USER", "DB_PASSWORD",
})
TABLE = "dds.me_long_v2_inverse_short_v2_"


class PreflightRefused(RuntimeError):
    """A safe, secret-free explanation for refusal."""


def read_service_env(path: Path, *, require_disabled: bool = True) -> dict[str, str]:
    """Parse only necessary EnvironmentFile keys; never expose their values."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PreflightRefused("SERVICE_ENV_UNREADABLE") from exc
    values: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            raise PreflightRefused("SERVICE_ENV_UNSUPPORTED_SYNTAX")
        if "=" not in line:
            raise PreflightRefused("SERVICE_ENV_UNSUPPORTED_SYNTAX")
        key, raw = line.split("=", 1)
        key = key.strip()
        if key not in ENV_KEYS:
            continue
        try:
            parsed = shlex.split(raw, comments=False, posix=True)
        except ValueError as exc:
            raise PreflightRefused("SERVICE_ENV_INVALID_VALUE") from exc
        if len(parsed) != 1 or key in values:
            raise PreflightRefused("SERVICE_ENV_DUPLICATE_OR_INVALID_KEY")
        values[key] = parsed[0]
    if any(not values.get(key) for key in ENV_KEYS):
        raise PreflightRefused("SERVICE_ENV_INCOMPLETE")
    if values["ME_INVERSE_SHORT_V2_ENABLED"] not in ("0", "1"):
        raise PreflightRefused("SERVICE_FLAG_INVALID")
    if require_disabled and values["ME_INVERSE_SHORT_V2_ENABLED"] != "0":
        raise PreflightRefused("SERVICE_FLAG_MUST_BE_OFF")
    if values["DB_NAME"] != "trad_bot":
        raise PreflightRefused("UNEXPECTED_DB_NAME")
    try:
        port = int(values["DB_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError as exc:
        raise PreflightRefused("INVALID_DB_PORT") from exc
    return values


def audit_cursor(cursor: Any) -> dict[str, Any]:
    """Read-only queries. Caller must already be inside a READ ONLY txn."""
    cursor.execute(
        "SELECT experiment_id, status, protocol_fingerprint, freeze_ts "
        f"FROM {TABLE}activation WHERE experiment_id = %s",
        (EXPERIMENT_ID,),
    )
    row = cursor.fetchone()
    if row is None:
        raise PreflightRefused("ACTIVATION_ROW_MISSING")
    experiment, status, stored_fp, freeze_ts = row
    if experiment != EXPERIMENT_ID or status != "READY_TO_START":
        raise PreflightRefused("ACTIVATION_NOT_READY")
    if stored_fp is not None or freeze_ts is not None:
        raise PreflightRefused("ALREADY_FROZEN")

    cursor.execute(
        "SELECT "
        f"(SELECT count(*) FROM {TABLE}event), "
        f"(SELECT count(*) FROM {TABLE}position), "
        f"(SELECT count(*) FROM {TABLE}close_outbox)"
    )
    counts = cursor.fetchone()
    if counts is None or len(counts) != 3 or any(value != 0 for value in counts):
        raise PreflightRefused("EXISTING_OOS_DATA")
    return {
        "experiment_id": experiment,
        "activation_status": status,
        "stored_fingerprint_null": True,
        "stored_freeze_ts_null": True,
        "events": counts[0],
        "positions": counts[1],
        "close_outbox": counts[2],
    }


def run_preflight(path: Path, *, connect: Any = None) -> dict[str, Any]:
    values = read_service_env(path)
    if not (PAPER_ONLY and PROSPECTIVE_ONLY and not HISTORICAL_BACKFILL):
        raise PreflightRefused("PROTOCOL_NOT_PAPER_PROSPECTIVE")
    if (SOURCE_SCANNER, SOURCE_DIRECTION, TRADE_DIRECTION) != (
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", "LONG", "SHORT"
    ):
        raise PreflightRefused("SOURCE_DIRECTION_MISMATCH")
    if connect is None:
        import pg8000
        connect = pg8000.connect

    connection = None
    try:
        connection = connect(
            host=values["DB_HOST"], port=int(values["DB_PORT"]),
            database=values["DB_NAME"], user=values["DB_USER"],
            password=values["DB_PASSWORD"], timeout=10,
        )
        # Explicitly start a read-only transaction; no DML can succeed.
        connection.autocommit = True
        cur = connection.cursor()
        try:
            cur.execute("BEGIN TRANSACTION READ ONLY")
            result = audit_cursor(cur)
        finally:
            connection.rollback()
            cur.close()
    except PreflightRefused:
        raise
    except Exception as exc:
        # Do NOT print exception text: drivers sometimes interpolate credentials.
        raise PreflightRefused("DATABASE_READ_ONLY_CHECK_FAILED") from exc
    finally:
        if connection is not None:
            connection.close()

    return {
        "result": "PREACTIVATION_DB_PASS",
        **result,
        "current_protocol_fingerprint": protocol_fingerprint(),
        "runtime_flag": "OFF",
        "source_scanner": SOURCE_SCANNER,
        "source_direction": SOURCE_DIRECTION,
        "paper_direction": TRADE_DIRECTION,
        "paper_only": True,
        "prospective_only": True,
        "backfill": False,
        # Scanner runs in its OWN service/process: not verified by this tool.
        "scanner_capture_effective_flag": "NOT_VERIFIED",
        "scanner_source_production_readiness": "NOT_VERIFIED",
        "activation_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file", type=Path,
        default=Path("/etc/trad-bot/me-inverse-short-v2-runtime.env"),
        help="dedicated runtime EnvironmentFile (no fallback)",
    )
    args = parser.parse_args(argv)
    try:
        result = run_preflight(args.env_file)
    except PreflightRefused as exc:
        print(json.dumps({"result": "REFUSED", "reason": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
