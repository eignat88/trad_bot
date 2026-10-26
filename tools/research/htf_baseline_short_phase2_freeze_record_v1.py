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
claim  (operator GO)  Two explicit paths (FIX-1):
                      * INITIAL ACTIVATION — the database is still PENDING:
                        validates the record against the PostgreSQL acceptance
                        window ``now()-60s .. now()+5min`` (tool side AND
                        inside the function), seals the CLAIM RECEIPT (the
                        trusted source of this record's identity, written
                        exactly once BEFORE the activation transaction commits
                        — FIX-4), claims the timestamp via
                        research.htf_baseline_short_phase2_activate() (the ONLY
                        sanctioned activation entry point), then writes the
                        private runtime activation JSON and verifies exact
                        agreement of all three freeze authorities.
                      * DELAYED REPAIR ``--repair-activation-json`` — the
                        database ALREADY carries a committed claim: re-derives
                        the activation JSON ONLY from the freeze record whose
                        identity (record_id AND record_sha256) is bound to the
                        original claim by its claim receipt and that matches
                        the unchanged DB record and its immutable
                        freeze_registry evidence EXACTLY (experiment id,
                        freeze timestamp, protocol fingerprint). The freshness
                        window is not re-applied (the original claim passed it
                        transactionally); no activation is performed and a NEW
                        timestamp can never be introduced. Any mismatch — and
                        any case where the original identity cannot be
                        established — fails closed BEFORE the activation JSON
                        is written. A second record minted in the same second
                        (identical freeze_ts + fingerprint, different
                        record_id/record_sha256) is therefore always refused;
                        the record's self-hash is tamper evidence ONLY and is
                        never treated as proof of provenance.
                      Without --repair-activation-json an existing activation
                      JSON is never overwritten.
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
  * the claim receipt is sealed exactly once, before the activation commit,
    and is never overwritten (only a byte-identical re-seal of the SAME record
    is accepted); delayed repair requires an exact record_id + record_sha256
    match against it and fails closed when it is missing;
  * every immutable artifact (freeze record, claim receipt, initial activation
    JSON) is published with atomic NO-CLOBBER semantics (FIX-6: ``os.link``,
    never ``os.replace``) — a concurrent claimer can never overwrite a
    successfully published artifact; only --repair-activation-json may
    replace an activation JSON on disk;
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
    build_claim_receipt,
    build_freeze_record,
    canonical_record_json,
    freeze_record_hash,
    load_claim_receipt,
    load_freeze_record,
    validate_claim_receipt,
    validate_freeze_record,
    verify_authority_agreement,
    verify_claim_identity,
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
    """Atomic, symlink-safe JSON write with explicit permissions (FIX-2).

    Security properties:
      * never writes through a symlink — the target and its parent directory
        must not be symlinks (prevents swapping a link into place of an
        artifact);
      * never clobbers an existing file unless ``overwrite=True`` — that flag
        is used ONLY for the activation-JSON repair path; the immutable freeze
        record is always written with ``overwrite=False`` and can therefore
        never be replaced;
      * FIX-6: with ``overwrite=False`` the publication is atomic NO-CLOBBER
        (``os.link``), so even a concurrent writer that publishes between the
        existence check and the publication cannot be overwritten — see
        ``_atomic_write_bytes``;
      * the temp file is created with ``O_EXCL`` so a pre-existing or
        symlinked temp file is refused instead of clobbered;
      * the temp file is fsynced before publication and carries mode 0600 on
        POSIX; the temp name is removed on every error path.
    """
    payload_text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _atomic_write_bytes(path, payload_text, overwrite=overwrite)


