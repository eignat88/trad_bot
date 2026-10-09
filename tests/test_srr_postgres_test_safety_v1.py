from __future__ import annotations

import pytest

from datetime import datetime, timezone

from tests.srr_postgres_test_safety import (
    SrrPostgresTestSafetyError,
    SRR_PERSISTENCE_MIGRATION_SEQUENCE,
    SRR_TEST_ACTIVATION_TS,
    close_admin_connection,
    load_srr_postgres_test_config,
    open_admin_connection,
    seed_test_activation_record,
    validate_bootstrap_database,
    validate_disposable_database,
    validate_server_identity,
    validate_test_activation_seed,
)


def valid_env():
    return {
        "TEST_DB_HOST": "127.0.0.1",
        "TEST_DB_PORT": "55432",
        "SRR_TEST_PERSISTENCE_DB": "trad_bot_srr_persistence_it_20261009_v3",
        "SRR_TEST_ACTIVATION_DB": "trad_bot_srr_activation_it_20261009_v3",
        "TEST_DB_USER": "postgres",
    }


def test_valid_configuration_uses_explicit_names():
    config = load_srr_postgres_test_config(valid_env())
    assert config.host == "127.0.0.1"
    assert config.port == 55432
    assert config.database == "trad_bot_srr_persistence_it_20261009_v3"
    assert config.activation_database == "trad_bot_srr_activation_it_20261009_v3"


@pytest.mark.parametrize("missing", ["TEST_DB_HOST", "TEST_DB_PORT", "SRR_TEST_PERSISTENCE_DB", "SRR_TEST_ACTIVATION_DB"])
def test_missing_configuration_fails_closed(missing):
    env = valid_env()
    env.pop(missing)
    with pytest.raises(SrrPostgresTestSafetyError, match="required"):
        load_srr_postgres_test_config(env)


def test_non_local_host_fails_closed():
    env = valid_env()
    env["TEST_DB_HOST"] = "10.0.0.5"
    with pytest.raises(SrrPostgresTestSafetyError, match="TEST_DB_HOST"):
        load_srr_postgres_test_config(env)


@pytest.mark.parametrize("port", ["5432", "0", "70000", "abc"])
def test_invalid_or_production_port_fails_closed(port):
    env = valid_env()
    env["TEST_DB_PORT"] = port
    with pytest.raises(SrrPostgresTestSafetyError, match="TEST_DB_PORT"):
        load_srr_postgres_test_config(env)


@pytest.mark.parametrize(
    "database",
    ["trad_bot", "postgres", "template0", "template1", "trad_bot_srr_bad", "bad_name"],
)
def test_protected_or_malformed_database_fails_closed(database):
    env = valid_env()
    env["SRR_TEST_PERSISTENCE_DB"] = database
    with pytest.raises(SrrPostgresTestSafetyError):
        load_srr_postgres_test_config(env)


def test_legacy_and_operator_databases_are_protected():
    for database in (
        "trad_bot_srr_activation_it_20261008",
        "trad_bot_srr_activation_it_20261009_v2",
        "trad_bot_srr_persistence_it_20261008",
        "trad_bot_srr_persistence_it_20261009_v2",
    ):
        with pytest.raises(SrrPostgresTestSafetyError, match="protected"):
            load_srr_postgres_test_config(
                {**valid_env(), "SRR_TEST_ACTIVATION_DB": database}
            )


def test_non_test_user_fails_closed():
    env = valid_env()
    env["TEST_DB_USER"] = "trad_bot"
    with pytest.raises(SrrPostgresTestSafetyError, match="TEST_DB_USER"):
        load_srr_postgres_test_config(env)


def test_disposable_database_validation():
    validate_disposable_database("trad_bot_srr_activation_it_20261009_v3", valid_env())
    with pytest.raises(SrrPostgresTestSafetyError):
        validate_disposable_database("trad_bot", valid_env())


class FakeAdminCursor:
    def __init__(self, conn, identity=("postgres", "17.2", "127.0.0.1", 55432), fail_on_create=False):
        self.conn = conn
        self.identity = identity
        self.fail_on_create = fail_on_create
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if sql.startswith("SELECT current_database"):
            if self.identity is None:
                raise RuntimeError("invalid input syntax for type integer")
            self.conn.identity = self.identity
            self.conn.identity_read = True
        if self.fail_on_create and sql.startswith("CREATE DATABASE"):
            raise RuntimeError("25001: CREATE DATABASE cannot run inside a transaction block")

    def fetchone(self):
        if self.identity is None:
            raise RuntimeError("invalid input syntax for type integer")
        return self.identity


class FakeAdminConnection:
    def __init__(self, *, autocommit=False, fail_on_create=False):
        self.autocommit = autocommit
        self.identity = None
        self.identity_read = False
        self.rollback_count = 0
        self.close_count = 0
        self.fail_on_create = fail_on_create
        self.executed = []

    def cursor(self):
        cursor = FakeAdminCursor(self, fail_on_create=self.fail_on_create)
        self.executed = cursor.executed
        return cursor

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.close_count += 1


def test_open_admin_connection_sets_autocommit_before_identity():
    config = load_srr_postgres_test_config(valid_env())
    created = FakeAdminConnection()

    def factory(**kwargs):
        assert kwargs["database"] == "postgres"
        assert kwargs["port"] == 55432
        return created

    conn = open_admin_connection(config, factory)
    assert conn is created
    assert created.autocommit is True
    assert created.identity_read is True
    close_admin_connection(conn)
    assert created.rollback_count == 1
    assert created.close_count == 1


