"""Issue #197 — Phase-2 timestamp authority: operator freeze-record tool.

This is the ONLY write path for the Phase-2 freeze timestamp. It exists so the
fresh UTC ``FREEZE_TS`` is chosen at the separate operator GO, as close to the
actual PostgreSQL activation as possible, and never during Git merge, CI or an
ordinary deployment. Nothing here runs automatically; every subcommand is an
explicit operator action and every one of them is fail-closed.

Subcommands
-----------
mint   (operator GO)  Create the immutable, self-hashed FREEZE RECORD that
                      binds (experiment_id, protocol_fingerprint, freeze_ts,
                      claimed_at, approval ref). Validates the timestamp
                      against the PostgreSQL acceptance window using the DB
                      server clock. Writes NOTHING to the database.
claim  (operator GO)  Atomically claim the timestamp in PostgreSQL via
                      research.htf_baseline_short_phase2_activate() (the ONLY
                      sanctioned activation entry point), then write the
                      private runtime activation JSON and verify exact
                      agreement of all three freeze authorities. Refuses to
                      overwrite an existing activation JSON unless
                      --repair-activation-json is given for the documented
                      interrupted-activation recovery.
verify (read-only)    Re-check the three authorities end to end.

Hard rules (any violation aborts with a non-zero exit and STOP guidance):
  * the protocol payload must match the pinned FROZEN fingerprint;
  * freeze_ts must be canonical UTC ``YYYY-MM-DDTHH:MM:SSZ``;
  * freeze_ts must be inside [db_now - 60s, db_now + 300s] at mint/claim time;
  * legacy/predecessor freeze boundaries (including the Point-B boundary
    2026-10-06T14:00:00Z) are structurally rejected and can never be re-used;
  * one record = one claim; a second claim with the same record is a no-op
    repair only, a claim with a NEW timestamp after a claim already succeeded
    is refused (the freeze registry also rejects it);
  * observer_enabled, systemd units, gate files and paper/live are NEVER
    touched by this tool — observe-only startup is issue #198.

Usage (from the repository root, as the dedicated admin role via HTF_P2_DB_*)::

    python tools/research/htf_baseline_short_phase2_freeze_record_v1.py mint \\
        --operator-approval-ref "GO-2026-xx (issue #198)" --issued-by operator \\
        --record-id <uuid> --out /opt/trad_bot/etc/htf_phase2_freeze_record.json
    python tools/research/htf_baseline_short_phase2_freeze_record_v1.py claim \\
        --record /opt/trad_bot/etc/htf_phase2_freeze_record.json \\
        --activation-out /opt/trad_bot/etc/htf_phase2_activation.json
    python tools/research/htf_baseline_short_phase2_freeze_record_v1.py verify \\
        --record ... --activation ...

Exit codes: 0 = OK, 1 = refused/failed (fail closed), 3 = not runnable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.research.htf_baseline_short_phase2_freeze_v1 import parse_ts_ms  # noqa: E402
from app.research.htf_baseline_short_phase2_protocol_v1 import (  # noqa: E402
    EXPERIMENT_ID,
    protocol_fingerprint,
    verify_frozen_fingerprint,
)
from app.research.htf_baseline_short_phase2_timestamp_authority_v1 import (  # noqa: E402
    ACCEPTANCE_FUTURE_WINDOW_S,
    ACCEPTANCE_PAST_WINDOW_S,
    FreezeRecordError,
    build_freeze_record,
    canonical_record_json,
    freeze_record_hash,
    load_freeze_record,
    validate_freeze_record,
    verify_authority_agreement,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_NOT_RUNNABLE = 3

ADMIN_ROLE = "trad_bot_htf_p2_admin"

#: Legacy freeze boundaries that must NEVER be re-used by Phase-2. The Point-B
#: prospective freeze is the known one; the freeze registry additionally makes
#: every re-used timestamp structurally impossible at the database boundary.
FORBIDDEN_LEGACY_FREEZE_TS = ("2026-10-06T14:00:00Z",)
FORBIDDEN_FREEZE_TS_MS = frozenset(parse_ts_ms(ts) for ts in FORBIDDEN_LEGACY_FREEZE_TS)


class ToolRefused(RuntimeError):
    """Operator action refused (fail closed) with actionable STOP guidance."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect():
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        connect_db_from_env,
    )

    return connect_db_from_env()


def _close(conn) -> None:
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        close_db_connection,
    )

    close_db_connection(conn)


def _db_now(conn) -> datetime:
    cur = conn.cursor()
    try:
        cur.execute("SELECT now()")
        row = cur.fetchone()
    finally:
        cur.close()
    return row[0]


def _load_state(conn, experiment_id: str):
    from app.research.htf_baseline_short_phase2_persistence_v1 import (
        load_experiment_state,
    )

    return load_experiment_state(conn, experiment_id)


