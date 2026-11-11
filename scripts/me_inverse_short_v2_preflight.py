#!/usr/bin/env python3
"""Non-mutating pre-activation audit for ME_LONG_V2_INVERSE_SHORT_V2.

This tool NEVER activates the contour and NEVER writes to the database.
It uses the dedicated systemd EnvironmentFile explicitly; it does not
silently fall back to config.yaml or the scanner's process environment.

P1-3 design — SIX independent checks, each reported as exactly one of
``PASS`` / ``FAIL`` / ``NOT_VERIFIED``:

``DB_READY``
    Dedicated PostgreSQL credentials reach the database, the migration-087
    V2 schema exists, and the read succeeds inside ``BEGIN TRANSACTION
    READ ONLY`` (no DML is possible).
``V2_RUNTIME_CONFIG_READY``
    The dedicated runtime EnvironmentFile exists, parses (no export lines,
    no duplicates), defines all required keys, has flag ``0``, a sane port
    and the expected database name.
``V2_SOURCE_CAPTURE_READY``
    The production source path audit proves ``scanner_runner`` delivers the
    pre-gate ``_me_inverse_short_source_events`` publication to BOTH the V1
    and the V2 hook, AND the Scanner process's own environment (the second
    process) is consistent with the runtime environment (flag configured
    and OFF at preflight; DB tuple comparable and equal).
``V2_ACTIVATION_READY``
    The activation row is exactly ``READY_TO_START`` with NULL fingerprint
    and NULL freeze, and event/position/close_outbox are empty (no OOS
    data can exist before activation).
``PROTOCOL_FINGERPRINT_VALID``
    ``protocol_fingerprint()`` equals the frozen reviewed fingerprint and
    the paper/prospective/backfill/direction invariants hold.
``V1_ISOLATION_VALID``
    The V2 deployment surface cannot touch V1: the V2 runtime env carries
    no V1 flag key, the V2 systemd unit's paths/EnvironmentFile/ExecStart
    are disjoint from the V1 unit's, and the V1 capture hook still exists
    in ``scanner_runner``.

Status precedence (worst wins within a compound check):
``FAIL`` > ``NOT_VERIFIED`` > ``PASS``.

``NOT_VERIFIED`` NEVER counts as satisfied: ``ready_for_activation`` is
True only when ALL SIX checks are ``PASS`` in this run.  The report
contains only check codes and details — never credentials, passwords or
environment values — and the only database statements executed are
``SELECT``/``to_regclass`` inside a read-only transaction that is always
rolled back.

Compatibility exports (used by ``scripts/me_inverse_short_v2_activate.py``):
``read_service_env``, ``PreflightRefused``.
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
from app.paper.me_long_v2_inverse_short_v2.source_path import audit_source_path

ENV_KEYS = frozenset({
    "ME_INVERSE_SHORT_V2_ENABLED", "DB_HOST", "DB_PORT", "DB_NAME",
    "DB_USER", "DB_PASSWORD",
})
DB_KEYS = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
V1_FLAG_KEY = "ME_INVERSE_SHORT_V1_ENABLED"
V2_FLAG_KEY = "ME_INVERSE_SHORT_V2_ENABLED"
TABLE = "dds.me_long_v2_inverse_short_v2_"

# The frozen, reviewed fingerprint of the V2 protocol (pinned here for the
# preflight; tests cross-check this pin against protocol_fingerprint() and
# the isolation suite).  A mismatch means the deployed code differs from
# the reviewed spec — activation must stop.
PINNED_PROTOCOL_FINGERPRINT = (
    "5a9047ca3621cefb4d53b45275ad6348887440568cb055f843f85e2eb1abdacc"
)

CHECK_NAMES = (
    "DB_READY",
    "V2_RUNTIME_CONFIG_READY",
    "V2_SOURCE_CAPTURE_READY",
    "V2_ACTIVATION_READY",
    "PROTOCOL_FINGERPRINT_VALID",
    "V1_ISOLATION_VALID",
)
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_NOT_VERIFIED = "NOT_VERIFIED"

# Default file locations (documented in the V2 runbook).
DEFAULT_RUNTIME_ENV = Path("/etc/trad-bot/me-inverse-short-v2-runtime.env")
DEFAULT_SCANNER_ENV = Path("/opt/trad_bot/.env")
V1_UNIT_NAME = "trad-bot-me-inverse-short-runtime.service"
V2_UNIT_NAME = "trad-bot-me-inverse-short-v2-runtime.service"

# read_service_env failures that mean "configuration is violated" (FAIL)
# versus "we cannot verify it here" (NOT_VERIFIED).
_CONFIG_UNVERIFIABLE = {"SERVICE_ENV_UNREADABLE"}


class PreflightRefused(RuntimeError):
    """A safe, secret-free explanation for refusal."""


def _check(status: str, *, reason: str = "", detail: str = "") -> dict[str, str]:
    return {"status": status, "reason": reason, "detail": detail}


def _combine(subs: dict[str, tuple[str, str, str]]) -> dict[str, str]:
    """Compound check: worst status wins (FAIL > NOT_VERIFIED > PASS)."""
    statuses = [status for status, _, _ in subs.values()]
    if STATUS_FAIL in statuses:
        overall = STATUS_FAIL
    elif STATUS_NOT_VERIFIED in statuses:
        overall = STATUS_NOT_VERIFIED
    else:
        overall = STATUS_PASS
    reasons = [
        f"{name}:{reason}"
        for name, (status, reason, _) in subs.items()
        if status != STATUS_PASS and reason
    ]
    details = [
        f"{name}:{detail}" if detail else name
        for name, (status, _, detail) in subs.items()
        if status == STATUS_PASS and detail
    ]
    return _check(
        overall,
        reason=", ".join(reasons),
        detail="; ".join(details),
    )


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
    if values[V2_FLAG_KEY] not in ("0", "1"):
        raise PreflightRefused("SERVICE_FLAG_INVALID")
    if require_disabled and values[V2_FLAG_KEY] != "0":
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


def _read_scanner_env_keys(path: Path) -> dict[str, Any]:
    """Lenient read of the SECOND process's environment file.

    The Scanner's environment file is owned by the Scanner service and may
    contain unrelated keys and syntax this audit does not model, so only
    the V2-relevant keys are extracted and everything else is ignored —
    and no value is ever printed.  Returns ``{"available": bool, "keys":
    {...}, "duplicate": bool}``.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"available": False, "keys": {}, "duplicate": False}
    keys: dict[str, str] = {}
    duplicate = False
    wanted = {V2_FLAG_KEY, *DB_KEYS}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        if key not in wanted:
            continue
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        if key in keys:
            duplicate = True
        keys[key] = raw
    return {"available": True, "keys": keys, "duplicate": duplicate}