class AutocommitLockingAdminConnection(FakeAdminConnection):
    def __setattr__(self, name, value):
        if name == "autocommit" and value is True:
            raise RuntimeError("driver refused autocommit mode")
        super().__setattr__(name, value)


def test_open_admin_connection_rejects_non_autocommit_before_create_database():
    config = load_srr_postgres_test_config(valid_env())

    def factory(**kwargs):
        return AutocommitLockingAdminConnection()

    with pytest.raises(RuntimeError, match="autocommit mode"):
        open_admin_connection(config, factory)


def test_admin_create_database_25001_failure_rolls_back_and_closes():
    config = load_srr_postgres_test_config(valid_env())

    def factory(**kwargs):
        return FakeAdminConnection(fail_on_create=True)

    admin = open_admin_connection(config, factory)
    with pytest.raises(RuntimeError, match="25001"):
        with admin.cursor() as cur:
            cur.execute('CREATE DATABASE "trad_bot_srr_activation_it_20261009_v3"')
    close_admin_connection(admin)
    assert admin.autocommit is True
    assert admin.rollback_count == 1
    assert admin.close_count == 1


def test_admin_identity_failure_rolls_back_and_closes():
    config = load_srr_postgres_test_config(valid_env())

    def bad_factory(**kwargs):
        connection = FakeAdminConnection()
        connection.cursor = lambda: FakeAdminCursor(connection, identity=None)
        return connection

    with pytest.raises(RuntimeError, match="invalid input syntax"):
        open_admin_connection(config, bad_factory)


def test_bootstrap_sequence_uses_required_production_migrations():
    names = [migration.name for migration in SRR_PERSISTENCE_MIGRATION_SEQUENCE]
    assert names == [
        "008_analytics_foundation.sql",
        "035_research_foundation.sql",
        "049_generic_research_framework.sql",
        "050_prospective_oos_v3_experiments.sql",
        "052_srr_oos_prospective_experiment.sql",
        "056_prospective_direction_lifecycle.sql",
        "060_srr_short_execution_r_expansion_prospective_registration.sql",
        "061_srr_short_execution_outcome_persistence.sql",
        "062_srr_short_writer_activation_boundary.sql",
    ]


def test_bootstrap_database_requires_new_disposable_name():
    validate_bootstrap_database(
        "trad_bot_srr_persistence_it_20261009_v3", valid_env()
    )
    with pytest.raises(SrrPostgresTestSafetyError):
        validate_bootstrap_database("trad_bot_srr_activation_it_20261008", valid_env())


def test_activation_seed_accepts_strictly_post_boundary_signal_times():
    signal = datetime(2026, 10, 7, 9, 20, tzinfo=timezone.utc)
    assert (
        validate_test_activation_seed(SRR_TEST_ACTIVATION_TS, (signal,))
        == SRR_TEST_ACTIVATION_TS
    )


@pytest.mark.parametrize(
    "signal",
    [
        datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 7, 9, 0, tzinfo=None),
    ],
)
def test_activation_seed_rejects_boundary_equality_pre_and_naive(signal):
    with pytest.raises(SrrPostgresTestSafetyError):
        validate_test_activation_seed(SRR_TEST_ACTIVATION_TS, (signal,))


def test_activation_seed_rejects_noncanonical_timestamp():
    with pytest.raises(SrrPostgresTestSafetyError):
        validate_test_activation_seed(
            "2026-10-07T11:00:00+02:00",
            (datetime(2026, 10, 7, 9, 20, tzinfo=timezone.utc),),
        )


def test_activation_seed_requires_existing_disposable_record():
    class FakeCursor:
        def __init__(self, conn):
            self.conn = conn
            self._row = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            if sql.startswith("SELECT current_database"):
                self._row = ("postgres", "17.2", "127.0.0.1", 55432)
            elif sql.startswith("SELECT experiment_id"):
                self._row = ("SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1", "SHORT", "SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1", SRR_TEST_ACTIVATION_TS, "ACTIVE")
            elif sql.startswith("INSERT INTO research.srr_short_writer_activation"):
                raise AssertionError("must not insert into protected database")

        def fetchone(self):
            row = self._row
            self._row = None
            return row

    class FakeConn:
        def cursor(self):
            return FakeCursor(self)

    with pytest.raises(SrrPostgresTestSafetyError, match="disposable"):
        seed_test_activation_record(
            FakeConn(),
            activation_ts=SRR_TEST_ACTIVATION_TS,
            signal_times=(datetime(2026, 10, 7, 9, 20, tzinfo=timezone.utc),),
            config=load_srr_postgres_test_config(valid_env()),
        )


def test_server_identity_requires_local_pg17():
    config = load_srr_postgres_test_config(valid_env())
    validate_server_identity(
        config,
        {
            "current_database": "postgres",
            "server_version": "17.2",
            "inet_server_addr": "127.0.0.1",
            "inet_server_port": 55432,
        },
    )
    with pytest.raises(SrrPostgresTestSafetyError, match="server address"):
        validate_server_identity(
            config,
            {
                "current_database": "postgres",
                "server_version": "17.2",
                "inet_server_addr": "10.0.0.9",
                "inet_server_port": 55432,
            },
        )
    with pytest.raises(SrrPostgresTestSafetyError, match="major version"):
        validate_server_identity(
            config,
            {
                "current_database": "postgres",
                "server_version": "16.3",
                "inet_server_addr": "127.0.0.1",
                "inet_server_port": 55432,
            },
        )
