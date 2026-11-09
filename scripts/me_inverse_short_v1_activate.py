#!/usr/bin/env python3
"""ME_LONG_V2_INVERSE_SHORT_V1 — separate protocol-fingerprint / freeze fixator.

Requirement 10 of the contour: the runner may start ONLY after the tests
pass AND the protocol fingerprint / ``freeze_ts`` are SEPARATELY fixed.

This tool performs that fixation (and nothing else):

  * ``--show`` (default)
        read-only status: current code fingerprint, stored row, whether they
        match, and whether the contour would be ACTIVE right now.
  * ``--activate --freeze-ts <ISO8601>``
        transition the single activation row to RUNNING with the CURRENT
        code's ``protocol_fingerprint`` and the given freeze boundary.
        Refuses a freeze in the future, refuses a naive timestamp, and
        refuses to overwrite an already-RUNNING activation unless
        ``--re-freeze`` is given (which must then be an explicit decision).
  * ``--drain``
        transition RUNNING -> DRAINING (stop new captures/entries; already
        open positions still manage their exits).

The tool writes ONLY ``dds.me_long_v2_inverse_short_v1_activation`` — never
events, positions, paper trades, setups, gates or expectancy data.

Typical sequence (after the test suites pass):

    python scripts/me_inverse_short_v1_activate.py --show
    python scripts/me_inverse_short_v1_activate.py --activate --freeze-ts 2026-11-07T00:00:00Z
    python scripts/me_inverse_short_v1_activate.py --show
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running as a plain script from the repository root or from scripts/.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config import load_settings  # noqa: E402
from scripts.me_inverse_short_v1_preflight import (  # noqa: E402
    read_service_env, PreflightRefused,
)
from app.paper.me_long_v2_inverse_short_v1.activation import (  # noqa: E402
    evaluate_activation,
    is_runtime_enabled,
)
from app.paper.me_long_v2_inverse_short_v1.models import ActivationRecord  # noqa: E402
from app.paper.me_long_v2_inverse_short_v1.protocol import (  # noqa: E402
    EXPERIMENT_ID,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_v1.store import (  # noqa: E402
    ACTIVATION_TABLE,
)

SELECT_SQL = (
    "SELECT status, protocol_fingerprint, freeze_ts, notes, updated_at "
    f"FROM {ACTIVATION_TABLE} WHERE experiment_id = %s"
)

# One UPDATE: status + fingerprint + freeze are fixed ATOMICALLY so a crash
# can never leave RUNNING with a stale/absent fingerprint (the table CHECK
# rejects that state anyway).
UPDATE_SQL = (
    f"UPDATE {ACTIVATION_TABLE} SET "
    "status = %s, protocol_fingerprint = %s, freeze_ts = %s, "
    "notes = %s, updated_at = now() "
    "WHERE experiment_id = %s"
)


class ActivationError(RuntimeError):
    """Operator-visible refusal (fail-closed, no partial writes)."""


def parse_freeze_ts(value: str, *, now: datetime | None = None) -> datetime:
    """Validate the freeze boundary: explicit ISO-8601, aware, not future."""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ActivationError("freeze-ts must be a non-empty trimmed ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActivationError(f"freeze-ts is not ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ActivationError("freeze-ts must be timezone-aware (e.g. ...Z or +00:00)")
    parsed = parsed.astimezone(timezone.utc)
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if parsed > reference:
        raise ActivationError(
            f"freeze-ts {parsed.isoformat()} is in the future "
            f"(now={reference.isoformat()})"
        )
    return parsed


def render_status(
    *,
    current_fingerprint: str,
    stored_status: str | None,
    stored_fingerprint: str | None,
    stored_freeze: datetime | None,
    now: datetime,
    runtime_enabled: bool,
) -> dict[str, Any]:
    """Read-only, JSON-friendly view of the activation state."""
    record = None
    if stored_status is not None:
        record = ActivationRecord(
            experiment_id=EXPERIMENT_ID,
            status=stored_status,
            protocol_fingerprint=stored_fingerprint or "",
            freeze_ts=stored_freeze,
        )
    identity, reason = evaluate_activation(
        record, enabled=runtime_enabled, expected_fingerprint=current_fingerprint,
    )
    return {
        "experiment_id": EXPERIMENT_ID,
        "current_protocol_fingerprint": current_fingerprint,
        "stored_status": stored_status,
        "stored_protocol_fingerprint": stored_fingerprint,
        "stored_freeze_ts": stored_freeze.isoformat() if stored_freeze else None,
        "fingerprint_matches": bool(
            stored_fingerprint and stored_fingerprint == current_fingerprint
        ),
        "runtime_flag": "ME_INVERSE_SHORT_V1_ENABLED=1" if runtime_enabled else "unset",
        "active": identity is not None,
        "reason": reason,
        "contour_status": "ACTIVE" if identity is not None else "NOT_DEPLOYED",
        "checked_at": now.isoformat(),
    }


def plan_activation(
    *,
    current_fingerprint: str,
    freeze_ts: datetime,
    existing_status: str | None,
    existing_fingerprint: str | None,
    existing_freeze: datetime | None,
    re_freeze: bool,
    note: str,
) -> tuple[str, tuple[Any, ...]]:
    """Decide the activation UPDATE (raises ActivationError when refused)."""
    if existing_status is None:
        raise ActivationError("activation row is missing; refusing to create an implicit activation")
    if existing_status == "DRAINING" and not re_freeze:
        raise ActivationError("DRAINING activation cannot be restarted without explicit --re-freeze")
    if existing_status not in ("READY_TO_START", "RUNNING", "DRAINING", "CANCELLED"):
        raise ActivationError("unknown activation status; refusing mutation")
    if existing_status == "RUNNING" and not re_freeze:
        same = (
            existing_fingerprint == current_fingerprint
            and existing_freeze == freeze_ts
        )
        if same:
            raise ActivationError(
                "activation is already RUNNING with this exact "
                "fingerprint/freeze (idempotent no-op; nothing to do)"
            )
        raise ActivationError(
            "a RUNNING activation already exists with a different "
            "fingerprint/freeze — refusing to silently re-freeze; "
            "pass --drain first, then --activate --re-freeze"
        )
    if existing_status == "CANCELLED":
        raise ActivationError(
            "the activation row is CANCELLED (terminal) — refusing to revive it"
        )
    return (
        UPDATE_SQL,
        (
            "RUNNING",
            current_fingerprint,
            freeze_ts.isoformat(),
            note,
            EXPERIMENT_ID,
        ),
    )


def plan_drain(
    *,
    existing_status: str | None,
    existing_fingerprint: str | None,
    existing_freeze: datetime | None,
    note: str,
) -> tuple[str, tuple[Any, ...]]:
    """RUNNING -> DRAINING; the table CHECK keeps fingerprint+freeze non-NULL."""
    if existing_status != "RUNNING":
        raise ActivationError(
            f"only a RUNNING activation can be drained (current: {existing_status!r})"
        )
    return (
        UPDATE_SQL,
        (
            "DRAINING",
            existing_fingerprint,
            existing_freeze.isoformat() if existing_freeze else None,
            note,
            EXPERIMENT_ID,
        ),
    )


def _connect(settings: Any, db_env: dict[str, str] | None = None) -> Any:
    import pg8000

    cfg = db_env or {}
    return pg8000.connect(
        host=cfg.get("DB_HOST", settings.db_host),
        port=int(cfg.get("DB_PORT", settings.db_port)),
        database=cfg.get("DB_NAME", settings.db_name),
        user=cfg.get("DB_USER", settings.db_user),
        password=cfg.get("DB_PASSWORD", settings.db_password),
        timeout=10,
    )


def _fetch(conn: Any, experiment_id: str) -> tuple[str | None, str | None, datetime | None, str | None, Any]:
    cursor = conn.cursor()
    try:
        cursor.execute(SELECT_SQL, (experiment_id,))
        row = cursor.fetchone()
    finally:
        try:
            cursor.close()
        except Exception:
            pass
    if row is None:
        return None, None, None, None, None
    return row[0], row[1], row[2], row[3], row[4]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fix the ME_LONG_V2_INVERSE_SHORT_V1 protocol fingerprint / freeze_ts",
    )
    parser.add_argument("--show", action="store_true", help="read-only status (default)")
    parser.add_argument("--activate", action="store_true", help="freeze the current fingerprint + freeze_ts")
    parser.add_argument("--drain", action="store_true", help="RUNNING -> DRAINING")
    parser.add_argument("--freeze-ts", default=None, help="ISO-8601 prospective boundary for --activate")
    parser.add_argument("--re-freeze", action="store_true", help="allow replacing an existing RUNNING freeze")
    parser.add_argument("--note", default="", help="operator note stored in the activation row")
    parser.add_argument("--env-file", type=Path, default=None,
                        help="explicit dedicated runtime EnvironmentFile; required for writes")
    args = parser.parse_args(argv)

    if (args.activate or args.drain) and args.env_file is None:
        print("REFUSED: --env-file is required for production state changes", file=sys.stderr)
        return 1
    try:
        db_env = (read_service_env(args.env_file, require_disabled=args.activate)
                  if args.env_file is not None else None)
    except PreflightRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    settings = load_settings()
    fingerprint = protocol_fingerprint()
    now = datetime.now(timezone.utc)

    if args.freeze_ts is not None:
        freeze_ts: datetime | None = parse_freeze_ts(args.freeze_ts, now=now)
    else:
        freeze_ts = None

    try:
        conn = _connect(settings, db_env)
    except Exception as exc:
        print(
            f"REFUSED: cannot connect to PostgreSQL for the activation row: {exc}",
            file=sys.stderr,
        )
        return 1
    try:
        status, stored_fp, stored_freeze, notes, updated_at = _fetch(conn, EXPERIMENT_ID)

        if args.activate:
            if freeze_ts is None:
                raise ActivationError("--activate requires --freeze-ts <ISO-8601>")
            sql, params = plan_activation(
                current_fingerprint=fingerprint,
                freeze_ts=freeze_ts,
                existing_status=status,
                existing_fingerprint=stored_fp,
                existing_freeze=stored_freeze,
                re_freeze=args.re_freeze,
                note=args.note or "protocol fingerprint + freeze_ts fixed",
            )
            cursor = conn.cursor()
            cursor.execute(sql, params)
            if cursor.rowcount != 1:
                raise ActivationError("activation update affected != 1 row; rolling back")
            conn.commit()
            print(
                json.dumps(
                    {
                        "action": "activate",
                        "experiment_id": EXPERIMENT_ID,
                        "status": "RUNNING",
                        "protocol_fingerprint": fingerprint,
                        "freeze_ts": freeze_ts.isoformat(),
                        "runtime_flag_required": "ME_INVERSE_SHORT_V1_ENABLED=1",
                    },
                    indent=2,
                )
            )
            return 0

        if args.drain:
            sql, params = plan_drain(
                existing_status=status,
                existing_fingerprint=stored_fp,
                existing_freeze=stored_freeze,
                note=args.note or "drained by operator",
            )
            cursor = conn.cursor()
            cursor.execute(sql, params)
            if cursor.rowcount != 1:
                raise ActivationError("activation update affected != 1 row; rolling back")
            conn.commit()
            print(json.dumps({"action": "drain", "status": "DRAINING"}, indent=2))
            return 0

        # Default: read-only status.
        report = render_status(
            current_fingerprint=fingerprint,
            stored_status=status,
            stored_fingerprint=stored_fp,
            stored_freeze=stored_freeze,
            now=now,
            runtime_enabled=(db_env["ME_INVERSE_SHORT_V1_ENABLED"] == "1"
                             if db_env is not None else is_runtime_enabled()),
        )
        report["stored_notes"] = notes
        report["stored_updated_at"] = updated_at.isoformat() if updated_at else None
        print(json.dumps(report, indent=2))
        return 0
    except ActivationError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        try:
            conn.rollback()
        except Exception:
            pass
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
