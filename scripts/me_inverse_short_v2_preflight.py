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
    Release-side readiness (concept ``V2_RUNTIME_CODE_READY`` plus the
    configuration): the dedicated runtime EnvironmentFile exists, parses
    (no export lines, no duplicates), defines all required keys, has flag
    ``0``, a sane port and the expected database name, AND the V2 RELEASE
    itself passes the static source-path audit (``runtime_code_ready``
    subcheck — the release carries the wired capture code).
``V2_SOURCE_CAPTURE_READY``
    Scanner-side readiness (P2): audits the ACTUAL production Scanner
    checkout given explicitly via ``--scanner-root`` (normally
    ``/opt/trad_bot`` — the V2 release directory is NEVER used as the
    Scanner source), verifies it against the installed systemd unit
    (``--scanner-unit``: ExecStart, WorkingDirectory, Python environment,
    script path, EnvironmentFile), audits V2 source routing on THAT
    checkout (an old Scanner checkout fails here even when the V2 release
    is fully ready), confirms from read-only process metadata that the
    running Scanner matches the unit and that no audited source changed
    after the process started (never restarting anything), and keeps the
    two-process flag/DB consistency checks.  When the real Scanner
    checkout, its unit or the running process cannot be confirmed, the
    check reports ``NOT_VERIFIED`` and ``ready_for_activation`` stays
    false — a static audit is never substituted for runtime evidence.
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

Effective-runtime honesty (P2-4): the Scanner process is inspected with
read-only metadata only (``systemctl show`` + ``/proc/<pid>``) — this tool
never restarts anything.  Exact identity of the *loaded* bytecode cannot
be proven without an approved Scanner restart or additional diagnostics;
the preflight says so instead of pretending (see runbook Step 9).

Compatibility exports (used by ``scripts/me_inverse_short_v2_activate.py``):
``read_service_env``, ``PreflightRefused``.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
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
# P2-1: the Scanner audit target is ALWAYS an explicit production checkout
# (consistent with the systemd unit below) — never the V2 release directory.
DEFAULT_SCANNER_ROOT = Path("/opt/trad_bot")
DEFAULT_SCANNER_UNIT = Path("/etc/systemd/system/trad-bot-scanner.service")
V1_UNIT_NAME = "trad-bot-me-inverse-short-runtime.service"
V2_UNIT_NAME = "trad-bot-me-inverse-short-v2-runtime.service"
SCANNER_UNIT_NAME = "trad-bot-scanner.service"

# The files audit_source_path() reads: their on-disk state must predate the
# running Scanner process for the process to be executing this audit state.
SCANNER_AUDIT_FILES = (
    "scanner_runner.py",
    "app/scanners/orchestrator.py",
    "app/paper/me_long_v2_inverse_short_v2/capture.py",
    "app/paper/me_long_v2_inverse_short_v2/engine.py",
    "app/paper/me_long_v2_inverse_short_v2/protocol.py",
)

# Sentinel: "detect the running Scanner process at runtime" (tests inject
# fixture process metadata explicitly instead — they never touch /proc).
_AUTO_PROCESS = object()

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
# Scanner unit / effective-runtime inspection (P2-1, P2-4) — READ-ONLY.
# Nothing here starts, stops, restarts or reloads any unit; only the unit
# FILE is read and (at runtime) `systemctl show` + /proc/<pid> metadata.
# ---------------------------------------------------------------------------
def _unit_directives(text: str) -> dict[str, list[str]]:
    """Extract the directives this audit compares (all values, no secrets)."""
    found: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        for name in (
            "EnvironmentFile", "WorkingDirectory", "ExecStart",
            "SyslogIdentifier", "ConditionPathExists", "ReadWritePaths",
        ):
            if line.startswith(name + "="):
                found.setdefault(name, []).append(line.split("=", 1)[1].strip())
    return found


