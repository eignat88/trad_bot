"""Standalone fail-closed runner for the isolated Issue #152 research contour.

``--preflight`` validates only the explicit isolated environment. ``--once`` is
available only with both explicit opt-ins and keeps every protocol/runtime
guard enabled. Production capture, deployment and OOS activation remain
separately unapproved.
"""
from __future__ import annotations

import argparse
import os
import signal
import threading
from datetime import datetime, timezone

from .pipeline import MeEarlyMaeV3Pipeline


ISOLATED_DATABASE_NAME = "trad_bot_runner_152_it"
ISOLATED_DATABASE_USER = "trad_bot_runner_152_it"
ISOLATED_OS_USER = "trad_bot_runner_152_it"
ISOLATED_DATABASE_SOCKET = "/var/run/postgresql"
ISOLATED_DB_API_SOCKET = ISOLATED_DATABASE_SOCKET + "/.s.PGSQL.5432"
ISOLATED_TEST_ONLY_ENV = "ME_EARLY_MAE_V3_ISOLATED_TEST_ONLY"
ISOLATED_ONCE_ENV = "ME_EARLY_MAE_V3_ISOLATED_ONCE"
ISOLATED_OPT_IN_VALUE = "true"

REQUIRED_V3_TABLES = frozenset({
    "protocol",
    "variant",
    "source_event",
    "variant_outcome",
    "pipeline_run",
})

REQUIRED_V3_CONSTRAINTS = frozenset({
    "protocol_pkey",
    "protocol_check",
    "protocol_direction_check",
    "protocol_experiment_id_check",
    "protocol_scanner_name_check",
    "variant_pkey",
    "variant_check",
    "variant_role_check",
    "variant_threshold_mae_r_check",
    "variant_horizon_minutes_check",
    "chk_me_mae_v3_exact_variant_definitions",
    "source_event_pkey",
    "source_event_protocol_version_fkey",
    "source_event_protocol_version_source_setup_id_key",
    "source_event_protocol_version_instrument_id_scanner_name_di_key",
    "source_event_source_status_check",
    "source_event_coverage_status_check",
    "source_event_check",
    "source_event_check1",
    "source_event_check2",
    "source_event_check3",
    "source_event_check4",
    "source_event_check5",
    "chk_me_mae_v3_finalized_baseline_unambiguous",
    "variant_outcome_pkey",
    "variant_outcome_source_event_id_fkey",
    "variant_outcome_variant_status_check",
    "variant_outcome_trigger_result_check",
    "variant_outcome_coverage_status_check",
    "variant_outcome_role_check",
    "variant_outcome_threshold_mae_r_check",
    "variant_outcome_horizon_minutes_check",
    "variant_outcome_check",
    "variant_outcome_check1",
    "variant_outcome_check2",
    "chk_me_mae_v3_ambiguous_status_consistency",
    "chk_me_mae_v3_finalized_not_ambiguous",
})

REQUIRED_V3_TRIGGERS = frozenset({
    "trg_me_mae_v3_source_immutable_update",
    "trg_me_mae_v3_source_immutable_delete",
    "trg_me_mae_v3_source_finalized",
    "trg_me_mae_v3_source_lock_variant_tuple",
    "trg_me_mae_v3_source_updated_at",
    "trg_me_mae_v3_source_block_direct_finalized_insert",
    "trg_me_mae_v3_source_commit_check",
    "trg_me_mae_v3_variant_immutable_update",
    "trg_me_mae_v3_variant_immutable_delete",
    "trg_me_mae_v3_variant_finalized_batch",
    "trg_me_mae_v3_variant_updated_at",
    "trg_me_mae_v3_variant_commit_check",
    "trg_me_mae_v3_variant_definition_immutable_update",
    "trg_me_mae_v3_variant_definition_immutable_delete",
    "trg_me_mae_v3_block_direct_ambiguous_insert",
    "trg_me_mae_v3_ambiguous_source_update",
    "trg_me_mae_v3_ambiguous_variant_insert",
    "trg_me_mae_v3_ambiguous_variant_update",
    "trg_me_mae_v3_ambiguous_commit_check",
})


def require_isolated_database_name(database_name: str) -> str:
    """Accept only the dedicated Issue #152 integration database."""
    if (
        not isinstance(database_name, str)
        or database_name != ISOLATED_DATABASE_NAME
    ):
        raise RuntimeError(
            "ME_R_EARLY_MAE_V3_ISOLATED_DATABASE_REQUIRED"
        )
    return database_name


