#!/usr/bin/env python3
"""ME_INVERSE_SHORT_V3_UPSTREAM_SOURCE_CAPTURE_V1 — administrative tool.

Research-only administrative contour of the Issue #305 upstream source
capture.  The capture itself is OFF by default and starts only after BOTH
the runtime feature flag ``ME_UPSTREAM_SOURCE_CAPTURE_V1_ENABLED=1`` AND a
RUNNING activation row with a SEPARATELY fixed ``freeze_ts`` exist.

Commands (the tool writes ONLY ``research_me_upstream_coverage.activation``
and ``research_me_upstream_coverage.coverage_audit``):

  * ``--show`` (default)
        read-only status: current code fingerprint, stored activation row,
        capture state, latest coverage audit, and whether the capture would
        be ACTIVE right now.
  * ``--activate --freeze-ts <ISO8601>``
        fix the single activation row to RUNNING with the CURRENT code's
        ``protocol_fingerprint`` and the given freeze boundary.  Refuses a
        naive timestamp, a freeze in the future or older than 2 minutes, an
        already-RUNNING activation, and any rewrite of an existing freeze.
  * ``--audit``
        READ-ONLY upstream ↔ V3 coverage reconciliation, printed as JSON.
  * ``--persist-audit``
        the same reconciliation, additionally persisted to
        ``research_me_upstream_coverage.coverage_audit``.

Never: backfill, freeze reset, state recovery (see
``deploy/sql/305_upstream_source_recovery.sql``), V3/OOS-2 writes, trading
table access, or enabling the runtime flag (that belongs to the deployment
configuration and requires a separate GO).

Typical sequence (ONLY after tests pass and explicit approval):

    python scripts/me_upstream_source_v1_admin.py --show
    # Only after explicit approval; FRESH_UTC must be within 2 min of execution:
    python scripts/me_upstream_source_v1_admin.py --activate --freeze-ts <FRESH_UTC>
    python scripts/me_upstream_source_v1_admin.py --show
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Allow running as a plain script from the repository root or from scripts/.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.research.me_upstream_source_v1.protocol import (  # noqa: E402
    ACTIVATION_FLAG,
    EXPERIMENT_ID,
    PROTOCOL_VERSION,
    protocol_fingerprint,
)
from app.research.me_upstream_source_v1.store import (  # noqa: E402
    ACTIVATION_TABLE,
    STATE_TABLE,
)

SELECT_ACTIVATION_SQL = (
    "SELECT status, protocol_version, protocol_fingerprint, freeze_ts, "
    "created_at, updated_at "
    f"FROM {ACTIVATION_TABLE} WHERE experiment_id = %s"
)

# One UPDATE: status + fingerprint + freeze are fixed ATOMICALLY so a crash
# can never leave RUNNING with a stale/absent fingerprint (the table CHECK
# rejects that state anyway).
ACTIVATE_SQL = (
    f"""
    INSERT INTO {ACTIVATION_TABLE} (
        experiment_id, status, protocol_version,
        protocol_fingerprint, freeze_ts, notes
    )
    VALUES (%s, 'RUNNING', %s, %s, %s, %s)
    ON CONFLICT (experiment_id) DO UPDATE SET
        status = 'RUNNING',
        protocol_version = EXCLUDED.protocol_version,
        protocol_fingerprint = EXCLUDED.protocol_fingerprint,
        freeze_ts = EXCLUDED.freeze_ts,
        notes = EXCLUDED.notes
    WHERE activation.status <> 'RUNNING'
      AND activation.freeze_ts IS NULL
    """
)


def _connect():
    """psycopg2 connection to the research database (admin contour)."""
    import psycopg2

    from app.config import load_settings

    settings = load_settings()
    return psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        connect_timeout=10,
    )


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SystemExit("REFUSED: naive freeze timestamp (provide UTC offset)")
    return parsed.astimezone(timezone.utc)


def cmd_show(conn: Any) -> None:
    cur = conn.cursor()
    cur.execute(SELECT_ACTIVATION_SQL, (EXPERIMENT_ID,))
    row = cur.fetchone()
    cur.execute(
        f"SELECT state, reason_code, recorded_at FROM {STATE_TABLE} "
        "ORDER BY id DESC LIMIT 1"
    )
    state = cur.fetchone()

    code_fingerprint = protocol_fingerprint()
    print(json.dumps({
        "experiment_id": EXPERIMENT_ID,
        "code_protocol_version": PROTOCOL_VERSION,
        "code_protocol_fingerprint": code_fingerprint,
        "activation": (
            {
                "status": row[0],
                "protocol_version": row[1],
                "protocol_fingerprint": row[2],
                "freeze_ts": row[3].isoformat() if row[3] else None,
                "created_at": row[4].isoformat() if row[4] else None,
                "updated_at": row[5].isoformat() if row[5] else None,
                "fingerprint_matches_code": row[2] == code_fingerprint,
            }
            if row else None
        ),
        "capture_state": (
            {
                "state": state[0],
                "reason_code": state[1],
                "recorded_at": state[2].isoformat() if state[2] else None,
            }
            if state else None
        ),
        "runtime_flag": f"{ACTIVATION_FLAG}="
        f"{__import__('os').environ.get(ACTIVATION_FLAG, '<unset>')}",
    }, indent=2, default=str))


def cmd_activate(conn: Any, freeze_ts: datetime) -> None:
    now = datetime.now(timezone.utc)
    if freeze_ts > now:
        raise SystemExit("REFUSED: freeze timestamp is in the future")
    if freeze_ts < now - timedelta(minutes=2):
        raise SystemExit(
            "REFUSED: freeze timestamp is older than 2 minutes "
            "(provide a FRESH_UTC within 2 min of execution)"
        )

    cur = conn.cursor()
    cur.execute(SELECT_ACTIVATION_SQL, (EXPERIMENT_ID,))
    existing = cur.fetchone()
    if existing is not None:
        if existing[0] == "RUNNING":
            raise SystemExit(
                "REFUSED: activation is already RUNNING — the freeze boundary "
                "is immutable and is never rewritten"
            )
        if existing[3] is not None:
            raise SystemExit(
                "REFUSED: freeze_ts is already fixed — it is immutable"
            )

    cur.execute(
        ACTIVATE_SQL,
        (
            EXPERIMENT_ID,
            PROTOCOL_VERSION,
            protocol_fingerprint(),
            freeze_ts,
            "Issue #305 upstream source capture activation",
        ),
    )
    if cur.rowcount != 1:
        conn.rollback()
        raise SystemExit("REFUSED: activation row state changed concurrently")
    conn.commit()
    print(json.dumps({
        "activated": True,
        "experiment_id": EXPERIMENT_ID,
        "freeze_ts": freeze_ts.isoformat(),
        "protocol_fingerprint": protocol_fingerprint(),
    }, indent=2))


def cmd_audit(conn: Any, *, persist: bool) -> None:
    from app.research.me_upstream_source_v1.coverage import (
        reconcile_coverage,
        save_coverage_audit,
    )

    result = reconcile_coverage(conn)
    if persist:
        save_coverage_audit(conn, result)
        result["audit_persisted"] = True
    print(json.dumps(result, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--persist-audit", action="store_true")
    parser.add_argument("--freeze-ts", type=str, default=None)
    args = parser.parse_args(argv)

    conn = _connect()
    try:
        if args.activate:
            if not args.freeze_ts:
                raise SystemExit("--activate requires --freeze-ts <ISO8601>")
            cmd_activate(conn, _as_utc(args.freeze_ts))
        elif args.audit:
            cmd_audit(conn, persist=False)
        elif args.persist_audit:
            cmd_audit(conn, persist=True)
        else:
            cmd_show(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
