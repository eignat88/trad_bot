#!/usr/bin/env python3
"""ME_LONG_V2_INVERSE_SHORT_V3 — deployment preflight (Issue #285).

Read-only deployment gate for the V3 entry-guards-and-capacity contour
(``ME_INVERSE_SHORT_V3_ENTRY_GUARDS_AND_CAPACITY_V1``).  It verifies the
deployment state and REFUSES (non-zero exit) anything that would violate the
frozen contract before the runtime may start:

STATIC (no database needed):

  MIGRATION_090_PRESENT_AND_UNIQUE   migration 090 exists and is unique
  GUARD_CONFIG_STRICT                strict guard config == frozen 12/True/12
  FINGERPRINT_PIN                    current protocol fingerprint is pinned
  PROTOCOL_PARITY_V2                 geometry/TTL/fees identical to V2
  SOURCE_CAPTURE_READY               scanner_runner delivers to the V3 hook
  RUNTIME_ARTIFACTS                  systemd unit + env example + runbook
  ISOLATION_VALID                    unit/env carry no V1/V2/OOS-2 identity

DATABASE (with --env-file / --db-*; otherwise reported SKIP):

  DB_MIGRATION_090_OBJECTS           the 4 V3 tables + guard indexes + trigger
  DB_V3_ACTIVATION_STATE             activation row fails closed (or matches)
  DB_V2_PRECONDITION                 V2 admits no entries (DRAINING/CANCELLED)
  DB_CROSS_CONTOUR_CAPACITY          OPEN V2 + OPEN V3 <= 12
  DB_V2_LEGACY_DUPLICATES            reported (informational, READ-ONLY)

Exit code: 0 = all checks PASS/WARN/SKIP, 1 = any FAIL (or refused input).
The tool never writes to the database or to any service state.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PINNED_FINGERPRINT = (
    "c10eb00f2777c2ca9b42d465ccbad4dc7908d86676b678fddcff5722f6f80aa4"
)

MIGRATION_090_NAME = "090_me_long_v2_inverse_short_v3.sql"
RUNBOOK_PATH = "docs/me_inverse_short_v3_production_runtime_v1.md"
UNIT_PATH = "deploy/systemd/trad-bot-me-inverse-short-v3-runtime.service"
ENV_EXAMPLE_PATH = "deploy/env/me-inverse-short-v3-runtime.env.example"
PREFLIGHT_SELF_PATH = "scripts/me_inverse_short_v3_preflight.py"
ACTIVATE_PATH = "scripts/me_inverse_short_v3_activate.py"

V3_TABLES = (
    "dds.me_long_v2_inverse_short_v3_activation",
    "dds.me_long_v2_inverse_short_v3_event",
    "dds.me_long_v2_inverse_short_v3_position",
    "dds.me_long_v2_inverse_short_v3_close_outbox",
)
V2_ACTIVATION_TABLE = "dds.me_long_v2_inverse_short_v2_activation"
V2_POSITION_TABLE = "dds.me_long_v2_inverse_short_v2_position"

V2_EXPERIMENT_ID = "ME_LONG_V2_INVERSE_SHORT_V2"
V3_EXPERIMENT_ID = "ME_LONG_V2_INVERSE_SHORT_V3"

FORBIDDEN_ENV_KEYS = (
    "ME_INVERSE_SHORT_V1_ENABLED",
    "ME_INVERSE_SHORT_V2_ENABLED",
    "ME_INVERSE_SHORT_OOS2_ENABLED",
    "ME_INVERSE_SHORT_OOS2_GUARD",
)

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


class PreflightRefused(RuntimeError):
    """Operator-visible refusal (fail-closed, read-only)."""


def read_service_env(
    path: str | Path,
    *,
    require_disabled: bool = False,
) -> dict[str, str]:
    """Strictly parse a KEY=VALUE runtime env file (read-only).

    Unknown V1/V2/OOS-2 contour keys are refused (isolation), ``DB_*``
    credentials must be present, and ``require_disabled=True`` additionally
    refuses when ME_INVERSE_SHORT_V3_ENABLED=1 (used before the activation
    state change: the FIRST factor must stay OFF until the row is fixed).
    """
    env_path = Path(path)
    if not env_path.exists():
        raise PreflightRefused(f"env file not found: {env_path}")
    values: dict[str, str] = {}
    for lineno, raw in enumerate(
        env_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PreflightRefused(
                f"{env_path}:{lineno}: not a KEY=VALUE line: {raw!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        if key in values:
            raise PreflightRefused(f"{env_path}:{lineno}: duplicate key {key!r}")
        values[key] = value.strip()
    for forbidden in FORBIDDEN_ENV_KEYS:
        if forbidden in values:
            raise PreflightRefused(
                f"{env_path}: foreign contour key {forbidden!r} present "
                f"(V3 isolation)"
            )
    for required in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        if required not in values:
            raise PreflightRefused(f"{env_path}: missing {required}")
    if require_disabled and values.get("ME_INVERSE_SHORT_V3_ENABLED") == "1":
        raise PreflightRefused(
            f"{env_path}: ME_INVERSE_SHORT_V3_ENABLED=1 while the activation "
            f"row is not fixed yet — keep the FIRST factor OFF until the "
            f"approved activation step"
        )
    return values


def _check_migrations(root: Path) -> tuple[str, str]:
    migrations = sorted((root / "sql" / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
    names = [p.name for p in migrations]
    if MIGRATION_090_NAME not in names:
        return FAIL, f"migration 090 missing: {MIGRATION_090_NAME}"
    numbers = [int(n.split("_")[0]) for n in names]
    if numbers.count(90) != 1:
        return FAIL, "migration number 090 must be unique"
    # Append-only migration numbering: later migrations from other work
    # packages (091+) are legitimate and never invalidate V3's own frozen
    # migration — only a missing or duplicated 090 does.
    return PASS, f"{MIGRATION_090_NAME} present and unique"


def _check_guard_config() -> tuple[str, str]:
    from app.paper.me_long_v2_inverse_short_v3.config import (
        V3ConfigError,
        load_v3_guard_config,
    )

    try:
        config = load_v3_guard_config()
    except V3ConfigError as exc:
        return FAIL, f"guard config refused: {exc}"
    return PASS, f"guards frozen at {config.as_dict()}"


def _check_fingerprint() -> tuple[str, str]:
    from app.paper.me_long_v2_inverse_short_v3.protocol import (
        protocol_fingerprint,
    )

    current = protocol_fingerprint()
    if current != PINNED_FINGERPRINT:
        return FAIL, (
            f"protocol fingerprint drift: {current} != pinned "
            f"{PINNED_FINGERPRINT}"
        )
    return PASS, f"protocol fingerprint pinned at {current[:16]}…"


def _check_protocol_parity() -> tuple[str, str]:
    from app.paper.me_long_v2_inverse_short_v2 import protocol as v2p
    from app.paper.me_long_v2_inverse_short_v3 import protocol as v3p

    for name in (
        "TAKE_PROFIT_PCT", "STOP_LOSS_PCT", "MAX_HOLD_MINUTES",
        "ENTRY_MAX_AGE_MINUTES", "FEE_PER_SIDE", "SLIPPAGE_PER_SIDE",
        "INTRABAR_POLICY", "EXIT_PRIORITY", "SOURCE_SCANNER",
    ):
        if getattr(v3p, name) != getattr(v2p, name):
            return FAIL, f"frozen value drifted from V2: {name}"
    for price in (1.0, 100.0, 250_000.0):
        if v3p.short_geometry(price) != v2p.short_geometry(price):
            return FAIL, f"SHORT geometry drifted from V2 at price {price}"
    return PASS, "geometry / TTL / fees / slippage / exits identical to V2"


def _check_source_capture(root: Path) -> tuple[str, str]:
    from app.paper.me_long_v2_inverse_short_v3.source_path import (
        audit_source_path,
    )

    audit = audit_source_path(root)
    if not audit.get("wired"):
        return FAIL, f"V3 source capture not wired: {audit.get('checks')}"
    return PASS, "scanner_runner delivers source events to the V3 hook"


def _check_runtime_artifacts(root: Path) -> tuple[str, str]:
    missing = [
        rel for rel in (RUNBOOK_PATH, UNIT_PATH, ENV_EXAMPLE_PATH,
                        PREFLIGHT_SELF_PATH, ACTIVATE_PATH)
        if not (root / rel).exists()
    ]
    if missing:
        return FAIL, f"missing deployment artifacts: {missing}"
    return PASS, "systemd unit + env example + runbook + tools present"


def _check_isolation(root: Path) -> tuple[str, str]:
    unit = (root / UNIT_PATH).read_text(encoding="utf-8")
    for token in ("me_inverse_short_v2_release", "me-inverse-short-v2-runtime.env",
                  "me_long_v2_inverse_short_v2.production_runtime"):
        if token in unit:
            return FAIL, f"systemd unit carries V2 identity: {token!r}"
    # The activation tool writes production state: it must never import the
    # V2 contour module (the preflight itself reads the V2 protocol module
    # READ-ONLY for the parity check, which is allowed by design).
    activate = (root / "scripts" / "me_inverse_short_v3_activate.py").read_text(
        encoding="utf-8")
    v2_import = "me_long_v2_inverse_short_v2."  # V2 package import path
    if v2_import in activate:
        return FAIL, "me_inverse_short_v3_activate.py imports the V2 contour"
    return PASS, "unit + tools carry V3-only identity"


# ---------------------------------------------------------------------------
# Database checks (read-only)
# ---------------------------------------------------------------------------
def _connect(db_env: dict[str, str]) -> Any:
    import pg8000

    return pg8000.connect(
        host=db_env["DB_HOST"],
        port=int(db_env["DB_PORT"]),
        database=db_env["DB_NAME"],
        user=db_env["DB_USER"],
        password=db_env["DB_PASSWORD"],
        timeout=10,
    )


def _check_db_objects(conn: Any) -> tuple[str, str]:
    cursor = conn.cursor()
    try:
        missing = []
        for table in V3_TABLES:
            cursor.execute("SELECT to_regclass(%s)", (table,))
            if not cursor.fetchone()[0]:
                missing.append(table)
        if missing:
            return FAIL, f"migration 090 objects missing: {missing}"
        cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'dds' "
            "AND indexname = 'uq_me_inverse_short_v3_position_symbol_open'"
        )
        if not cursor.fetchone():
            return FAIL, "single-open-symbol UNIQUE index missing"
        cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'dds' "
            "AND indexname = 'idx_me_inverse_short_v3_position_symbol_entered'"
        )
        if not cursor.fetchone():
            return FAIL, "cooldown (symbol, entered_at DESC) index missing"
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
            "AND tgname = 'trg_me_long_v2_inverse_short_v3_guard'"
        )
        if not cursor.fetchone():
            return FAIL, "entry-facts immutability trigger missing"
    finally:
        try:
            cursor.close()
        except Exception:
            pass
    return PASS, "migration 090 tables + guard indexes + trigger present"


def _check_db_v3_activation(conn: Any, expected_fingerprint: str) -> tuple[str, str]:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT status, protocol_fingerprint, freeze_ts "
            "FROM dds.me_long_v2_inverse_short_v3_activation "
            "WHERE experiment_id = %s",
            (V3_EXPERIMENT_ID,),
        )
        row = cursor.fetchone()
    finally:
        try:
            cursor.close()
        except Exception:
            pass
    if row is None:
        return FAIL, "V3 activation row missing (migration 090 not applied?)"
    status, fingerprint, freeze_ts = row
    if status == "READY_TO_START":
        if fingerprint is None and freeze_ts is None:
            return PASS, "V3 activation READY_TO_START (fail closed, inert)"
        return FAIL, "READY_TO_START must not carry a fingerprint/freeze"
    if status in ("RUNNING", "DRAINING"):
        if fingerprint != expected_fingerprint:
            return FAIL, (
                f"stored fingerprint {str(fingerprint)[:16]}… != current "
                f"code {expected_fingerprint[:16]}… (PROTOCOL_FINGERPRINT_MISMATCH)"
            )
        return PASS, f"V3 activation {status} with the pinned fingerprint"
    return FAIL, f"unknown V3 activation status: {status!r}"


def _check_db_v2_precondition(conn: Any) -> tuple[str, str]:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT status FROM dds.me_long_v2_inverse_short_v2_activation "
            "WHERE experiment_id = %s",
            (V2_EXPERIMENT_ID,),
        )
        row = cursor.fetchone()
    finally:
        try:
            cursor.close()
        except Exception:
            pass
    if row is None:
        return WARN, ("V2 activation row unreadable: V3 entries will FAIL "
                      "CLOSED until the V2 state is readable")
    status = row[0]
    if status in ("DRAINING", "CANCELLED"):
        return PASS, f"V2 activation {status}: V3 may admit entries"
    return WARN, (
        f"V2 activation is {status} — V3 entry admission FAILS CLOSED by "
        f"design until V2 is drained (exits are unaffected)"
    )


def _check_db_capacity(conn: Any) -> tuple[str, str]:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM dds.me_long_v2_inverse_short_v3_position "
            " WHERE status = 'OPEN'), "
            "(SELECT COUNT(*) FROM dds.me_long_v2_inverse_short_v2_position "
            " WHERE status = 'OPEN')"
        )
        v3_open, v2_open = cursor.fetchone()
    finally:
        try:
            cursor.close()
        except Exception:
            pass
    total = int(v3_open) + int(v2_open)
    if total > 12:
        return FAIL, f"OPEN V2+V3 = {total} exceeds the frozen cap of 12"
    return PASS, f"OPEN V2+V3 = {total} (cap 12)"


def _check_db_v2_duplicates(conn: Any) -> tuple[str, str]:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT symbol, COUNT(*) FROM dds.me_long_v2_inverse_short_v2_position "
            "WHERE status = 'OPEN' GROUP BY symbol HAVING COUNT(*) > 1"
        )
        rows = cursor.fetchall()
    finally:
        try:
            cursor.close()
        except Exception:
            pass
    if not rows:
        return PASS, "no legacy V2 duplicate OPEN rows"
    detail = ", ".join(f"{symbol}×{count}" for symbol, count in rows)
    return WARN, (
        f"legacy V2 duplicate OPEN rows observed ({detail}) — reported "
        f"READ-ONLY, allowed to run out under the frozen drain contract; "
        f"the V3 partial UNIQUE index prevents any new one"
    )


def run_preflight(
    root: Path = _ROOT,
    *,
    db_env: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def _record(name: str, outcome: tuple[str, str]) -> None:
        status, detail = outcome
        checks.append({"check": name, "status": status, "detail": detail})

    _record("MIGRATION_090_PRESENT_AND_UNIQUE", _check_migrations(root))
    _record("GUARD_CONFIG_STRICT", _check_guard_config())
    _record("FINGERPRINT_PIN", _check_fingerprint())
    _record("PROTOCOL_PARITY_V2", _check_protocol_parity())
    _record("SOURCE_CAPTURE_READY", _check_source_capture(root))
    _record("RUNTIME_ARTIFACTS", _check_runtime_artifacts(root))
    _record("ISOLATION_VALID", _check_isolation(root))

    if db_env is None:
        for name in ("DB_MIGRATION_090_OBJECTS", "DB_V3_ACTIVATION_STATE",
                     "DB_V2_PRECONDITION", "DB_CROSS_CONTOUR_CAPACITY",
                     "DB_V2_LEGACY_DUPLICATES"):
            checks.append({"check": name, "status": SKIP,
                           "detail": "no database connection requested"})
    else:
        from app.paper.me_long_v2_inverse_short_v3.protocol import (
            protocol_fingerprint,
        )

        try:
            conn = _connect(db_env)
        except Exception as exc:  # noqa: BLE001
            for name in ("DB_MIGRATION_090_OBJECTS", "DB_V3_ACTIVATION_STATE",
                         "DB_V2_PRECONDITION", "DB_CROSS_CONTOUR_CAPACITY",
                         "DB_V2_LEGACY_DUPLICATES"):
                checks.append({
                    "check": name, "status": FAIL,
                    "detail": f"PostgreSQL unreachable ({type(exc).__name__})",
                })
            return checks
        try:
            _record("DB_MIGRATION_090_OBJECTS", _check_db_objects(conn))
            _record(
                "DB_V3_ACTIVATION_STATE",
                _check_db_v3_activation(conn, protocol_fingerprint()),
            )
            _record("DB_V2_PRECONDITION", _check_db_v2_precondition(conn))
            _record("DB_CROSS_CONTOUR_CAPACITY", _check_db_capacity(conn))
            _record("DB_V2_LEGACY_DUPLICATES", _check_db_v2_duplicates(conn))
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ME_LONG_V2_INVERSE_SHORT_V3 deployment preflight (read-only)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--env-file", type=Path, default=None,
                        help="runtime env file; enables the database checks")
    parser.add_argument("--skip-db", action="store_true",
                        help="run only the static checks even with --env-file")
    args = parser.parse_args(argv)

    db_env: dict[str, str] | None = None
    if args.env_file is not None and not args.skip_db:
        try:
            db_env = read_service_env(args.env_file)
        except PreflightRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1

    try:
        checks = run_preflight(_ROOT, db_env=db_env)
    except Exception as exc:  # noqa: BLE001 - preflight never crashes silently
        print(f"REFUSED: preflight failed ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 1

    failed = [c for c in checks if c["status"] == FAIL]
    if args.json:
        print(json.dumps(
            {
                "preflight": "ME_INVERSE_SHORT_V3_ENTRY_GUARDS_AND_CAPACITY_V1",
                "checks": checks,
                "ok": not failed,
            },
            indent=2,
        ))
    else:
        for check in checks:
            print(f"{check['status']:>4}  {check['check']}: {check['detail']}")
        print(f"\nresult: {'OK' if not failed else 'REFUSED'} "
              f"({len(checks)} checks, {len(failed)} failed)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