# ---------------------------------------------------------------------------
# Individual checks (each returns {status, reason, detail})
# ---------------------------------------------------------------------------
def _runtime_config_check(path: Path) -> tuple[dict[str, str], dict | None]:
    try:
        values = read_service_env(path)
    except PreflightRefused as exc:
        code = str(exc)
        if code in _CONFIG_UNVERIFIABLE:
            return (
                _check(
                    STATUS_NOT_VERIFIED,
                    reason="SERVICE_ENV_NOT_INSTALLED",
                    detail="dedicated runtime EnvironmentFile unreadable/absent",
                ),
                None,
            )
        return _check(STATUS_FAIL, reason=code), None
    return (
        _check(
            STATUS_PASS,
            detail="dedicated runtime EnvironmentFile parsed: flag OFF, "
                   "required keys complete, port/DB name valid",
        ),
        values,
    )


def _protocol_check() -> dict[str, str]:
    fingerprint = protocol_fingerprint()
    if fingerprint != PINNED_PROTOCOL_FINGERPRINT:
        return _check(
            STATUS_FAIL, reason="PROTOCOL_FINGERPRINT_MISMATCH",
            detail="deployed fingerprint differs from the reviewed pin",
        )
    if not (PAPER_ONLY and PROSPECTIVE_ONLY and not HISTORICAL_BACKFILL):
        return _check(STATUS_FAIL, reason="PROTOCOL_NOT_PAPER_PROSPECTIVE")
    if (SOURCE_SCANNER, SOURCE_DIRECTION, TRADE_DIRECTION) != (
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", "LONG", "SHORT",
    ):
        return _check(STATUS_FAIL, reason="SOURCE_DIRECTION_MISMATCH")
    return _check(
        STATUS_PASS,
        detail="fingerprint equals the reviewed pin; paper-only, "
               "prospective-only, no backfill, LONG->SHORT mapping intact",
    )


