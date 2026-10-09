"""Shared fail-closed safety guards for destructive SRR PostgreSQL tests."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
_BLOCKED_DATABASES = frozenset(
    {
        "postgres",
        "template0",
        "template1",
        "trad_bot",
    }
)
_DATABASE_PATTERN = re.compile(r"\Atrad_bot_srr_(persistence|activation)_it_[a-z0-9_]+\Z")


class SrrPostgresTestSafetyError(RuntimeError):
    """Raised before any destructive PostgreSQL test operation."""


@dataclass(frozen=True)
class SrrPostgresTestConfig:
    host: str
    port: int
    database: str
    user: str
    activation_database: str

    @property
    def persistence_database(self) -> str:
        return self.database


def _required(name: str, env: Mapping[str, str]) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise SrrPostgresTestSafetyError(
            f"STOP: {name} is required; destructive PostgreSQL tests have no fallback"
        )
    return value


def load_srr_postgres_test_config(
    env: Mapping[str, str] | None = None,
) -> SrrPostgresTestConfig:
    """Load and validate explicit test-only PostgreSQL configuration."""
    values = os.environ if env is None else env

    host = _required("TEST_DB_HOST", values)
    if host not in _ALLOWED_HOSTS:
        raise SrrPostgresTestSafetyError(
            f"STOP: TEST_DB_HOST must be one of {sorted(_ALLOWED_HOSTS)}; got {host!r}"
        )

    raw_port = _required("TEST_DB_PORT", values)
    if not raw_port.isdigit():
        raise SrrPostgresTestSafetyError(
            f"STOP: TEST_DB_PORT must be numeric; got {raw_port!r}"
        )
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise SrrPostgresTestSafetyError(
            f"STOP: TEST_DB_PORT is outside 1..65535; got {port}"
        )
    if port == 5432:
        raise SrrPostgresTestSafetyError(
            "STOP: TEST_DB_PORT=5432 is reserved for the pre-existing local service"
        )

    database = _required("SRR_TEST_PERSISTENCE_DB", values)
    activation_database = _required("SRR_TEST_ACTIVATION_DB", values)
    for name, value in (
        ("SRR_TEST_PERSISTENCE_DB", database),
        ("SRR_TEST_ACTIVATION_DB", activation_database),
    ):
        if not _DATABASE_PATTERN.fullmatch(value):
            raise SrrPostgresTestSafetyError(
                f"STOP: {name} must match trad_bot_srr_<lowercase_test_name>; got {value!r}"
            )
        if value in _BLOCKED_DATABASES:
            raise SrrPostgresTestSafetyError(
                f"STOP: {name} is a protected database: {value!r}"
            )
        if value.endswith("_20261008"):
            raise SrrPostgresTestSafetyError(
                f"STOP: legacy disposable database is protected from destructive tests: {value!r}"
            )
        if value.endswith("_20261009_v2"):
            raise SrrPostgresTestSafetyError(
                f"STOP: operator-provided database is protected from destructive tests: {value!r}"
            )

    user = values.get("TEST_DB_USER", "postgres").strip() or "postgres"
    if user != "postgres":
        raise SrrPostgresTestSafetyError(
            f"STOP: TEST_DB_USER must remain the isolated test administrator; got {user!r}"
        )

    return SrrPostgresTestConfig(
        host=host,
        port=port,
        database=database,
        user=user,
        activation_database=activation_database,
    )


def validate_server_identity(
    config: SrrPostgresTestConfig,
    identity: Mapping[str, Any],
) -> None:
    """Validate a real PostgreSQL server identity before destructive SQL."""
    current_database = identity.get("current_database")
    address = identity.get("inet_server_addr")
    server_port = identity.get("inet_server_port")
    server_version = identity.get("server_version")

    if str(address) not in _ALLOWED_HOSTS:
        raise SrrPostgresTestSafetyError(
            f"STOP: server address is not local: {address!r}"
        )
    if int(server_port) != config.port:
        raise SrrPostgresTestSafetyError(
            f"STOP: server port mismatch: expected {config.port}, got {server_port!r}"
        )
    if str(server_version).split(".", 1)[0] != "17":
        raise SrrPostgresTestSafetyError(
            f"STOP: server major version must be 17; got {server_version!r}"
        )
    if current_database not in {"postgres", config.database, config.activation_database}:
        raise SrrPostgresTestSafetyError(
            f"STOP: unexpected current database: {current_database!r}"
        )


def open_admin_connection(config: SrrPostgresTestConfig, connect_factory: Any) -> Any:
    """Open a PostgreSQL administration connection outside a transaction block.

    ``CREATE DATABASE`` and ``DROP DATABASE`` require autocommit before the
    identity SELECT is executed. Any setup or identity-check failure rolls
    back defensively, closes the connection, and propagates fail-closed.
    """
    conn = connect_factory(
        host=config.host,
        port=config.port,
        database="postgres",
        user=config.user,
        timeout=5,
    )
    try:
        conn.autocommit = True
        if getattr(conn, "autocommit", False) is not True:
            raise SrrPostgresTestSafetyError(
                "STOP: administrative PostgreSQL connection must use autocommit=True"
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_database(), current_setting('server_version'), "
                "inet_server_addr(), inet_server_port()"
            )
            database, version, address, port = cur.fetchone()
            validate_server_identity(
                config,
                {
                    "current_database": database,
                    "server_version": version,
                    "inet_server_addr": address,
                    "inet_server_port": port,
                },
            )
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn


def close_admin_connection(conn: Any) -> None:
    """Rollback and close an administrative connection without masking errors."""
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


class SrrPostgresBootstrapError(RuntimeError):
    """Raised when the disposable SRR persistence bootstrap cannot proceed."""


@dataclass(frozen=True)
class SrrPostgresMigration:
    name: str
    path: str
    expected_tables: tuple[str, ...] = ()
    expected_functions: tuple[str, ...] = ()
    expected_triggers: tuple[str, ...] = ()


SRR_PERSISTENCE_MIGRATION_SEQUENCE: tuple[SrrPostgresMigration, ...] = (
    SrrPostgresMigration(
        name="008_analytics_foundation.sql",
        path="sql/migrations/008_analytics_foundation.sql",
        expected_tables=(
            "analytics.analysis_run",
            "analytics.analysis_stage_run",
            "analytics.data_quality_result",
        ),
        expected_functions=("analytics.update_updated_at_column",),
    ),
    SrrPostgresMigration(
        name="035_research_foundation.sql",
        path="sql/migrations/035_research_foundation.sql",
        expected_tables=(
            "research.finding",
            "research.finding_occurrence",
            "research.hypothesis",
            "research.hypothesis_finding",
            "research.experiment",
            "research.experiment_run",
            "research.validation_result",
            "research.change_candidate",
            "research.production_change",
            "research.monitoring_result",
            "research.transition_history",
            "research.fingerprint",
        ),
        expected_functions=(
            "research.fn_set_updated_at",
            "research.fn_block_transition_history_mutation",
        ),
        expected_triggers=(
            "trg_transition_history_immutability",
            "trg_finding_updated_at",
            "trg_hypothesis_updated_at",
            "trg_experiment_updated_at",
        ),
    ),
    SrrPostgresMigration(
        name="049_generic_research_framework.sql",
        path="sql/migrations/049_generic_research_framework.sql",
        expected_tables=(
            "research.research_experiment",
            "research.research_observation",
            "research.research_signal",
            "research.research_outcome",
        ),
        expected_functions=("research.fn_set_updated_at",),
        expected_triggers=("trg_research_experiment_updated_at", "trg_research_outcome_updated_at"),
    ),
    SrrPostgresMigration(
        name="050_prospective_oos_v3_experiments.sql",
        path="sql/migrations/050_prospective_oos_v3_experiments.sql",
        expected_tables=(
            "research.prospective_experiment",
            "research.prospective_observation",
            "research.prospective_outcome",
        ),
        expected_triggers=(
            "trg_prospective_experiment_updated_at",
            "trg_prospective_outcome_updated_at",
        ),
    ),
    SrrPostgresMigration(
        name="052_srr_oos_prospective_experiment.sql",
        path="sql/migrations/052_srr_oos_prospective_experiment.sql",
        expected_tables=("research.prospective_experiment",),
    ),
    SrrPostgresMigration(
        name="056_prospective_direction_lifecycle.sql",
        path="sql/migrations/056_prospective_direction_lifecycle.sql",
        expected_tables=("research.prospective_experiment_direction_state",),
    ),
    SrrPostgresMigration(
        name="060_srr_short_execution_r_expansion_prospective_registration.sql",
        path="sql/migrations/060_srr_short_execution_r_expansion_prospective_registration.sql",
        expected_tables=("research.prospective_experiment",),
    ),
    SrrPostgresMigration(
        name="061_srr_short_execution_outcome_persistence.sql",
        path="sql/migrations/061_srr_short_execution_outcome_persistence.sql",
        expected_tables=("research.srr_short_execution_prospective_outcome",),
        expected_functions=("research.fn_block_srr_short_execution_final_update",),
        expected_triggers=(
            "trg_srr_short_execution_outcome_updated_at",
            "trg_srr_short_execution_outcome_block_final_update",
        ),
    ),
    SrrPostgresMigration(
        name="062_srr_short_writer_activation_boundary.sql",
        path="sql/migrations/062_srr_short_writer_activation_boundary.sql",
        expected_tables=("research.srr_short_writer_activation",),
        expected_functions=(
            "research.fn_block_srr_short_writer_activation_change",
            "research.fn_block_srr_short_writer_activation_delete",
        ),
        expected_triggers=(
            "trg_srr_short_writer_activation_updated_at",
            "trg_srr_short_writer_activation_block_change",
            "trg_srr_short_writer_activation_block_delete",
        ),
    ),
)


SRR_EXPERIMENT_ID = "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1"
SRR_TEST_EXPERIMENT_ID = "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1"
SRR_TEST_BOUNDARY_VERSION = "SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1"
SRR_TEST_ACTIVATION_TS = "2026-10-07T09:00:00Z"


def _seed_activation_ts_ns(value: Any) -> int:
    from app.research.srr_short_writer_activation_boundary_v1 import (
        normalize_activation_ts,
    )
    return normalize_activation_ts(value)[0]


def validate_test_activation_seed(
    activation_ts: str,
    signal_times: tuple[Any, ...],
) -> str:
    """Validate a synthetic activation boundary against every observation."""
    from app.research.srr_short_writer_activation_boundary_v1 import (
        normalize_activation_ts,
        SrrShortWriterActivationBoundary,
    )

    canonical, _ = normalize_activation_ts(activation_ts)
    if canonical != activation_ts and not (
        activation_ts.endswith("Z") and "." not in activation_ts
    ):
        raise SrrPostgresTestSafetyError(
            "STOP: synthetic activation timestamp must be canonical UTC"
        )
    boundary = SrrShortWriterActivationBoundary.create(activation_ts)
    for signal_time in signal_times:
        if not boundary.is_eligible(
            experiment_id=SRR_TEST_EXPERIMENT_ID,
            direction="SHORT",
            signal_time=signal_time,
        ):
            raise SrrPostgresTestSafetyError(
                f"STOP: synthetic observation is not strictly after activation: {signal_time!r}"
            )
    return activation_ts


def _activation_seed_expected(activation_ts: str, status: str) -> tuple[Any, ...]:
    return (
        SRR_TEST_EXPERIMENT_ID,
        "SHORT",
        SRR_TEST_BOUNDARY_VERSION,
        activation_ts,
        status,
    )


def _activation_seed_matches(
    existing: Mapping[str, Any],
    activation_ts: str,
    status: str,
) -> bool:
    expected = _activation_seed_expected(activation_ts, status)
    normalized_ts = _seed_activation_ts_ns(existing.get("activation_ts"))
    wanted_ts = _seed_activation_ts_ns(activation_ts)
    return (
        existing.get("experiment_id") == expected[0]
        and existing.get("direction") == expected[1]
        and existing.get("boundary_version") == expected[2]
        and normalized_ts == wanted_ts
        and existing.get("status") == expected[4]
    )


def seed_test_activation_record(
    conn: Any,
    *,
    activation_ts: str = SRR_TEST_ACTIVATION_TS,
    signal_times: tuple[Any, ...] = (),
    status: str = "ACTIVE",
    config: SrrPostgresTestConfig | None = None,
) -> Mapping[str, Any]:
    """Insert exactly one synthetic activation record in a disposable DB."""
    validate_test_activation_seed(activation_ts, signal_times)
    if config is None:
        config = load_srr_postgres_test_config()
    validate_bootstrap_database(config.database)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_database(), current_setting('server_version'), "
            "inet_server_addr(), inet_server_port()"
        )
        database, version, address, port = cur.fetchone()
        validate_server_identity(
            config,
            {
                "current_database": database,
                "server_version": version,
                "inet_server_addr": address,
                "inet_server_port": port,
            },
        )
        if database not in {config.database, config.activation_database}:
            raise SrrPostgresTestSafetyError(
                "STOP: activation seed requires the configured disposable database"
            )
        if status not in {"ACTIVE", "PENDING", "REVOKED"}:
            raise SrrPostgresTestSafetyError("STOP: invalid synthetic activation status")
        cur.execute(
            "SELECT experiment_id, direction, boundary_version, activation_ts, status "
            "FROM research.srr_short_writer_activation WHERE experiment_id=%s FOR UPDATE",
            (SRR_TEST_EXPERIMENT_ID,),
        )
        existing = cur.fetchone()
        if existing is not None:
            if not _activation_seed_matches(
                dict(
                    zip(
                        (
                            "experiment_id",
                            "direction",
                            "boundary_version",
                            "activation_ts",
                            "status",
                        ),
                        existing,
                    )
                ),
                activation_ts,
                status,
            ):
                raise SrrPostgresTestSafetyError(
                    "STOP: existing activation record does not exactly match synthetic seed"
                )
            return {
                "experiment_id": existing[0],
                "direction": existing[1],
                "boundary_version": existing[2],
                "activation_ts": existing[3],
                "status": existing[4],
                "reused": True,
            }
        cur.execute(
            "INSERT INTO research.srr_short_writer_activation "
            "(experiment_id, direction, boundary_version, activation_ts, status, notes) "
            "VALUES (%s, 'SHORT', %s, %s::timestamptz, %s, %s)",
            (
                SRR_TEST_EXPERIMENT_ID,
                SRR_TEST_BOUNDARY_VERSION,
                activation_ts,
                status,
                "synthetic disposable integration-test record",
            ),
        )
        conn.commit()
        cur.execute(
            "SELECT experiment_id, direction, boundary_version, activation_ts, status "
            "FROM research.srr_short_writer_activation WHERE experiment_id=%s",
            (SRR_EXPERIMENT_ID,),
        )
        seeded = cur.fetchone()
    if seeded is None or seeded[-1] != status:
        conn.rollback()
        raise SrrPostgresTestSafetyError("STOP: synthetic activation seed verification failed")
    return {
        "experiment_id": seeded[0],
        "direction": seeded[1],
        "boundary_version": seeded[2],
        "activation_ts": seeded[3],
        "status": seeded[4],
    }


def validate_bootstrap_database(
    name: str, env: Mapping[str, str] | None = None
) -> None:
    """Allow bootstrap only for the explicit new disposable database."""
    values = dict(os.environ if env is None else env)
    values["TEST_DB_HOST"] = values.get("TEST_DB_HOST", "127.0.0.1")
    values["TEST_DB_PORT"] = values.get("TEST_DB_PORT", "55432")
    values["TEST_DB_USER"] = values.get("TEST_DB_USER", "postgres")
    values["SRR_TEST_PERSISTENCE_DB"] = name
    values["SRR_TEST_ACTIVATION_DB"] = name
    load_srr_postgres_test_config(values)


def open_bootstrap_connection(
    config: SrrPostgresTestConfig, connect_factory: Any
) -> Any:
    """Open and identity-check the disposable bootstrap target database."""
    validate_bootstrap_database(config.database)
    conn = connect_factory(
        host=config.host,
        port=config.port,
        database=config.database,
        user=config.user,
        timeout=5,
    )
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_database(), current_setting('server_version'), "
                "inet_server_addr(), inet_server_port()"
            )
            database, version, address, port = cur.fetchone()
            validate_server_identity(
                config,
                {
                    "current_database": database,
                    "server_version": version,
                    "inet_server_addr": address,
                    "inet_server_port": port,
                },
            )
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn


def close_bootstrap_connection(conn: Any) -> None:
    """Rollback and close a bootstrap connection without masking errors."""
    close_admin_connection(conn)


def apply_migration_sequence(
    conn: Any,
    repository_root: Any,
    migrations: tuple[SrrPostgresMigration, ...] = SRR_PERSISTENCE_MIGRATION_SEQUENCE,
) -> list[str]:
    """Apply repository migrations in order with transactional fail-closed steps."""
    from pathlib import Path

    root = Path(repository_root)
    applied: list[str] = []
    for migration in migrations:
        path = root / migration.path
        if not path.is_file():
            raise SrrPostgresBootstrapError(
                f"STOP: required migration file is missing: {path}"
            )
        sql = path.read_text(encoding="utf-8")
        try:
            conn.cursor().execute(sql)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise SrrPostgresBootstrapError(
                f"STOP: migration failed before later migrations: {migration.name}"
            ) from None
        applied.append(migration.name)
    return applied


def verify_post_bootstrap(
    conn: Any,
    migrations: tuple[SrrPostgresMigration, ...] = SRR_PERSISTENCE_MIGRATION_SEQUENCE,
) -> dict[str, Any]:
    """Read-only verification of every required object and seed row."""
    objects: dict[str, Any] = {"tables": [], "functions": [], "triggers": [], "seeds": {}}
    for migration in migrations:
        for table in migration.expected_tables:
            schema, name = table.split(".", 1)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass(%s::text)",
                    (table,),
                )
                if cur.fetchone()[0] is None:
                    raise SrrPostgresBootstrapError(
                        f"STOP: missing required table {table} after {migration.name}"
                    )
            objects["tables"].append(table)
        for function in migration.expected_functions:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname=%s AND p.proname=%s",
                    tuple(function.split(".", 1)),
                )
                if cur.fetchone() is None:
                    raise SrrPostgresBootstrapError(
                        f"STOP: missing required function {function} after {migration.name}"
                    )
            objects["functions"].append(function)
        for trigger in migration.expected_triggers:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_trigger WHERE tgname=%s AND NOT tgisinternal",
                    (trigger,),
                )
                if cur.fetchone() is None:
                    raise SrrPostgresBootstrapError(
                        f"STOP: missing required trigger {trigger} after {migration.name}"
                    )
            objects["triggers"].append(trigger)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT experiment_id, status, started_at FROM research.prospective_experiment "
            "WHERE experiment_id=%s",
            ("SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1",),
        )
        row = cur.fetchone()
        if row is None or row[1] != "READY_TO_START" or row[2] is not None:
            raise SrrPostgresBootstrapError(
                "STOP: SRR prospective experiment seed is not READY_TO_START with NULL started_at"
            )
        objects["seeds"]["srr_experiment"] = row
        cur.execute(
            "SELECT count(*) FROM research.srr_short_execution_prospective_outcome"
        )
        objects["seeds"]["outcome_rows"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM research.srr_short_writer_activation")
        objects["seeds"]["activation_rows"] = cur.fetchone()[0]
    return objects


def validate_disposable_database(name: str, env: Mapping[str, str] | None = None) -> None:
    """Allow destructive operations only for a fresh per-run database."""
    values = dict(os.environ if env is None else env)
    values.setdefault("TEST_DB_HOST", "127.0.0.1")
    values.setdefault("TEST_DB_PORT", "55432")
    values["SRR_TEST_PERSISTENCE_DB"] = name
    values["SRR_TEST_ACTIVATION_DB"] = name
    values.setdefault("TEST_DB_USER", "postgres")
    load_srr_postgres_test_config(values)