def _is_explicit_opt_in(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == ISOLATED_OPT_IN_VALUE


def require_isolated_test_only_opt_in() -> None:
    if not _is_explicit_opt_in(ISOLATED_TEST_ONLY_ENV):
        raise RuntimeError("ME_R_EARLY_MAE_V3_ISOLATED_OPT_IN_REQUIRED")


def require_isolated_once_opt_in() -> None:
    if not _is_explicit_opt_in(ISOLATED_ONCE_ENV):
        raise RuntimeError("ME_R_EARLY_MAE_V3_ISOLATED_ONCE_OPT_IN_REQUIRED")


def connect_isolated_database():
    """Open and verify the dedicated local integration database."""
    import pg8000.dbapi

    database = require_isolated_database_name(ISOLATED_DATABASE_NAME)

    connection = pg8000.dbapi.connect(
        user=ISOLATED_DATABASE_USER,
        database=database,
        unix_sock=ISOLATED_DB_API_SOCKET,
    )

    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT current_database(), current_user, "
            "session_user, inet_client_addr() IS NULL"
        )
        row = cursor.fetchone()
        expected = (
            ISOLATED_DATABASE_NAME,
            ISOLATED_DATABASE_USER,
            ISOLATED_DATABASE_USER,
            True,
        )
        if tuple(row or ()) != expected:
            raise RuntimeError(
                "ME_R_EARLY_MAE_V3_DATABASE_IDENTITY_MISMATCH"
            )

        connection.rollback()
        return connection

    except BaseException:
        connection.close()
        raise


def require_live_connection(connection) -> None:
    """Fail closed when the repository is handed a dead connection."""
    if connection is None or getattr(connection, "closed", False):
        raise RuntimeError("ME_R_EARLY_MAE_V3_DB_CONNECTION_CLOSED")
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT 1")
        if tuple(cursor.fetchone() or ()) != (1,):
            raise RuntimeError("ME_R_EARLY_MAE_V3_DB_CONNECTION_UNHEALTHY")
    finally:
        connection.rollback()


def require_isolated_schema(connection) -> None:
    """Reject incomplete constraints/triggers, not merely missing tables."""
    cursor = connection.cursor()
    cursor.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'research_me_early_mae_v3' "
        "AND table_type = 'BASE TABLE'"
    )
    found = {row[0] for row in cursor.fetchall()}
    missing = REQUIRED_V3_TABLES - found
    if missing:
        raise RuntimeError(
            "ME_R_EARLY_MAE_V3_SCHEMA_INCOMPLETE:"
            + ",".join(sorted(missing))
        )

    cursor.execute(
        "SELECT conname FROM pg_constraint "
        "WHERE connamespace = 'research_me_early_mae_v3'::regnamespace"
    )
    constraints = {row[0] for row in cursor.fetchall()}
    missing_constraints = REQUIRED_V3_CONSTRAINTS - constraints
    if missing_constraints:
        raise RuntimeError(
            "ME_R_EARLY_MAE_V3_CONSTRAINTS_INCOMPLETE:"
            + ",".join(sorted(missing_constraints))
        )

    cursor.execute(
        "SELECT tgname FROM pg_trigger "
        "WHERE tgrelid = 'research_me_early_mae_v3.source_event'::regclass "
        "AND NOT tgisinternal "
        "UNION ALL "
        "SELECT tgname FROM pg_trigger "
        "WHERE tgrelid = 'research_me_early_mae_v3.variant_outcome'::regclass "
        "AND NOT tgisinternal "
        "UNION ALL "
        "SELECT tgname FROM pg_trigger "
        "WHERE tgrelid = 'research_me_early_mae_v3.variant'::regclass "
        "AND NOT tgisinternal"
    )
    triggers = {row[0] for row in cursor.fetchall()}
    missing_triggers = REQUIRED_V3_TRIGGERS - triggers
    if missing_triggers:
        raise RuntimeError(
            "ME_R_EARLY_MAE_V3_TRIGGERS_INCOMPLETE:"
            + ",".join(sorted(missing_triggers))
        )
    connection.rollback()


def validate_isolated_fingerprint_config() -> None:
    """Verify approved configuration without authorizing activation."""
    import hmac

    from .protocol import (
        ACTIVATION_AT,
        APPROVED_FINGERPRINT,
        FROZEN_AT,
        PROTOCOL_VERSION,
        calculate_protocol_fingerprint,
        protocol_payload,
    )

    if PROTOCOL_VERSION != "3.0.0":
        raise RuntimeError("ME_R_EARLY_MAE_V3_PROTOCOL_VERSION_MISMATCH")
    if FROZEN_AT is None or ACTIVATION_AT is None:
        raise RuntimeError("ME_R_EARLY_MAE_V3_BOUNDARIES_MISSING")

    if FROZEN_AT >= ACTIVATION_AT:
        raise RuntimeError("ME_R_EARLY_MAE_V3_BOUNDARIES_INVALID")

    if APPROVED_FINGERPRINT is None:
        raise RuntimeError("ME_R_EARLY_MAE_V3_FINGERPRINT_NOT_APPROVED")

    actual = calculate_protocol_fingerprint(
        protocol_payload(FROZEN_AT, ACTIVATION_AT)
    )

    if not hmac.compare_digest(APPROVED_FINGERPRINT, actual):
        raise RuntimeError("ME_R_EARLY_MAE_V3_FINGERPRINT_MISMATCH")