def _source_capture_check(
    repo_root: Path,
    runtime_values: dict | None,
    scanner_env_path: Path,
) -> dict[str, str]:
    subs: dict[str, tuple[str, str, str]] = {}

    # 1) Static production-path audit (both hooks fed pre-gate publication).
    audit = audit_source_path(repo_root)
    if audit["wired"]:
        subs["static_wiring"] = (
            STATUS_PASS, "",
            "production loop delivers the pre-gate source publication to "
            "the V1 and V2 hooks (fail-open, V2-flag-only)",
        )
    else:
        failing = [
            (name, check) for name, check in audit["checks"].items()
            if check["status"] != STATUS_PASS
        ]
        reason = failing[0][1]["reason"] or "SOURCE_PATH_NOT_WIRED" if failing \
            else "SOURCE_PATH_NOT_WIRED"
        subs["static_wiring"] = (
            STATUS_FAIL, reason, f"failed audit check: {failing[0][0]}"
            if failing else "",
        )

    # 2) The Scanner process (second process) has the V2 flag configured
    #    and OFF at preflight time.
    scanner = _read_scanner_env_keys(scanner_env_path)
    if not scanner["available"]:
        subs["scanner_flag"] = (
            STATUS_NOT_VERIFIED, "SCANNER_ENV_UNAVAILABLE",
            "scanner environment file unreadable/absent",
        )
    elif scanner["duplicate"]:
        subs["scanner_flag"] = (
            STATUS_FAIL, "SCANNER_ENV_DUPLICATE_KEY", "",
        )
    elif V2_FLAG_KEY not in scanner["keys"]:
        subs["scanner_flag"] = (
            STATUS_NOT_VERIFIED, "SCANNER_FLAG_NOT_CONFIGURED",
            f"{V2_FLAG_KEY} not present in the Scanner environment",
        )
    elif scanner["keys"][V2_FLAG_KEY] == "1":
        subs["scanner_flag"] = (
            STATUS_FAIL, "SCANNER_FLAG_NOT_OFF",
            "the Scanner would capture before activation approval",
        )
    elif scanner["keys"][V2_FLAG_KEY] != "0":
        subs["scanner_flag"] = (
            STATUS_FAIL, "SCANNER_FLAG_INVALID", "",
        )
    else:
        subs["scanner_flag"] = (
            STATUS_PASS, "", "Scanner flag configured and OFF at preflight",
        )

    # 3) Two-process DB consistency: both environments must point at the
    #    same database (compared WITHOUT printing any value).
    scanner_keys = scanner["keys"]
    if runtime_values is None:
        subs["db_consistency"] = (
            STATUS_NOT_VERIFIED, "RUNTIME_CONFIG_UNAVAILABLE",
            "runtime EnvironmentFile unavailable for comparison",
        )
    elif all(key in scanner_keys for key in DB_KEYS):
        if all(scanner_keys[key] == runtime_values[key] for key in DB_KEYS):
            subs["db_consistency"] = (
                STATUS_PASS, "",
                "Scanner and runtime EnvironmentFiles resolve the same "
                "database tuple",
            )
        else:
            subs["db_consistency"] = (
                STATUS_FAIL, "SCANNER_RUNTIME_DB_MISMATCH",
                "the two processes would read different databases",
            )
    else:
        subs["db_consistency"] = (
            STATUS_NOT_VERIFIED, "SCANNER_DB_KEYS_NOT_COMPARABLE",
            "the Scanner environment does not define the full DB_* tuple",
        )
    return _combine(subs)