def _atomic_write_json(path: str | Path, payload: dict, *, overwrite: bool) -> None:
    target = Path(str(path))
    if target.exists() and not overwrite:
        raise ToolRefused(
            f"{target} already exists; refusing to overwrite an activation "
            "artifact (use --repair-activation-json only for the documented "
            "interrupted-activation recovery)"
        )
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def _require_pinned_protocol() -> str:
    fingerprint = protocol_fingerprint()
    if not verify_frozen_fingerprint():
        raise ToolRefused(
            "running protocol payload fingerprint "
            f"{fingerprint} != pinned FROZEN_PROTOCOL_FINGERPRINT — the "
            "deployed code is not the reviewed frozen protocol; STOP"
        )
    return fingerprint


# --------------------------------------------------------------------- mint
def cmd_mint(args) -> int:
    fingerprint = _require_pinned_protocol()
    if not args.operator_approval_ref or not args.issued_by or not args.record_id:
        raise ToolRefused(
            "--operator-approval-ref, --issued-by and --record-id are mandatory: "
            "the freeze record must name the human GO that authorized it"
        )
    try:
        uuid.UUID(str(args.record_id))
    except ValueError as exc:
        raise ToolRefused("--record-id must be a UUID (one record = one claim)") from exc

    conn = None
    try:
        conn = _connect()
        db_now = _db_now(conn)
    finally:
        if conn is not None:
            _close(conn)
    now_ms = parse_ts_ms(db_now)
    claimed_at = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    freeze_ts = args.freeze_ts or claimed_at
    record = build_freeze_record(
        experiment_id=args.experiment_id,
        freeze_ts=freeze_ts,
        protocol_fingerprint=fingerprint,
        claimed_at=claimed_at,
        operator_approval_ref=args.operator_approval_ref,
        issued_by=args.issued_by,
        record_id=str(args.record_id),
        note=args.note,
    )
    validate_freeze_record(
        record,
        expected_experiment_id=args.experiment_id,
        expected_protocol_fingerprint=fingerprint,
        now_ms=now_ms,
        forbidden_freeze_ts_ms=FORBIDDEN_FREEZE_TS_MS,
        require_pinned_fingerprint=True,
    )
    print(canonical_record_json(record))
    print(f"record_sha256={record['record_sha256']}")
    print(f"db_now={db_now.isoformat()} freeze_ts={record['freeze_ts']}")
    if args.dry_run:
        print("DRY_RUN: record NOT written")
        return EXIT_OK
    if not args.out:
        raise ToolRefused("--out is required to persist the freeze record")
    _atomic_write_json(args.out, record, overwrite=False)
    print(f"FREEZE_RECORD_WRITTEN={args.out}")
    print(
        "NEXT: at the same operator GO run `claim`; do NOT enable the observer, "
        "create gate files or start any unit (issue #198)."
    )
    return EXIT_OK


# -------------------------------------------------------------------- claim
def _load_and_validate_record(args, now_ms: int | None) -> dict:
    record = load_freeze_record(args.record)
    validate_freeze_record(
        record,
        expected_experiment_id=EXPERIMENT_ID,
        expected_protocol_fingerprint=protocol_fingerprint(),
        now_ms=now_ms,
        forbidden_freeze_ts_ms=FORBIDDEN_FREEZE_TS_MS,
        require_pinned_fingerprint=True,
    )
    return record