def require_isolated_os_user() -> None:
    """Require the dedicated unprivileged Linux service account."""
    import pwd

    try:
        expected_uid = pwd.getpwnam(ISOLATED_OS_USER).pw_uid
    except KeyError as exc:
        raise RuntimeError(
            "ME_R_EARLY_MAE_V3_ISOLATED_OS_USER_MISSING"
        ) from exc
    if os.getuid() != expected_uid or os.geteuid() != expected_uid:
        raise RuntimeError("ME_R_EARLY_MAE_V3_ISOLATED_OS_USER_REQUIRED")


def isolated_preflight() -> None:
    """Validate the isolated environment without creating any rows."""
    from .protocol import ACTIVATION_AT, APPROVED_FINGERPRINT, FROZEN_AT

    require_isolated_test_only_opt_in()
    if FROZEN_AT is None or ACTIVATION_AT is None:
        raise RuntimeError("ME_R_EARLY_MAE_V3_BOUNDARIES_MISSING")

    if APPROVED_FINGERPRINT is None:
        raise RuntimeError("ME_R_EARLY_MAE_V3_FINGERPRINT_NOT_APPROVED")

    validate_isolated_fingerprint_config()
    require_isolated_os_user()
    connection = connect_isolated_database()
    try:
        require_live_connection(connection)
        require_isolated_schema(connection)
    finally:
        connection.close()


def _require_strict_activation(now: datetime) -> None:
    """Runtime starts only after the approved prospective boundary."""
    from .protocol import ACTIVATION_AT, parse_utc

    if now <= parse_utc(ACTIVATION_AT):
        raise RuntimeError("ME_R_EARLY_MAE_V3_ACTIVATION_NOT_EFFECTIVE")


def _install_shutdown_handler() -> threading.Event:
    stop_event = threading.Event()

    def request_shutdown(signum, frame):  # pragma: no cover - signal path
        del signum, frame
        stop_event.set()

    previous = {
        signal.SIGTERM: signal.signal(signal.SIGTERM, request_shutdown),
        signal.SIGINT: signal.signal(signal.SIGINT, request_shutdown),
    }
    stop_event.restore = lambda: [
        signal.signal(number, handler)
        for number, handler in previous.items()
    ]
    return stop_event


def run_isolated_once() -> dict[str, int]:
    """One guarded isolated cycle; production routing remains impossible."""
    require_isolated_test_only_opt_in()
    require_isolated_once_opt_in()
    isolated_preflight()
    require_runtime_configuration_now()

    connection = connect_isolated_database()
    try:
        require_live_connection(connection)
        require_isolated_schema(connection)
        require_live_connection(connection)

        stop_event = _install_shutdown_handler()
        try:
            pipeline = MeEarlyMaeV3Pipeline(connection)
            pipeline.initialize()
            require_live_connection(connection)
            stats = pipeline.evaluate_cycle()
        finally:
            stop_event.restore()

        if not isinstance(stats, dict):
            raise RuntimeError("ME_R_EARLY_MAE_V3_INVALID_EVALUATOR_STATS")

        if stats.get("errors", 0) != 0:
            raise RuntimeError("ME_R_EARLY_MAE_V3_EVALUATOR_ERRORS")

        connection.commit()
        return stats

    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def require_runtime_configuration_now() -> None:
    """Keep the existing runtime guard mandatory for ``--once``."""
    from .guards import require_runtime_configuration

    require_runtime_configuration(datetime.now(timezone.utc))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()

    if args.preflight and args.once:
        raise SystemExit(
            "ME_R_EARLY_MAE_V3_CAPTURE_DISABLED: invalid CLI combination"
        )

    if args.preflight:
        try:
            isolated_preflight()
        except BaseException as exc:
            raise SystemExit(
                f"ME_R_EARLY_MAE_V3_PREFLIGHT_FAILED: {exc}"
            ) from exc
        print("ME_R_EARLY_MAE_V3_ISOLATED_PREFLIGHT_PASS")
        return

    if args.once:
        try:
            stats = run_isolated_once()
        except BaseException as exc:
            raise SystemExit(
                f"ME_R_EARLY_MAE_V3_CAPTURE_DISABLED: {exc}"
            ) from exc
        print(
            "ME_R_EARLY_MAE_V3_ISOLATED_ONCE_PASS "
            + " ".join(f"{key}={value}" for key, value in sorted(stats.items()))
        )
        return

    raise SystemExit(
        "ME_R_EARLY_MAE_V3_CAPTURE_DISABLED: deployment, migration, freeze "
        "and activation remain separately unapproved"
    )


if __name__ == "__main__":
    main()