def _v1_isolation_check(repo_root: Path, runtime_env_path: Path) -> dict[str, str]:
    subs: dict[str, tuple[str, str, str]] = {}

    # 1) The V2 runtime environment must not carry any V1 flag key.
    try:
        runtime_text = runtime_env_path.read_text(encoding="utf-8")
    except OSError:
        runtime_text = None
    if runtime_text is None:
        subs["env_surface"] = (
            STATUS_NOT_VERIFIED, "SERVICE_ENV_NOT_INSTALLED",
            "runtime EnvironmentFile unreadable/absent",
        )
    else:
        has_v1_key = any(
            line.split("=", 1)[0].strip().startswith("ME_INVERSE_SHORT_V1")
            for line in runtime_text.splitlines()
            if "=" in line and not line.strip().startswith("#")
        )
        subs["env_surface"] = (
            (STATUS_FAIL, "V1_FLAG_IN_V2_RUNTIME_ENV", "")
            if has_v1_key else
            (STATUS_PASS, "", "no V1 flag/key inside the V2 runtime env")
        )

    # 2) V2 systemd unit is installed in the checkout and its service
    #    surface (paths, env file, command) is disjoint from the V1 unit.
    v1_unit = repo_root / "deploy" / "systemd" / V1_UNIT_NAME
    v2_unit = repo_root / "deploy" / "systemd" / V2_UNIT_NAME
    if not v2_unit.is_file():
        subs["unit_isolation"] = (
            STATUS_FAIL, "V2_UNIT_ASSET_MISSING",
            f"deploy/systemd/{V2_UNIT_NAME} absent from the checkout",
        )
    elif not v1_unit.is_file():
        subs["unit_isolation"] = (
            STATUS_NOT_VERIFIED, "V1_UNIT_ASSET_MISSING",
            "cannot compare unit surfaces without the V1 unit",
        )
    else:
        v2_text = v2_unit.read_text(encoding="utf-8")
        v1_text = v1_unit.read_text(encoding="utf-8")

        def _directives(text: str) -> dict[str, list[str]]:
            found: dict[str, list[str]] = {}
            for line in text.splitlines():
                line = line.strip()
                for name in (
                    "EnvironmentFile", "WorkingDirectory", "ExecStart",
                    "SyslogIdentifier", "ConditionPathExists", "ReadWritePaths",
                ):
                    if line.startswith(name + "="):
                        found.setdefault(name, []).append(
                            line.split("=", 1)[1].strip(),
                        )
            return found

        v1_values = _directives(v1_text)
        v2_values = _directives(v2_text)
        shared_names = set(v1_values) & set(v2_values)
        collisions = sorted(
            name for name in shared_names
            if set(v1_values[name]) & set(v2_values[name])
        )
        if collisions:
            subs["unit_isolation"] = (
                STATUS_FAIL, "V1_V2_UNIT_PATH_COLLISION",
                "shared directive value(s): " + ",".join(collisions),
            )
        elif (
            "me_long_v2_inverse_short_v1" in v2_text
            or "me-inverse-short-runtime.env" in v2_text
        ):
            subs["unit_isolation"] = (
                STATUS_FAIL, "V2_UNIT_REFERENCES_V1", "",
            )
        elif not v2_values:
            subs["unit_isolation"] = (
                STATUS_FAIL, "V2_UNIT_DIRECTIVES_MISSING", "",
            )
        else:
            subs["unit_isolation"] = (
                STATUS_PASS, "",
                "V2 unit paths/EnvironmentFile/ExecStart disjoint from V1",
            )

    # 3) The V1 capture hook still exists in scanner_runner (deploying V2
    #    must not have removed or disabled V1 delivery).
    runner = repo_root / "scanner_runner.py"
    try:
        runner_text = runner.read_text(encoding="utf-8")
    except OSError:
        runner_text = None
    if runner_text is None:
        subs["v1_surface"] = (
            STATUS_NOT_VERIFIED, "SCANNER_RUNNER_UNREADABLE", "",
        )
    elif "def _capture_me_inverse_short_source(" in runner_text:
        subs["v1_surface"] = (
            STATUS_PASS, "", "V1 source capture hook present in scanner_runner",
        )
    else:
        subs["v1_surface"] = (
            STATUS_FAIL, "V1_CAPTURE_HOOK_MISSING", "",
        )
    return _combine(subs)