def _atomic_write_bytes(path: str | Path, text: str, *, overwrite: bool) -> None:
    """Atomic, symlink-safe raw-text write (same properties as above).

    FIX-4: the claim receipt is sealed through THIS primitive — deliberately
    a separate write path from ``_atomic_write_json`` — so the one-time
    identity seal and the derived activation JSON remain independent
    artifacts: an interruption of the activation-JSON write never invalidates
    the already-sealed receipt, and the receipt is never rewritten by any
    later step.

    FIX-6 atomic no-clobber publication: for ``overwrite=False`` the file is
    published with ``os.link`` — NEVER ``os.replace``. ``os.link`` fails with
    ``FileExistsError`` when the destination exists ATOMICALLY at link time,
    so a concurrently published artifact can never be overwritten: exactly
    one competing publication wins and the winner's bytes are the final
    content. Rename-based no-clobber is deliberately NOT used — ``os.rename``
    refuses an existing destination on Windows but clobbers it on POSIX —
    while ``os.link`` is no-clobber on both platforms (CreateHardLinkW /
    ``link(2)``), proven by tests on each. If the filesystem cannot provide
    hard-link publication the write fails closed. The temp name is removed on
    EVERY path — success, interrupted write, or publication conflict.
    """
    target = Path(str(path))
    if target.is_symlink():
        raise ToolRefused(f"{target} is a symlink; refusing to write through it")
    if target.parent.is_symlink():
        raise ToolRefused(f"{target.parent} is a symlink; refusing to write into it")
    if not target.parent.is_dir():
        raise ToolRefused(f"{target.parent} is not a directory; refusing to write")
    if target.exists() and not overwrite:
        raise ToolRefused(
            f"{target} already exists; refusing to overwrite an activation "
            "artifact (use --repair-activation-json only for the documented "
            "interrupted-activation recovery)"
        )
    tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    if tmp.exists() or tmp.is_symlink():
        raise ToolRefused(
            f"{tmp} already exists; refusing to clobber a pre-existing temp "
            "file (possible squatting)"
        )
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(str(tmp), 0o600)
        if overwrite:
            # Authorized activation-JSON repair path ONLY (FIX-1/FIX-6): the
            # one documented place where re-derived bytes replace an existing
            # artifact on disk.
            os.replace(str(tmp), str(target))
        else:
            # Immutable publication (freeze record, claim receipt, initial
            # activation JSON): atomic NO-CLOBBER — never os.replace().
            try:
                os.link(str(tmp), str(target))
            except FileExistsError as exc:
                raise ToolRefused(
                    f"{target} already exists (a concurrent publication won "
                    "the race); refusing to overwrite an immutable artifact"
                ) from exc
            except OSError as exc:
                raise ToolRefused(
                    f"cannot atomically publish {target} without clobbering "
                    f"({exc}); the filesystem must support hard links — "
                    "refusing to write (fail closed)"
                ) from exc
    finally:
        # FIX-6: the temp name never survives on ANY path — success (only the
        # target name may remain), interrupted write, or publication conflict.
        try:
            os.unlink(str(tmp))
        except OSError:
            pass


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
    """Structural record validation; freshness only when ``now_ms`` is given.

    ``now_ms=None`` validates integrity only (self-hash, canonical form,
    forbidden legacy timestamps, pinned fingerprint, experiment id). The
    PostgreSQL ``now()-60s .. now()+5min`` window is enforced ONLY for the
    initial activation (FIX-1): a repair after an already-committed claim must
    keep working once that window has expired, because the immutable database
    record is the evidence that the original claim passed the window when it
    mattered.
    """
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


