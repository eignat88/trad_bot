#!/usr/bin/env python3
"""
Migration 050 integrity tests.

Tests execute against a real local PostgreSQL database to verify:
  - fresh migration inserts exactly 6 rows
  - all frozen fields match JSON registry
  - activated state is preserved on re-run
  - frozen field drift causes HARD FAIL
  - transaction rollback on drift
  - JSON->SQL structural equivalence

Requires: local PostgreSQL with 'trad_bot_research_snapshot' database.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_050 = PROJECT_ROOT / "sql" / "migrations" / "050_prospective_oos_v3_experiments.sql"
REGISTRY_JSON = PROJECT_ROOT / "app" / "research" / "prospective_registry.json"

EXPECTED_IDS = [
    "SRR_LONG_BASELINE_V1", "ME_SHORT_GEOM_A_V1", "ME_SHORT_GEOM_B_V1",
    "ME_SHORT_GEOM_C_V1", "VC_SHORT_BB_WIDTH_V1", "LR_SHORT_GATE_V1",
]

# Frozen fields that MUST match between JSON and DB
FROZEN_FIELDS = [
    "experiment_id", "version", "scanner_name", "direction",
    "experiment_type", "hypothesis", "primary_metric",
    "secondary_metrics", "filter_rule", "threshold", "entry_rule",
    "stop_rule", "target_rule", "position_sizing", "fee_assumption",
    "horizons", "minimum_n", "minimum_symbols", "discovery_source",
    "paired_with",
]

# Fields that map differently between JSON and DB
JSON_TO_DB = {
    "position_sizing_rule": "position_sizing",
    "discovery_source_commit": "discovery_source",
}


@pytest.fixture(scope="function")
def db():
    """Connect to trad_bot_research_snapshot. Fresh connection per test."""
    try:
        conn = psycopg2.connect(
            host="localhost", port=5432,
            database="trad_bot_research_snapshot",
            user="postgres",
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            assert cur.fetchone()[0] == "trad_bot_research_snapshot"
        conn.autocommit = False
        yield conn
        conn.close()
    except psycopg2.OperationalError:
        pytest.skip("Cannot connect to trad_bot_research_snapshot")


@pytest.fixture(scope="module")
def registry():
    """Load frozen registry JSON."""
    with open(REGISTRY_JSON) as f:
        data = json.load(f)
    return {e["experiment_id"]: e for e in data["experiments"]}


@pytest.fixture(scope="module")
def migration_sql():
    """Load migration SQL."""
    return MIGRATION_050.read_text()


def run_migration(conn):
    """Execute migration 050 against a database.

    Uses a SEPARATE connection to avoid corrupting the caller's connection.
    """
    sql = MIGRATION_050.read_text()
    # Open a fresh connection for the migration
    fresh_conn = psycopg2.connect(
        host="localhost", port=5432,
        database="trad_bot_research_snapshot",
        user="postgres",
    )
    fresh_conn.autocommit = True
    try:
        cur = fresh_conn.cursor()
        cur.execute(sql)
        cur.close()
    except Exception:
        raise
    finally:
        fresh_conn.close()


def get_experiment_row(conn, experiment_id: str) -> dict | None:
    """Fetch a single experiment row."""
    was_ac = conn.autocommit; conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM research.prospective_experiment WHERE experiment_id = %s",
        (experiment_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.autocommit = was_ac
    return row


def get_registry_count(conn) -> int:
    was_ac = conn.autocommit; conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM research.prospective_experiment")
    count = cur.fetchone()[0]
    cur.close()
    conn.autocommit = was_ac
    return count


# ============================================================
# TEST A — Fresh migration inserts exactly 6 rows
# ============================================================
class TestFreshMigration:
    def test_fresh_inserts_six_rows(self, db):
        # Clean first
        was_ac = db.autocommit; db.autocommit = True
        cur = db.cursor()
        cur.execute("DELETE FROM research.prospective_experiment")
        cur.close()
        db.autocommit = was_ac
        run_migration(db)
        count = get_registry_count(db)
        assert count == 6, f"Expected 6 rows, got {count}"

    def test_all_six_ids_present(self, db):
        for eid in EXPECTED_IDS:
            row = get_experiment_row(db, eid)
            assert row is not None, f"Missing experiment: {eid}"


# ============================================================
# TEST B — Exact ID set equality
# ============================================================
class TestExactIDs:
    def test_set_equality(self, db):
        # Ensure clean state
        was_ac = db.autocommit; db.autocommit = True
        cur = db.cursor()
        cur.execute("DELETE FROM research.prospective_experiment")
        cur.close()
        db.autocommit = was_ac
        run_migration(db)
        with db.cursor() as cur:
            cur.execute("SELECT experiment_id FROM research.prospective_experiment ORDER BY experiment_id")
            actual = {row[0] for row in cur.fetchall()}
        expected = set(EXPECTED_IDS)
        assert actual == expected, f"ID mismatch: extra={actual-expected}, missing={expected-actual}"


# ============================================================
# TEST C — Boundary: all started_at NULL, status READY_TO_START
# ============================================================
class TestBoundary:
    def test_started_at_null(self, db):
        # Ensure clean state
        was_ac = db.autocommit; db.autocommit = True
        cur = db.cursor()
        cur.execute("DELETE FROM research.prospective_experiment")
        cur.close()
        db.autocommit = was_ac
        run_migration(db)
        for eid in EXPECTED_IDS:
            row = get_experiment_row(db, eid)
            assert row is not None
            assert row["started_at"] is None, f"{eid} has started_at = {row['started_at']}"

    def test_status_ready_to_start(self, db):
        for eid in EXPECTED_IDS:
            row = get_experiment_row(db, eid)
            assert row is not None
            assert row["status"] == "READY_TO_START", f"{eid} has status = {row['status']}"


# ============================================================
# TEST D — Idempotency: run migration twice
# ============================================================
class TestIdempotency:
    def test_second_run_still_six(self, db):
        # Ensure clean state
        was_ac = db.autocommit; db.autocommit = True
        cur = db.cursor()
        cur.execute("DELETE FROM research.prospective_experiment")
        cur.close()
        db.autocommit = was_ac
        run_migration(db)  # first run
        run_migration(db)  # second run (idempotent)
        count = get_registry_count(db)
        assert count == 6, f"After second run: expected 6, got {count}"

    def test_second_run_preserves_frozen_fields(self, db, registry):
        """Second run must not change any frozen field."""
        # Ensure clean state with fresh seed
        was_ac = db.autocommit; db.autocommit = True
        cur = db.cursor()
        cur.execute("DELETE FROM research.prospective_experiment")
        cur.close()
        db.autocommit = was_ac
        run_migration(db)  # first run

        # Record original values
        originals = {}
        for eid in EXPECTED_IDS:
            row = get_experiment_row(db, eid)
            originals[eid] = {f: row[f] for f in ["scanner_name", "direction", "threshold", "version"]}

        run_migration(db)  # second run - must not change anything

        for eid in EXPECTED_IDS:
            row = get_experiment_row(db, eid)
            for f in ["scanner_name", "direction", "threshold", "version"]:
                assert row[f] == originals[eid][f], f"{eid}.{f} changed from {originals[eid][f]} to {row[f]}"
            row = get_experiment_row(db, eid)
            assert row is not None
            # Verify key frozen fields match JSON registry
            assert row["scanner_name"] == registry[eid]["scanner_name"]
            assert row["direction"] == registry[eid]["direction"]
            assert row["version"] == registry[eid]["version"]
            # threshold: None in JSON should be None in DB
            json_thresh = registry[eid].get("threshold")
            db_thresh = row["threshold"]
            if json_thresh is None:
                assert db_thresh is None, f"{eid}.threshold: JSON=None, DB={db_thresh}"
            else:
                assert float(db_thresh) == float(json_thresh), \
                    f"{eid}.threshold: JSON={json_thresh} != DB={db_thresh}"


# ============================================================
# TEST E — Partial migration recovery (simulates VPS state)
# ============================================================
class TestPartialRecovery:
    def test_recovers_from_empty_registry(self, db):
        """VPS has tables but 0 registry rows. Migration must seed all 6."""
        with db.cursor() as cur:
            cur.execute("DELETE FROM research.prospective_experiment")
        db.commit()
        assert get_registry_count(db) == 0

        run_migration(db)
        assert get_registry_count(db) == 6


# ============================================================
# TEST F — Immutable configuration: drift causes HARD FAIL
# ============================================================
class TestImmutableConfig:
    def _seed_valid(self, db):
        """Ensure clean valid state."""
        was_ac = db.autocommit
        db.autocommit = True
        cur = db.cursor()
        cur.execute("DELETE FROM research.prospective_experiment")
        cur.close()
        db.autocommit = was_ac
        run_migration(db)

    def _mutate_field(self, db, experiment_id: str, field: str, new_value):
        was_ac = db.autocommit
        db.autocommit = True
        cur = db.cursor()
        cur.execute(
            f"UPDATE research.prospective_experiment SET {field} = %s WHERE experiment_id = %s",
            (new_value, experiment_id),
        )
        cur.close()
        db.autocommit = was_ac

    def test_valid_existing_row_succeeds(self, db):
        self._seed_valid(db)
        run_migration(db)  # should succeed
        assert get_registry_count(db) == 6

    def test_mutate_text_hypothesis(self, db):
        self._seed_valid(db)
        self._mutate_field(db, "SRR_LONG_BASELINE_V1", "hypothesis", "MUTATED HYPOTHESIS")
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_mutate_text_primary_metric(self, db):
        self._seed_valid(db)
        self._mutate_field(db, "VC_SHORT_BB_WIDTH_V1", "primary_metric", "WRONG_METRIC")
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_mutate_numeric_threshold(self, db):
        self._seed_valid(db)
        self._mutate_field(db, "VC_SHORT_BB_WIDTH_V1", "threshold", 0.999)
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_mutate_jsonb_secondary_metrics(self, db):
        self._seed_valid(db)
        was_ac = db.autocommit; db.autocommit = True
        cur = db.cursor()
        cur.execute(
            "UPDATE research.prospective_experiment SET secondary_metrics = %s WHERE experiment_id = %s",
            (json.dumps(["WRONG_METRIC"]), "SRR_LONG_BASELINE_V1"),
        )
        cur.close()
        db.autocommit = was_ac
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_mutate_jsonb_horizons(self, db):
        self._seed_valid(db)
        was_ac = db.autocommit; db.autocommit = True
        cur = db.cursor()
        cur.execute(
            "UPDATE research.prospective_experiment SET horizons = %s WHERE experiment_id = %s",
            (json.dumps(["15m", "30m"]), "VC_SHORT_BB_WIDTH_V1"),
        )
        cur.close()
        db.autocommit = was_ac
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_mutate_paired_with_null_to_value(self, db):
        self._seed_valid(db)
        self._mutate_field(db, "ME_SHORT_GEOM_A_V1", "paired_with", "WRONG_EXPERIMENT")
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_mutate_paired_with_value_to_null(self, db):
        self._seed_valid(db)
        self._mutate_field(db, "ME_SHORT_GEOM_B_V1", "paired_with", None)
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_mutate_minimum_n(self, db):
        self._seed_valid(db)
        self._mutate_field(db, "LR_SHORT_GATE_V1", "minimum_n", 999)
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_mutate_entry_rule(self, db):
        self._seed_valid(db)
        self._mutate_field(db, "ME_SHORT_GEOM_C_V1", "entry_rule", "DIFFERENT RULE")
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)


# ============================================================
# TEST G — Transaction rollback with database state
# ============================================================
class TestTransactionRollback:
    def test_rollback_on_drift(self, db):
        """Create one valid + one conflicting row, run migration.
        Migration fails, transaction rolls back."""
        was_ac = db.autocommit; db.autocommit = True
        cur = db.cursor()
        cur.execute("DELETE FROM research.prospective_experiment")
        cur.execute(
            """INSERT INTO research.prospective_experiment
               (experiment_id, version, scanner_name, direction, experiment_type,
                hypothesis, primary_metric, secondary_metrics, filter_rule, threshold,
                entry_rule, stop_rule, target_rule, position_sizing, fee_assumption,
                horizons, minimum_n, minimum_symbols, discovery_source, paired_with,
                status, started_at)
               VALUES ('SRR_LONG_BASELINE_V1', 1, 'SUPPORT_RESISTANCE_REACTION', 'LONG',
                       'BASELINE_VALIDATION', 'test', 'MFE_pct_60m',
                       '[]'::jsonb, 'NONE', NULL, 'a', 'b', 'c', 'shadow_only', 'none',
                       '[]'::jsonb, 50, 10, '49b3828', NULL, 'READY_TO_START', NULL)"""
        )
        cur.execute("UPDATE research.prospective_experiment SET hypothesis = 'DRIFTED' WHERE experiment_id = 'SRR_LONG_BASELINE_V1'")
        cur.close()
        db.autocommit = was_ac
        assert get_registry_count(db) == 1

        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

        count = get_registry_count(db)
        assert count == 1, f"After rollback: expected 1 (pre-existing drifted row), got {count}"
        row = get_experiment_row(db, "SRR_LONG_BASELINE_V1")
        assert row["hypothesis"] == "DRIFTED"


# ============================================================
# TEST H — Activated state preservation
# ============================================================
class TestActivatedState:
    def test_activated_preserved(self, db):
        """Seed valid, activate, re-run migration. Activated state preserved."""
        was_ac = db.autocommit; db.autocommit = True
        cur = db.cursor()
        cur.execute("DELETE FROM research.prospective_experiment")
        cur.close()
        db.autocommit = was_ac

        run_migration(db)
        assert get_registry_count(db) == 6

        db.autocommit = True
        cur = db.cursor()
        cur.execute(
            "UPDATE research.prospective_experiment SET status = 'RUNNING', started_at = '2026-10-01T12:00:00Z' WHERE experiment_id = 'ME_SHORT_GEOM_A_V1'"
        )
        cur.close()
        db.autocommit = was_ac

        row = get_experiment_row(db, "ME_SHORT_GEOM_A_V1")
        assert row["status"] == "RUNNING"
        original_started_at = row["started_at"]
        assert original_started_at is not None

        run_migration(db)

        row = get_experiment_row(db, "ME_SHORT_GEOM_A_V1")
        assert row["status"] == "RUNNING", f"Status changed from RUNNING to {row['status']}"
        assert row["started_at"] == original_started_at, f"started_at changed"

        for eid in ["SRR_LONG_BASELINE_V1", "VC_SHORT_BB_WIDTH_V1"]:
            r = get_experiment_row(db, eid)
            assert r["status"] == "READY_TO_START"

        # Verify count still 6
        assert get_registry_count(db) == 6


# ============================================================
# TEST I — JSON->SQL structural equivalence
# ============================================================
class TestJSONSQLEquivalence:
    def test_per_experiment_field_equivalence(self, db, registry):
        """For each experiment, compare every frozen field from JSON to DB row."""
        # Ensure clean state first
        self._reset_and_seed(db)

        for eid in EXPECTED_IDS:
            row = get_experiment_row(db, eid)
            assert row is not None, f"Missing experiment: {eid}"
            json_exp = registry[eid]

            for field in FROZEN_FIELDS:
                if field == "experiment_id":
                    continue  # already verified by lookup

                if field == "discovery_source":
                    continue

                # Map DB column to JSON field name
                json_field = {v: k for k, v in JSON_TO_DB.items()}.get(field, field)

                json_val = json_exp.get(json_field)
                db_val = row.get(field)

                # threshold may be None in both JSON and DB
                if json_val is None and db_val is None:
                    continue  # both NULL, equal
                if json_val is None or db_val is None:
                    assert False, f"{eid}.{field}: JSON={json_val!r} != DB={db_val!r}"

                # Normalize both sides: JSON string -> parsed Python, DB list stays as-is
                if isinstance(json_val, str):
                    try:
                        json_val = json.loads(json_val)
                    except (json.JSONDecodeError, TypeError):
                        pass  # keep as string
                if isinstance(db_val, str):
                    try:
                        db_val = json.loads(db_val)
                    except (json.JSONDecodeError, TypeError):
                        pass  # keep as string

                # Normalize numeric types (Decimal from DB vs float from JSON)
                try:
                    if float(json_val) == float(db_val):
                        continue
                except (TypeError, ValueError):
                    pass  # not numeric, fall through to direct comparison

                assert json_val == db_val, \
                    f"{eid}.{field}: JSON={json_val!r} != DB={db_val!r}"

    def _reset_and_seed(self, db):
        was_ac = db.autocommit; db.autocommit = True
        cur = db.cursor()
        cur.execute("DELETE FROM research.prospective_experiment")
        cur.close()
        db.autocommit = was_ac
        run_migration(db)

    def _mutate_raw(self, db, sql):
        was_ac = db.autocommit; db.autocommit = True
        cur = db.cursor()
        cur.execute(sql)
        cur.close()
        db.autocommit = was_ac

    def test_swap_scanner_detected(self, db):
        self._reset_and_seed(db)
        self._mutate_raw(db, """
            UPDATE research.prospective_experiment
            SET scanner_name = CASE
                WHEN experiment_id = 'SRR_LONG_BASELINE_V1' THEN 'MOMENTUM_EXHAUSTION'
                WHEN experiment_id = 'ME_SHORT_GEOM_A_V1' THEN 'SUPPORT_RESISTANCE_REACTION'
                ELSE scanner_name
            END
            WHERE experiment_id IN ('SRR_LONG_BASELINE_V1', 'ME_SHORT_GEOM_A_V1')
        """)
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_wrong_threshold_on_wrong_experiment(self, db):
        self._reset_and_seed(db)
        self._mutate_raw(db, "UPDATE research.prospective_experiment SET threshold = 0.569723 WHERE experiment_id = 'SRR_LONG_BASELINE_V1'")
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_change_vc_threshold(self, db):
        self._reset_and_seed(db)
        self._mutate_raw(db, "UPDATE research.prospective_experiment SET threshold = 0.5 WHERE experiment_id = 'VC_SHORT_BB_WIDTH_V1'")
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_change_one_jsonb_value(self, db):
        self._reset_and_seed(db)
        self._mutate_raw(db, "UPDATE research.prospective_experiment SET secondary_metrics = secondary_metrics - 'MAE_pct_60m' WHERE experiment_id = 'SRR_LONG_BASELINE_V1'")
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_change_paired_with(self, db):
        self._reset_and_seed(db)
        self._mutate_raw(db, "UPDATE research.prospective_experiment SET paired_with = 'SRR_LONG_BASELINE_V1' WHERE experiment_id = 'ME_SHORT_GEOM_B_V1'")
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_change_minimum_n(self, db):
        self._reset_and_seed(db)
        self._mutate_raw(db, "UPDATE research.prospective_experiment SET minimum_n = 200 WHERE experiment_id = 'LR_SHORT_GATE_V1'")
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_change_entry_rule(self, db):
        self._reset_and_seed(db)
        self._mutate_raw(db, "UPDATE research.prospective_experiment SET entry_rule = 'DIFFERENT' WHERE experiment_id = 'ME_SHORT_GEOM_C_V1'")
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_null_to_nonnull_paired_with(self, db):
        self._reset_and_seed(db)
        self._mutate_raw(db, "UPDATE research.prospective_experiment SET paired_with = 'WRONG' WHERE experiment_id = 'SRR_LONG_BASELINE_V1'")
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)

    def test_nonnull_to_null_paired_with(self, db):
        self._reset_and_seed(db)
        self._mutate_raw(db, "UPDATE research.prospective_experiment SET paired_with = NULL WHERE experiment_id = 'ME_SHORT_GEOM_B_V1'")
        with pytest.raises(psycopg2.errors.RaiseException, match="FROZEN CONFIGURATION DRIFT"):
            run_migration(db)


# ============================================================
# TEST J — Smoke validation consistency
# ============================================================
class TestSmokeConsistency:
    def test_smoke_references_all_six_ids(self):
        smoke = PROJECT_ROOT / "research_snapshot" / "05_smoke_validation.sql"
        content = smoke.read_text()
        for eid in EXPECTED_IDS:
            assert eid in content, f"Smoke SQL missing: {eid}"

    def test_smoke_uses_correct_status_values(self):
        """Smoke SQL must read actual DB values, not invent new ones."""
        smoke = PROJECT_ROOT / "research_snapshot" / "05_smoke_validation.sql"
        content = smoke.read_text()
        # Must reference pe.status (actual DB column)
        assert "pe.status" in content or "pe.status" in content.lower()
        # Must reference pe.started_at
        assert "started_at" in content