def _evaluate_db(
    values: dict | None, connect: Any,
) -> tuple[dict[str, str], dict[str, str]]:
    """Read-only DB checks: (DB_READY, V2_ACTIVATION_READY)."""
    if values is None:
        not_verified = _check(
            STATUS_NOT_VERIFIED, reason="CONFIG_UNAVAILABLE",
            detail="runtime EnvironmentFile unavailable; no credentials to "
                   "attempt the read-only check",
        )
        return not_verified, dict(not_verified)
    connection = None
    cursor = None
    try:
        connection = connect(
            host=values["DB_HOST"], port=int(values["DB_PORT"]),
            database=values["DB_NAME"], user=values["DB_USER"],
            password=values["DB_PASSWORD"], timeout=10,
        )
        connection.autocommit = True
        cursor = connection.cursor()
        try:
            cursor.execute("BEGIN TRANSACTION READ ONLY")
            # Schema first (migration 087):
            cursor.execute(
                "SELECT to_regclass(%s), to_regclass(%s), to_regclass(%s), "
                "to_regclass(%s)",
                (
                    f"{TABLE}activation", f"{TABLE}event",
                    f"{TABLE}position", f"{TABLE}close_outbox",
                ),
            )
            names = cursor.fetchone()
            if not names or any(name is None for name in names):
                db_ready = _check(
                    STATUS_FAIL, reason="V2_SCHEMA_MISSING",
                    detail="migration 087 objects not found in this database",
                )
                activation = _check(
                    STATUS_NOT_VERIFIED, reason="V2_SCHEMA_MISSING",
                    detail="activation row cannot be checked without the schema",
                )
                return db_ready, activation

            db_ready = _check(
                STATUS_PASS,
                detail="read-only connection succeeded; V2 schema present "
                       "(transaction rolled back)",
            )

            cursor.execute(
                "SELECT experiment_id, status, protocol_fingerprint, freeze_ts "
                f"FROM {TABLE}activation WHERE experiment_id = %s",
                (EXPERIMENT_ID,),
            )
            row = cursor.fetchone()
            if row is None:
                activation = _check(
                    STATUS_FAIL, reason="ACTIVATION_ROW_MISSING",
                )
            else:
                experiment, status, stored_fp, freeze_ts = row
                if experiment != EXPERIMENT_ID or status != "READY_TO_START":
                    activation = _check(
                        STATUS_FAIL, reason="ACTIVATION_NOT_READY",
                        detail="activation status is not READY_TO_START",
                    )
                elif stored_fp is not None or freeze_ts is not None:
                    activation = _check(
                        STATUS_FAIL, reason="ALREADY_FROZEN",
                        detail="fingerprint/freeze already written",
                    )
                else:
                    cursor.execute(
                        "SELECT "
                        f"(SELECT count(*) FROM {TABLE}event), "
                        f"(SELECT count(*) FROM {TABLE}position), "
                        f"(SELECT count(*) FROM {TABLE}close_outbox)",
                    )
                    counts = cursor.fetchone()
                    if counts is None or any(value != 0 for value in counts):
                        activation = _check(
                            STATUS_FAIL, reason="EXISTING_OOS_DATA",
                            detail="event/position/outbox not empty",
                        )
                    else:
                        activation = _check(
                            STATUS_PASS,
                            detail="READY_TO_START, fingerprint/freeze NULL, "
                                   "event/position/outbox all empty",
                        )
            return db_ready, activation
        finally:
            connection.rollback()
            if cursor is not None:
                cursor.close()
    except Exception:
        # Do NOT surface the exception text: drivers sometimes interpolate
        # credentials into it.
        db_ready = _check(
            STATUS_FAIL, reason="DATABASE_READ_ONLY_CHECK_FAILED",
            detail="connection or read-only query failed (details withheld: "
                   "driver messages may contain credentials)",
        )
        activation = _check(
            STATUS_NOT_VERIFIED, reason="DATABASE_UNAVAILABLE",
            detail="activation state not verifiable without the database",
        )
        return db_ready, activation
    finally:
        if connection is not None:
            connection.close()