def _registry_row(conn, freeze_ts: str):
    """Durable, immutable evidence of the committed claim (freeze_registry).

    ``research.htf_baseline_short_phase2_freeze_registry`` is append-only and
    keyed by ``freeze_ts``; its row proves that the boundary was claimed once
    and binds it to an experiment id and protocol fingerprint.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT experiment_id, protocol_fingerprint FROM "
            "research.htf_baseline_short_phase2_freeze_registry "
            "WHERE freeze_ts = %s",
            (freeze_ts,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
    if row is None:
        return None
    return {"experiment_id": row[0], "protocol_fingerprint": row[1]}


def _require_activation_path_distinct_from_record(args) -> None:
    """FIX-2: the immutable freeze record is never overwritten, even by a
    mis-pointed ``--activation-out``."""
    record_path = Path(str(args.record)).resolve()
    if Path(str(args.activation_out)).resolve() == record_path:
        raise ToolRefused(
            "--activation-out must not point at the freeze record itself: the "
            "freeze record is immutable and is never overwritten"
        )
    if _claim_receipt_path(args.activation_out).resolve() == record_path:
        raise ToolRefused(
            "--activation-out must not place the claim receipt on the freeze "
            "record itself: the freeze record is immutable and is never "
            "overwritten"
        )


# -------------------------------------------------- claim receipt identity seal
def _claim_receipt_path(activation_out: str | Path) -> Path:
    """The claim receipt lives next to the runtime activation JSON (authority
    3) it authenticates: ``<activation-out>.claim_receipt.json``."""
    return Path(str(activation_out) + ".claim_receipt.json")


def _seal_claim_receipt(activation_out: str | Path, record: dict) -> Path:
    """FIX-4: seal the ORIGINAL record identity at the initial claim.

    The receipt is written EXACTLY ONCE and strictly BEFORE the ``activate()``
    transaction commits, so every committed claim provably carries the identity
    of the freeze record that was claimed: a second ``mint`` in the same second
    (identical ``freeze_ts`` + fingerprint, different ``record_id`` /
    ``record_sha256``) can never be mistaken for the original record later.

    A pre-existing receipt is accepted ONLY when it is byte-identical to the
    receipt of THIS record (idempotent retry of the same claim after an
    interruption); any other content fails closed BEFORE the database is
    touched. The receipt is never overwritten and never re-sealed by repair.
    """
    path = _claim_receipt_path(activation_out)
    # the full receipt (WITH its ``receipt_sha256`` self-hash, exactly like the
    # freeze record file carries ``record_sha256``); the hash input itself is
    # the canonical payload WITHOUT the hash field
    text = json.dumps(build_claim_receipt(record), indent=2, sort_keys=True) + "\n"
    try:
        _atomic_write_bytes(path, text, overwrite=False)
    except ToolRefused:
        # Idempotent retry of the SAME record is safe and expected after an
        # interrupted claim; anything else is a fail-closed STOP.
        if path.is_symlink() or path.parent.is_symlink():
            raise ToolRefused(
                f"claim receipt {path} is (under) a symlink; refusing to read "
                "it as identity evidence — STOP and escalate"
            )
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolRefused(
                f"claim receipt {path} exists but is unreadable; refusing to "
                "claim — STOP and escalate"
            ) from exc
        if existing != text:
            raise ToolRefused(
                f"claim receipt {path} already exists with DIFFERENT content: "
                "the seal belongs to another freeze record (or has been "
                "tampered with). Refusing to claim — the seal is written once "
                "and never replaced. STOP and escalate"
            )
    return path


def _require_original_record_identity(activation_out: str | Path, record: dict) -> None:
    """FIX-4 identity gate for DELAYED REPAIR: fail closed unless the presented
    record is EXACTLY the record the original claim committed.

    The trusted source is the sealed claim receipt. The presented record's
    ``record_sha256`` self-hash is tamper evidence ONLY — it does NOT prove the
    record belongs to the original GO. When the receipt is missing, unreadable
    or bound to a different ``record_id`` / ``record_sha256`` (e.g. a second
    record minted in the same second), the original identity cannot be
    established and the repair refuses BEFORE any artifact is written.
    """
    path = _claim_receipt_path(activation_out)
    if path.is_symlink() or path.parent.is_symlink():
        raise ToolRefused(
            f"DELAYED REPAIR REFUSED: claim receipt {path} is (under) a "
            "symlink; identity evidence must be the sealed regular file. Fail "
            "closed: nothing is written; STOP and escalate"
        )
    try:
        receipt = validate_claim_receipt(load_claim_receipt(path))
        verify_claim_identity(receipt, record)
    except FreezeRecordError as exc:
        raise ToolRefused(
            f"DELAYED REPAIR REFUSED: the identity of the originally claimed "
            f"freeze record cannot be established from {path} ({exc}). The "
            "record self-hash is tamper evidence ONLY, never proof that the "
            "record belongs to the original GO. Fail closed: nothing is "
            "written; STOP and escalate — never mint or claim another record"
        ) from exc


def _committed_claim_matches(state, record) -> bool:
    """DB-BOUNDARY match ONLY (status + freeze_ts + fingerprint).

    This does NOT establish record identity: a second record minted in the
    same second has the identical DB boundary with a different ``record_id`` /
    ``record_sha256``. Identity is gated separately by
    :func:`_require_original_record_identity` (FIX-4).
    """
    return (
        state.status == "RUNNING"
        and state.freeze_ts is not None
        and parse_ts_ms(state.freeze_ts) == parse_ts_ms(record["freeze_ts"])
        and state.protocol_fingerprint == record["protocol_fingerprint"]
    )


def _write_activation_and_verify(args, conn, record, *, overwrite: bool) -> int:
    """Authoritative re-read, write runtime authority 3, verify agreement."""
    state = _load_state(conn, record["experiment_id"])
    if not _committed_claim_matches(state, record):
        raise ToolRefused(
            "PARTIAL ACTIVATION: database state does not match the freeze "
            "record after the claim. Do NOT retry with another timestamp. "
            "Keep the observer stopped and escalate with this output."
        )
    if not args.activation_out:
        raise ToolRefused(
            "--activation-out is required: the runtime activation JSON is "
            "freeze authority 3 and must be deployed at the same GO"
        )
    _require_activation_path_distinct_from_record(args)
    activation = {
        "experiment_id": record["experiment_id"],
        "freeze_ts": record["freeze_ts"],
        "protocol_fingerprint": record["protocol_fingerprint"],
    }
    _atomic_write_json(args.activation_out, activation, overwrite=overwrite)
    print(f"ACTIVATION_CONFIG_WRITTEN={args.activation_out}")
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


def _repair_after_committed_claim(args, conn, record) -> int:
    """FIX-1 recovery path: repair AFTER a successfully committed claim.

    Preconditions (each checked fail-closed BEFORE any write):
      * the database registration carries exactly this record's experiment id,
        freeze timestamp and protocol fingerprint (the DB record must be the
        unchanged, originally claimed one);
      * FIX-4: the presented record is EXACTLY the record the original claim
        committed — ``record_id`` AND ``record_sha256`` must match the claim
        receipt sealed at that claim (the trusted source of identity). The
        record self-hash is tamper evidence ONLY and never counts as proof of
        provenance; a second record minted in the same second with the same
        boundary is refused, and when the identity cannot be established at
        all the repair fails closed;
      * the immutable ``freeze_registry`` row evidences the SAME claim;
      * the record itself passed structural validation (self-hash intact).

    The ``now()-60s .. now()+5min`` freshness window is deliberately NOT
    re-applied here: it gated the original activation (``activate()`` enforces
    it transactionally, and this path performs no activation at all), and the
    repair only re-derives runtime authority 3 from an already-validated
    agreement. Re-activation with a NEW timestamp is structurally impossible
    on this path — there is no ``activate()`` call, and the status/registry
    guards would reject any other timestamp anyway.
    """
    state = _load_state(conn, record["experiment_id"])
    if not _committed_claim_matches(state, record):
        raise ToolRefused(
            "database does not carry the committed claim recorded in this "
            "freeze record (state="
            f"status={state.status!r}, freeze_ts={state.freeze_ts!r}, "
            f"protocol_fingerprint={state.protocol_fingerprint!r}); refusing "
            "repair — STOP and escalate, never re-mint a timestamp"
        )
    _require_original_record_identity(args.activation_out, record)
    row = _registry_row(conn, record["freeze_ts"])
    if (
        row is None
        or row["experiment_id"] != record["experiment_id"]
        or row["protocol_fingerprint"] != record["protocol_fingerprint"]
    ):
        raise ToolRefused(
            f"freeze_registry does not evidence this exact claim (row={row}); "
            "refusing repair — the database record cannot be corroborated. "
            "STOP and escalate"
        )
    print(
        "REPAIR_MODE: database already carries the committed claim "
        f"freeze_ts={record['freeze_ts']} fingerprint={record['protocol_fingerprint']}; "
        "re-deriving activation JSON from the identical freeze record "
        "(no activation is performed)"
    )
    if args.dry_run:
        print("DRY_RUN: activation JSON NOT written")
        return EXIT_OK
    return _write_activation_and_verify(args, conn, record, overwrite=True)


def cmd_claim(args) -> int:
    _require_pinned_protocol()
    # Argument-shape checks FIRST (FIX-2/4): nothing is activated or sealed
    # for a mis-pointed --activation-out that would target the immutable
    # freeze record.
    _require_activation_path_distinct_from_record(args)
    conn = None
    try:
        conn = _connect()
        db_now = _db_now(conn)
        now_ms = parse_ts_ms(db_now)
        # Structural record validation FIRST (integrity only, no freshness):
        # whether the window applies depends on which path is taken, and that
        # is decided by the authoritative database state below (FIX-1).
        record = _load_and_validate_record(args, now_ms=None)
        state = _load_state(conn, record["experiment_id"])

        if _committed_claim_matches(state, record):
            # Path B: recovery after an already-committed identical claim.
            if not args.repair_activation_json:
                raise ToolRefused(
                    "database is ALREADY activated on this freeze boundary; "
                    "nothing to claim. If the activation JSON is missing "
                    "(interrupted activation), re-run with "
                    "--repair-activation-json using the ORIGINAL claim record "
                    "— never with a NEW timestamp and never with another "
                    "record on the same boundary"
                )
            return _repair_after_committed_claim(args, conn, record)
        if args.repair_activation_json:
            raise ToolRefused(
                "--repair-activation-json requires an already-committed, "
                "identical database claim; this database carries none. Repair "
                "is never combined with the initial activation — run a plain "
                "claim while the record is inside the acceptance window"
            )

        # Path A: initial claim — the strict PostgreSQL acceptance window
        # now()-60s .. now()+5min applies in full (tool-side AND inside
        # research.htf_baseline_short_phase2_activate()).
        record = _load_and_validate_record(args, now_ms=now_ms)
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
        # FIX-4: seal the identity of THIS record BEFORE the activation
        # transaction commits. Every committed claim therefore provably carries
        # the claim receipt of exactly one record: a same-second second mint
        # (different record_id / record_sha256) can never be presented later as
        # the original, and an interrupted seal leaves the DB un-claimed.
        receipt_path = _seal_claim_receipt(args.activation_out, record)
        print(f"CLAIM_RECEIPT_SEALED={receipt_path}")
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
        return _write_activation_and_verify(args, conn, record, overwrite=False)
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
        "from the ORIGINAL claim record (its record_id/record_sha256 must match "
        "the sealed claim receipt and the database exactly); any other record "
        "fails closed",
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