def _read_unit_directives(path: Path) -> dict[str, list[str]] | None:
    """Parse the installed systemd unit; None when it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _unit_directives(text)


def _detect_scanner_process(unit_path: Path) -> dict[str, Any]:
    """Read-only metadata of the RUNNING Scanner process (never restarts).

    Returns ``{"available": False, "reason": ...}`` when this host cannot
    provide the metadata (non-systemd host, permission denied, unit not
    loaded) — the preflight then reports NOT_VERIFIED instead of guessing.
    A file on disk proves nothing about the executing process by itself.
    """
    try:
        shown = subprocess.run(
            ["systemctl", "show", unit_path.name, "--no-pager",
             "--property=ActiveState,SubState,MainPID,FragmentPath"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "reason": "SYSTEMCTL_UNAVAILABLE"}
    if shown.returncode != 0:
        return {"available": False, "reason": "SYSTEMCTL_SHOW_FAILED"}
    props: dict[str, str] = {}
    for line in shown.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value
    try:
        pid = int(props.get("MainPID", "0") or 0)
    except ValueError:
        pid = 0
    fragment = props.get("FragmentPath", "")
    if props.get("ActiveState") != "active" or pid <= 0:
        return {
            "available": True, "active": False, "pid": pid,
            "state": props.get("ActiveState", "unknown"), "fragment": fragment,
        }
    try:
        cmdline = [
            part.decode("utf-8", "replace")
            for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if part
        ]
        cwd = str(Path(f"/proc/{pid}/cwd").resolve())
        # /proc/<pid>/stat field 22 (0-based 19 after "comm) ") = start time
        # in clock ticks; combine with /proc/uptime for a wall-clock start.
        rest = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        start_ticks = int(rest.split(") ", 1)[1].split()[19])
        clock_ticks = float(os.sysconf("SC_CLK_TCK"))
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        started_at = time.time() - (uptime - start_ticks / clock_ticks)
    except (OSError, ValueError, IndexError):
        return {"available": False, "reason": "PROC_METADATA_UNREADABLE"}
    return {
        "available": True, "active": True, "state": "active", "pid": pid,
        "fragment": fragment, "cmdline": cmdline, "cwd": cwd,
        "started_at": started_at,
    }


def _path_text(value: str) -> str:
    """Normalize a path spelling for unit/process comparisons (no IO)."""
    try:
        return str(Path(value))
    except (TypeError, ValueError):
        return str(value)


def _path_key(value: str) -> str:
    """Canonical path key for file-backed comparisons (unit fragment,
    EnvironmentFile): equivalent spellings/symlinks must not read as
    mismatches; falls back to the plain normalization when unresolvable."""
    try:
        return str(Path(value).resolve())
    except (OSError, ValueError):
        return _path_text(value)


# ---------------------------------------------------------------------------
# Individual checks (each returns {status, reason, detail})
# ---------------------------------------------------------------------------
def _runtime_config_check(
    path: Path, repo_root: Path,
) -> tuple[dict[str, str], dict | None]:
    """Release-side readiness (P2-3): config + the V2 RELEASE source code.

    The ``runtime_code_ready`` subcheck carries the concept
    ``V2_RUNTIME_CODE_READY``: the V2 release checkout itself must pass
    the static source-path audit.  It says nothing about the production
    Scanner — that is what ``V2_SOURCE_CAPTURE_READY`` audits (Scanner
    checkout + systemd unit + running process).
    """
    subs: dict[str, tuple[str, str, str]] = {}

    try:
        values = read_service_env(path)
    except PreflightRefused as exc:
        code = str(exc)
        values = None
        if code in _CONFIG_UNVERIFIABLE:
            subs["service_env"] = (
                STATUS_NOT_VERIFIED, "SERVICE_ENV_NOT_INSTALLED",
                "dedicated runtime EnvironmentFile unreadable/absent",
            )
        else:
            subs["service_env"] = (STATUS_FAIL, code, "")
    else:
        subs["service_env"] = (
            STATUS_PASS, "",
            "dedicated runtime EnvironmentFile parsed: flag OFF, "
            "required keys complete, port/DB name valid",
        )

    # V2_RUNTIME_CODE_READY (concept): the V2 release carries the wiring.
    audit = audit_source_path(repo_root)
    if audit["wired"]:
        subs["runtime_code_ready"] = (
            STATUS_PASS, "",
            "V2 release passes the static source-path audit (pre-gate "
            "publication delivered to both hooks, fail-open, V2-flag-only)",
        )
    else:
        failing = [
            (name, check) for name, check in audit["checks"].items()
            if check["status"] != STATUS_PASS
        ]
        reason = failing[0][1]["reason"] or "SOURCE_PATH_NOT_WIRED" if failing \
            else "SOURCE_PATH_NOT_WIRED"
        subs["runtime_code_ready"] = (
            STATUS_FAIL, reason,
            f"failed release audit check: {failing[0][0]}" if failing else "",
        )
    return _combine(subs), values


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


def _scanner_source_check(
    scanner_root: Path,
    runtime_values: dict | None,
    scanner_env_path: Path,
    *,
    scanner_unit_path: Path,
    process: dict[str, Any] | None,
) -> dict[str, str]:
    """Scanner-side readiness (P2): the ACTUAL production Scanner, never the
    V2 release directory.

    Subchecks (compound — worst wins): ``scanner_checkout_resolved`` (an
    explicit ``--scanner-root`` checkout exists), ``scanner_unit_available``
    (the installed systemd unit file is readable), ``scanner_unit_matches_
    checkout`` (ExecStart script + Python environment + WorkingDirectory
    resolve to this checkout), ``scanner_unit_env`` (the unit's
    EnvironmentFile is the Scanner env audited here), ``scanner_source_
    routing`` (``audit_source_path`` over THIS checkout — an old Scanner
    fails even when the V2 release passes), ``scanner_process_matches_unit``
    (read-only process metadata agrees with the unit; never restarted),
    ``scanner_sources_effective`` (no audited source changed after the
    process started), plus the unchanged ``scanner_flag`` /
    ``db_consistency`` two-process env checks.  Anything unconfirmable is
    NOT_VERIFIED — a static audit is never passed off as runtime proof.
    """
    subs: dict[str, tuple[str, str, str]] = {}
    root = Path(scanner_root)

    # 1) The explicit production Scanner checkout (NEVER the V2 release dir).
    runner = root / "scanner_runner.py"
    checkout_ok = root.is_dir() and runner.is_file()
    if checkout_ok:
        subs["scanner_checkout_resolved"] = (
            STATUS_PASS, "",
            f"scanner checkout resolved from --scanner-root ({root.name}), "
            "separate from the V2 release directory",
        )
    else:
        subs["scanner_checkout_resolved"] = (
            STATUS_NOT_VERIFIED, "SCANNER_CHECKOUT_MISSING",
            "the explicit production Scanner checkout has no "
            "scanner_runner.py; Scanner readiness cannot be established",
        )

    # 2) The installed systemd unit FILE of the Scanner.
    directives = _read_unit_directives(scanner_unit_path)
    if directives is None:
        subs["scanner_unit_available"] = (
            STATUS_NOT_VERIFIED, "SCANNER_UNIT_UNAVAILABLE",
            f"systemd unit {scanner_unit_path.name} unreadable/absent; the "
            "running Scanner cannot be attributed to this checkout",
        )
    else:
        subs["scanner_unit_available"] = (
            STATUS_PASS, "",
            "Scanner systemd unit parsed (ExecStart/WorkingDirectory/"
            "EnvironmentFile)",
        )

    exec_parts: list[str] = []
    workdir = ""
    unit_env = ""
    if directives is not None:
        exec_start = (directives.get("ExecStart") or [""])[0]
        exec_parts = exec_start.split() if exec_start else []
        workdir = (directives.get("WorkingDirectory") or [""])[0]
        env_files = directives.get("EnvironmentFile") or []
        unit_env = env_files[0] if env_files else ""

    # 3) The unit must run THIS checkout: script path, Python environment
    #    and WorkingDirectory.  scanner_runner.py merely existing proves
    #    nothing when the unit executes a different tree.
    if directives is None or not checkout_ok:
        subs["scanner_unit_matches_checkout"] = (
            STATUS_NOT_VERIFIED, "SCANNER_CHECKOUT_OR_UNIT_UNAVAILABLE",
            "unit or Scanner checkout unavailable — unit<->checkout "
            "consistency not verifiable",
        )
    else:
        script = exec_parts[-1] if exec_parts else ""
        python_exe = exec_parts[0] if exec_parts else ""
        problems: list[str] = []
        if not script.endswith("scanner_runner.py"):
            problems.append("ExecStart has no scanner_runner.py script path")
        elif Path(script).parent.resolve() != root.resolve():
            problems.append("ExecStart script lives outside --scanner-root")
        if not python_exe:
            problems.append("ExecStart python missing")
        else:
            try:
                Path(python_exe).resolve().relative_to(root.resolve())
            except (OSError, ValueError):
                problems.append(
                    "ExecStart python environment is outside --scanner-root",
                )
        if not workdir:
            problems.append("WorkingDirectory missing")
        elif Path(workdir).resolve() != root.resolve():
            problems.append("WorkingDirectory differs from --scanner-root")
        if problems:
            subs["scanner_unit_matches_checkout"] = (
                STATUS_FAIL, "SCANNER_UNIT_CHECKOUT_MISMATCH",
                "; ".join(problems),
            )
        else:
            subs["scanner_unit_matches_checkout"] = (
                STATUS_PASS, "",
                "unit ExecStart/WorkingDirectory/Python environment resolve "
                "to the audited Scanner checkout",
            )

    # 4) The Scanner env audited here must be the unit's EnvironmentFile.
    if directives is None:
        subs["scanner_unit_env"] = (
            STATUS_NOT_VERIFIED, "SCANNER_UNIT_UNAVAILABLE",
            "unit EnvironmentFile directive not verifiable",
        )
    elif not unit_env:
        subs["scanner_unit_env"] = (
            STATUS_NOT_VERIFIED, "SCANNER_UNIT_ENV_MISSING",
            "unit has no EnvironmentFile; the audited Scanner env cannot "
            "be attributed to it",
        )
    elif _path_key(unit_env) != _path_key(str(scanner_env_path)):
        subs["scanner_unit_env"] = (
            STATUS_FAIL, "SCANNER_UNIT_ENV_MISMATCH",
            "unit EnvironmentFile differs from --scanner-env-file; the "
            "audited Scanner env is not the unit's",
        )
    else:
        subs["scanner_unit_env"] = (
            STATUS_PASS, "",
            "unit EnvironmentFile equals the audited --scanner-env-file",
        )

    # 5) V2 source routing on the SCANNER checkout (P2-2): publication,
    #    V1+V2 delivery, fail-open hook, flag independence, pre-filter
    #    route, permission parity, no trading-gate dependence.
    if not checkout_ok:
        subs["scanner_source_routing"] = (
            STATUS_NOT_VERIFIED, "SCANNER_CHECKOUT_UNAVAILABLE",
            "Scanner checkout unavailable — V2 source routing not audited",
        )
    else:
        audit = audit_source_path(root)
        if audit["wired"]:
            subs["scanner_source_routing"] = (
                STATUS_PASS, "",
                "Scanner checkout delivers the pre-gate publication to BOTH "
                "hooks with independent V2 routing (fail-open, V2-flag-only, "
                "no trading-gate dependency)",
            )
        else:
            failing = [
                (name, check) for name, check in audit["checks"].items()
                if check["status"] != STATUS_PASS
            ]
            reason = failing[0][1]["reason"] or "SOURCE_PATH_NOT_WIRED" \
                if failing else "SOURCE_PATH_NOT_WIRED"
            subs["scanner_source_routing"] = (
                STATUS_FAIL, reason,
                f"failed scanner audit check: {failing[0][0]}"
                if failing else "",
            )

    # 6) The RUNNING Scanner must match the unit (read-only metadata —
    #    this tool never restarts anything; mismatch = definite FAIL,
    #    unavailable = NOT_VERIFIED, never a silent PASS).
    if directives is None:
        subs["scanner_process_matches_unit"] = (
            STATUS_NOT_VERIFIED, "SCANNER_UNIT_UNAVAILABLE",
            "running Scanner cannot be checked without the unit file",
        )
    elif not process or not process.get("available"):
        reason = (process or {}).get("reason", "PROCESS_METADATA_UNAVAILABLE")
        subs["scanner_process_matches_unit"] = (
            STATUS_NOT_VERIFIED, "SCANNER_PROCESS_NOT_VERIFIABLE",
            f"running Scanner metadata unavailable ({reason}); a static "
            "audit alone is never runtime evidence",
        )
    elif not process.get("active"):
        subs["scanner_process_matches_unit"] = (
            STATUS_NOT_VERIFIED, "SCANNER_PROCESS_NOT_RUNNING",
            "the Scanner unit is not active — the audited sources are not "
            "provably executing",
        )
    else:
        cmdline = [str(token) for token in process.get("cmdline", [])]
        problems = []
        if not cmdline:
            problems.append("running cmdline unreadable")
        elif not exec_parts:
            problems.append("unit ExecStart missing")
        else:
            if _path_text(cmdline[0]) != _path_text(exec_parts[0]):
                problems.append(
                    "running python differs from unit ExecStart python",
                )
            wanted_script = exec_parts[-1]
            if not any(
                _path_text(token) == _path_text(wanted_script)
                for token in cmdline
            ):
                problems.append(
                    "running script path differs from unit ExecStart "
                    "scanner_runner.py",
                )
        if workdir and _path_text(str(process.get("cwd", ""))) != \
                _path_text(workdir):
            problems.append("running cwd differs from unit WorkingDirectory")
        fragment = str(process.get("fragment", "") or "")
        if fragment and _path_key(fragment) != _path_key(str(scanner_unit_path)):
            problems.append(
                "loaded unit fragment differs from the audited unit file",
            )
        if problems:
            subs["scanner_process_matches_unit"] = (
                STATUS_FAIL, "SCANNER_PROCESS_UNIT_MISMATCH",
                "; ".join(problems),
            )
        else:
            subs["scanner_process_matches_unit"] = (
                STATUS_PASS, "",
                f"running Scanner (pid {process.get('pid')}) "
                "cmdline/cwd/loaded-unit match the unit",
            )

    # 7) Sources-on-disk vs the running process: files updated AFTER the
    #    process start mean it may still hold pre-update code.
    if not checkout_ok:
        subs["scanner_sources_effective"] = (
            STATUS_NOT_VERIFIED, "SCANNER_CHECKOUT_UNAVAILABLE",
            "audited source freshness not checkable without the checkout",
        )
    elif not process or not process.get("available") \
            or not process.get("active") \
            or process.get("started_at") is None:
        subs["scanner_sources_effective"] = (
            STATUS_NOT_VERIFIED, "SCANNER_PROCESS_START_TIME_UNKNOWN",
            "running Scanner start time unavailable — sources-on-disk vs "
            "loaded code cannot be compared",
        )
    else:
        try:
            newest = max(
                (root / rel).stat().st_mtime for rel in SCANNER_AUDIT_FILES
            )
        except OSError:
            subs["scanner_sources_effective"] = (
                STATUS_NOT_VERIFIED, "SCANNER_SOURCE_STAT_FAILED",
                "audited source files cannot be stat'ed",
            )
        else:
            if newest > float(process["started_at"]):
                subs["scanner_sources_effective"] = (
                    STATUS_NOT_VERIFIED,
                    "SCANNER_SOURCES_CHANGED_AFTER_START",
                    "audited sources were modified AFTER the Scanner "
                    "started — the process may still hold pre-update code; "
                    "confirming the loaded version needs the approved "
                    "Scanner restart (separate GO) or extra diagnostics — "
                    "never done here",
                )
            else:
                subs["scanner_sources_effective"] = (
                    STATUS_PASS, "",
                    "no audited source changed after process start: disk "
                    "state is consistent with the loaded code (read-only "
                    "process metadata; exact loaded-bytecode identity would "
                    "require an approved restart — see runbook Step 9)",
                )

    # 8) The Scanner process (second process) has the V2 flag configured
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

    # 9) Two-process DB consistency: both environments must point at the
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


def _v1_isolation_check(
    repo_root: Path, runtime_env_path: Path, scanner_root: Path,
) -> dict[str, str]:
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
        v1_values = _unit_directives(v1_text)
        v2_values = _unit_directives(v2_text)
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

    # 3) The V1 capture hook still exists in the ACTUAL Scanner's
    #    scanner_runner (deploying V2 must not have removed or disabled
    #    V1 delivery in the production checkout that really runs).
    runner = Path(scanner_root) / "scanner_runner.py"
    try:
        runner_text = runner.read_text(encoding="utf-8")
    except OSError:
        runner_text = None
    if runner_text is None:
        subs["v1_surface"] = (
            STATUS_NOT_VERIFIED, "SCANNER_RUNNER_UNREADABLE",
            "Scanner checkout unavailable — V1 hook presence not verifiable",
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
    scanner_root: Path | None = None,
    scanner_unit_path: Path | None = None,
    scanner_process: dict[str, Any] | None | object = _AUTO_PROCESS,
) -> dict[str, Any]:
    """Run ALL SIX checks; never activates, never writes, never prints secrets.

    ``scanner_root`` is the EXPLICIT production Scanner checkout (P2-1:
    defaults to ``/opt/trad_bot`` — the V2 release ``repo_root`` is never
    reused as the Scanner source); ``scanner_unit_path`` is the installed
    systemd unit it must agree with; ``scanner_process`` is the running
    process metadata (default: read-only detection via
    ``_detect_scanner_process`` — tests inject fixtures and never touch a
    real systemd or /proc).
    """
    repo_root = Path(repo_root) if repo_root is not None else _ROOT
    if scanner_env_path is None:
        scanner_env_path = DEFAULT_SCANNER_ENV
    if scanner_root is None:
        scanner_root = DEFAULT_SCANNER_ROOT
    else:
        scanner_root = Path(scanner_root)
    if scanner_unit_path is None:
        scanner_unit_path = DEFAULT_SCANNER_UNIT
    else:
        scanner_unit_path = Path(scanner_unit_path)
    if scanner_process is _AUTO_PROCESS:
        scanner_process = _detect_scanner_process(scanner_unit_path)
    if connect is None:
        # dbapi flavour: exposes cursor()/autocommit/rollback for the
        # read-only transaction below (the native flavour does not).
        import pg8000.dbapi
        connect = pg8000.dbapi.connect

    checks: dict[str, dict[str, str]] = {}

    checks["V2_RUNTIME_CONFIG_READY"], runtime_values = _runtime_config_check(
        path, repo_root,
    )
    checks["PROTOCOL_FINGERPRINT_VALID"] = _protocol_check()
    checks["V2_SOURCE_CAPTURE_READY"] = _scanner_source_check(
        scanner_root, runtime_values, scanner_env_path,
        scanner_unit_path=scanner_unit_path,
        process=scanner_process if isinstance(scanner_process, dict) else None,
    )
    checks["DB_READY"], checks["V2_ACTIVATION_READY"] = _evaluate_db(
        runtime_values, connect,
    )
    checks["V1_ISOLATION_VALID"] = _v1_isolation_check(repo_root, path, scanner_root)

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
    parser.add_argument(
        "--scanner-root", type=Path, default=DEFAULT_SCANNER_ROOT,
        help="production Scanner checkout to audit (the systemd ExecStart "
             "tree; never the V2 release directory) "
             f"(default: {DEFAULT_SCANNER_ROOT.as_posix()})",
    )
    parser.add_argument(
        "--scanner-unit", type=Path, default=DEFAULT_SCANNER_UNIT,
        help="installed Scanner systemd unit file "
             "(ExecStart/WorkingDirectory/EnvironmentFile) "
             f"(default: {DEFAULT_SCANNER_UNIT.as_posix()})",
    )
    args = parser.parse_args(argv)
    try:
        result = run_preflight(
            args.env_file,
            scanner_env_path=args.scanner_env_file,
            scanner_root=args.scanner_root,
            scanner_unit_path=args.scanner_unit,
        )
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