def run_preflight(
    path: Path,
    *,
    connect: Any = None,
    scanner_env_path: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Run ALL SIX checks; never activates, never writes, never prints secrets."""
    repo_root = Path(repo_root) if repo_root is not None else _ROOT
    if scanner_env_path is None:
        scanner_env_path = DEFAULT_SCANNER_ENV
    if connect is None:
        # dbapi flavour: exposes cursor()/autocommit/rollback for the
        # read-only transaction below (the native flavour does not).
        import pg8000.dbapi
        connect = pg8000.dbapi.connect

    checks: dict[str, dict[str, str]] = {}

    checks["V2_RUNTIME_CONFIG_READY"], runtime_values = _runtime_config_check(path)
    checks["PROTOCOL_FINGERPRINT_VALID"] = _protocol_check()
    checks["V2_SOURCE_CAPTURE_READY"] = _source_capture_check(
        repo_root, runtime_values, scanner_env_path,
    )
    checks["DB_READY"], checks["V2_ACTIVATION_READY"] = _evaluate_db(
        runtime_values, connect,
    )
    checks["V1_ISOLATION_VALID"] = _v1_isolation_check(repo_root, path)

    # NEVER auto-PASS: ready requires every check confirmed in THIS run.
    ready = all(
        checks[name]["status"] == STATUS_PASS for name in CHECK_NAMES
    )
    return {
        "result": "READY_FOR_ACTIVATION" if ready else "NOT_READY",
        "ready_for_activation": ready,
        "checks": checks,
        "experiment_id": EXPERIMENT_ID,
        "current_protocol_fingerprint": protocol_fingerprint(),
        "activation_authorized": False,  # this tool never activates
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file", type=Path, default=DEFAULT_RUNTIME_ENV,
        help="dedicated runtime EnvironmentFile (no fallback)",
    )
    parser.add_argument(
        "--scanner-env-file", type=Path, default=DEFAULT_SCANNER_ENV,
        help="the Scanner service's environment file (second process)",
    )
    args = parser.parse_args(argv)
    try:
        result = run_preflight(args.env_file, scanner_env_path=args.scanner_env_file)
    except PreflightRefused as exc:
        print(json.dumps({"result": "REFUSED", "reason": str(exc)}))
        return 1
    except Exception:  # noqa: BLE001 - never leak driver/credential text
        print(json.dumps({"result": "REFUSED", "reason": "PREFLIGHT_INTERNAL_ERROR"}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=False))
    return 0 if result["ready_for_activation"] else 1


if __name__ == "__main__":
    sys.exit(main())