def cmd_claim(args) -> int:
    _require_pinned_protocol()
    conn = None
    try:
        conn = _connect()
        db_now = _db_now(conn)
        now_ms = parse_ts_ms(db_now)
        record = _load_and_validate_record(args, now_ms)
        state = _load_state(conn, record["experiment_id"])
        already = (
            state.status == "RUNNING"
            and state.freeze_ts is not None
            and parse_ts_ms(state.freeze_ts) == parse_ts_ms(record["freeze_ts"])
            and state.protocol_fingerprint == record["protocol_fingerprint"]
        )
        if already:
            if not args.repair_activation_json:
                raise ToolRefused(
                    "database is ALREADY activated with exactly this freeze "
                    "record; nothing to claim. If the activation JSON is "
                    "missing (interrupted activation), re-run with "
                    "--repair-activation-json — never with a NEW timestamp"
                )
        else:
            if state.status not in ("PENDING", "READY_TO_START"):
                raise ToolRefused(
                    f"experiment registration is {state.status!r}: activation "
                    "refused. If a concurrent/earlier attempt already claimed a "
                    "freeze, STOP and escalate — never mint a new timestamp to "
                    "'fix' a half-finished activation"
                )
            if state.freeze_ts is not None or state.protocol_fingerprint:
                raise ToolRefused(
                    "registration already carries freeze/fingerprint values "
                    "that do not match this record; STOP and escalate"
                )
            if args.dry_run:
                print(
                    f"DRY_RUN: would activate freeze_ts={record['freeze_ts']} "
                    f"fingerprint={record['protocol_fingerprint']}"
                )
                return EXIT_OK
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
                    (
                        record["experiment_id"],
                        record["freeze_ts"],
                        record["protocol_fingerprint"],
                    ),
                )
                cur.fetchone()
            finally:
                cur.close()
            conn.commit()
            print(f"DB_ACTIVATED freeze_ts={record['freeze_ts']}")

        # post-claim authoritative read (the DB is now the durable claim)
        state = _load_state(conn, record["experiment_id"])
        if (
            state.status != "RUNNING"
            or state.freeze_ts is None
            or parse_ts_ms(state.freeze_ts) != parse_ts_ms(record["freeze_ts"])
            or state.protocol_fingerprint != record["protocol_fingerprint"]
        ):
            raise ToolRefused(
                "PARTIAL ACTIVATION: database state does not match the freeze "
                "record after the claim. Do NOT retry with another timestamp. "
                "Keep the observer stopped and escalate with this output."
            )
        if args.activation_out:
            activation = {
                "experiment_id": record["experiment_id"],
                "freeze_ts": record["freeze_ts"],
                "protocol_fingerprint": record["protocol_fingerprint"],
            }
            _atomic_write_json(
                args.activation_out,
                activation,
                overwrite=bool(already and args.repair_activation_json),
            )
            print(f"ACTIVATION_CONFIG_WRITTEN={args.activation_out}")
        else:
            raise ToolRefused(
                "--activation-out is required: the runtime activation JSON is "
                "freeze authority 3 and must be deployed at the same GO"
            )
        activation = json.loads(Path(str(args.activation_out)).read_text(encoding="utf-8"))
        verify_authority_agreement(
            record=record,
            database_freeze_ts=state.freeze_ts,
            database_fingerprint=state.protocol_fingerprint,
            activation=activation,
            expected_experiment_id=record["experiment_id"],
            expected_protocol_fingerprint=record["protocol_fingerprint"],
        )
        print("THREE_AUTHORITY_AGREEMENT=OK")
        print(f"record_sha256={record['record_sha256']}")
        print(
            "STOP NOTE: activation complete. Do NOT set observer_enabled=true, "
            "create /opt/trad_bot/etc/htf_phase2_observer_enabled or start the "
            "systemd unit here — observe-only startup is a separate GO (issue #198)."
        )
        return EXIT_OK
    except FreezeRecordError as exc:
        raise ToolRefused(str(exc)) from exc
    finally:
        if conn is not None:
            _close(conn)


# ------------------------------------------------------------------- verify
def cmd_verify(args) -> int:
    _require_pinned_protocol()
    conn = None
    try:
        conn = _connect()
        record = _load_and_validate_record(args, now_ms=None)
        state = _load_state(conn, record["experiment_id"])
        activation = json.loads(Path(str(args.activation)).read_text(encoding="utf-8"))
        agreement = verify_authority_agreement(
            record=record,
            database_freeze_ts=state.freeze_ts,
            database_fingerprint=state.protocol_fingerprint,
            activation=activation,
            expected_experiment_id=record["experiment_id"],
            expected_protocol_fingerprint=record["protocol_fingerprint"],
        )
        print(json.dumps({"verdict": "THREE_AUTHORITY_AGREEMENT=OK", **{
            "freeze_ts_ms": agreement["freeze_ts_ms"],
            "protocol_fingerprint": agreement["protocol_fingerprint"],
            "experiment_id": agreement["experiment_id"],
            "db_status": state.status,
            "observer_enabled": state.observer_enabled,
            "record_sha256": freeze_record_hash(record),
        }}, indent=2, sort_keys=True))
        return EXIT_OK
    except FreezeRecordError as exc:
        raise ToolRefused(str(exc)) from exc
    finally:
        if conn is not None:
            _close(conn)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase-2 timestamp authority tool")
    sub = parser.add_subparsers(dest="command", required=True)

    p_mint = sub.add_parser("mint", help="create the operator freeze record")
    p_mint.add_argument("--experiment-id", default=EXPERIMENT_ID)
    p_mint.add_argument("--operator-approval-ref", required=True)
    p_mint.add_argument("--issued-by", required=True)
    p_mint.add_argument("--record-id", required=True, help="UUID; one record = one claim")
    p_mint.add_argument(
        "--freeze-ts",
        default=None,
        help="explicit freeze timestamp (canonical UTC ...Z); default = DB now",
    )
    p_mint.add_argument("--note", default=None)
    p_mint.add_argument("--out", default=None)
    p_mint.add_argument("--dry-run", action="store_true")

    p_claim = sub.add_parser("claim", help="claim the freeze in PostgreSQL + deploy activation JSON")
    p_claim.add_argument("--record", required=True)
    p_claim.add_argument("--activation-out", required=True)
    p_claim.add_argument("--dry-run", action="store_true")
    p_claim.add_argument(
        "--repair-activation-json",
        action="store_true",
        help="interrupted-activation recovery ONLY: re-derive the activation JSON "
        "from a record that already matches the database exactly",
    )

    p_verify = sub.add_parser("verify", help="read-only three-authority check")
    p_verify.add_argument("--record", required=True)
    p_verify.add_argument("--activation", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "mint":
            return cmd_mint(args)
        if args.command == "claim":
            return cmd_claim(args)
        return cmd_verify(args)
    except ToolRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as exc:  # fail closed on anything unexpected
        print(f"REFUSED (unexpected error): {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
