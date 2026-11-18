"""ME_LONG_V2_INVERSE_SHORT_V3 — isolation + migration 090 contract (Issue #285).

Acceptance scope for ``ME_INVERSE_SHORT_V3_ENTRY_GUARDS_AND_CAPACITY_V1``:

* migration 090 is strictly additive and V3-only — it never touches the V2
  / OOS-2 / V1 / production / research tables, activation rows, fingerprints
  or freeze_ts;
* the V3 admission code READS the V2 ledger read-only and fails closed when
  the V2 state cannot be read — it can never re-create a V2 duplicate OPEN
  row and never mutates V2 state;
* V2 detector / geometry / sizing / fees / slippage / funding / TTL are
  byte-identical to the frozen V2 protocol (pinned V2 fingerprint);
* the V3 capture hook is wired on the production source path and is
  independent of the V1 / V2 / OOS-2 flags (own ME_INVERSE_SHORT_V3_ENABLED).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_090 = REPO_ROOT / "sql" / "migrations" / "090_me_long_v2_inverse_short_v3.sql"

V3_TABLES = {
    "dds.me_long_v2_inverse_short_v3_activation",
    "dds.me_long_v2_inverse_short_v3_event",
    "dds.me_long_v2_inverse_short_v3_position",
    "dds.me_long_v2_inverse_short_v3_close_outbox",
}

# Untouched namespaces this migration must NEVER write to (V1 / V2 / OOS-2 /
# production / research).
FORBIDDEN_NAMESPACES = (
    "dds.me_long_v2_inverse_short_v1_",
    "dds.me_long_v2_inverse_short_v2_",
    "dds.me_long_v2_inverse_short_oos2_v1_",
    "dds.paper_trade",
    "dds.paper_shadow_trade",
    "dds.scanner_setup",
    "dds.signal_outcome",
    "research.",
)


@pytest.fixture(scope="module")
def sql() -> str:
    assert MIGRATION_090.exists(), "migration 090 is missing"
    return MIGRATION_090.read_text(encoding="utf-8")


def _sql_without_comments(sql: str) -> str:
    return "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )


# ---------------------------------------------------------------------------
# 1. Migration 090: additive, V3-only
# ---------------------------------------------------------------------------
class TestMigration090Contract:
    def test_transactional(self, sql):
        body = _sql_without_comments(sql)
        assert re.search(r"^BEGIN;", body, re.M)
        assert re.search(r"^COMMIT;", body, re.M)

    def test_creates_exactly_the_v3_tables(self, sql):
        body = _sql_without_comments(sql)
        created = set(
            re.findall(r"CREATE TABLE IF NOT EXISTS (\S+)", body)
        )
        assert created == V3_TABLES

    def test_no_forbidden_namespace_is_written(self, sql):
        body = _sql_without_comments(sql)
        for namespace in FORBIDDEN_NAMESPACES:
            for verb in ("INSERT ", "UPDATE ", "DELETE ", "ALTER ", "DROP ",
                         "TRUNCATE "):
                for match in re.finditer(
                    rf"{verb}[A-Z ]*?(?:INTO |FROM |TABLE |DATABASE )?"
                    rf"({re.escape(namespace)}\S*)",
                    body,
                ):
                    pytest.fail(
                        f"migration 090 must never touch "
                        f"{match.group(1)!r} ({verb.strip()})"
                    )

    def test_no_destructive_ddl_at_all(self, sql):
        body = _sql_without_comments(sql)
        assert "DROP TABLE" not in body
        assert "TRUNCATE" not in body
        assert "ALTER TABLE" not in body
        assert "DROP SCHEMA" not in body

    def test_five_state_event_disposition(self, sql):
        body = _sql_without_comments(sql)
        for status in ("CAPTURED", "ENTERED", "EXPIRED", "CAPACITY_REJECTED",
                       "SYMBOL_REJECTED"):
            assert f"'{status}'" in body
        # SYMBOL_REJECTED is terminal with exactly the Issue #285 guard
        # causes — never ENTRY_TTL / ENTRY_CAP.
        assert "'SYMBOL_ALREADY_OPEN', 'SYMBOL_COOLDOWN_12H'" in body

    def test_strict_prospective_boundary(self, sql):
        body = _sql_without_comments(sql)
        assert "signal_time > activation_freeze_ts" in body

    def test_single_open_position_per_symbol_index(self, sql):
        body = _sql_without_comments(sql)
        assert (
            "uq_me_inverse_short_v3_position_symbol_open" in body
            and "WHERE status = 'OPEN'" in body
            and "UNIQUE INDEX" in body
        )

    def test_cooldown_lookup_index(self, sql):
        body = _sql_without_comments(sql)
        assert "idx_me_inverse_short_v3_position_symbol_entered" in body
        assert "(symbol, entered_at DESC)" in body

    def test_entry_facts_immutable_trigger(self, sql):
        body = _sql_without_comments(sql)
        assert "fn_me_long_v2_inverse_short_v3_guard" in body
        assert "trg_me_inverse_short_v2_inverse_short_v3_guard" in body or (
            "trg_me_long_v2_inverse_short_v3_guard" in body
        )
        assert "entered_at IS DISTINCT FROM" in body
        assert "deadline_at IS DISTINCT FROM" in body

    def test_seed_is_inert_and_idempotent(self, sql):
        body = _sql_without_comments(sql)
        assert "'READY_TO_START'" in body
        assert "ON CONFLICT DO NOTHING" in body
        assert "CREATE TABLE IF NOT EXISTS" in body
        assert "CREATE INDEX IF NOT EXISTS" in body
        assert "CREATE UNIQUE INDEX IF NOT EXISTS" in body


# ---------------------------------------------------------------------------
# 2. V2 protocol untouched (pinned) and V3 guard coverage
# ---------------------------------------------------------------------------
class TestV2Untouched:
    def test_v2_protocol_fingerprint_pinned(self):
        from app.paper.me_long_v2_inverse_short_v2 import protocol as v2p

        # The exact pin the V2 suites already assert: the V3 work must not
        # change a single byte of the V2 protocol.
        assert v2p.protocol_fingerprint() == (
            "5a9047ca3621cefb4d53b45275ad6348887440568cb055f843f85e2eb1abdacc"
        )

    def test_v3_fingerprint_pinned_and_differs(self):
        from app.paper.me_long_v2_inverse_short_v2 import protocol as v2p
        from app.paper.me_long_v2_inverse_short_v3 import protocol as v3p

        assert v3p.protocol_fingerprint() == (
            "c10eb00f2777c2ca9b42d465ccbad4dc7908d86676b678fddcff5722f6f80aa4"
        )
        assert v3p.protocol_fingerprint() != v2p.protocol_fingerprint()

    def test_guard_parameters_are_in_the_frozen_spec(self):
        from app.paper.me_long_v2_inverse_short_v3.protocol import (
            execution_spec,
        )

        spec = execution_spec()
        assert spec["max_open_positions_total"] == 12
        assert spec["single_active_symbol"] is True
        assert spec["symbol_entry_cooldown_hours"] == 12
        assert spec["cross_contour_scope"] == "V2+V3"

    def test_sizing_and_risk_geometry_unchanged(self):
        """Issue #285: paper sizing and SHORT geometry must not drift."""
        from app.paper.me_long_v2_inverse_short_v2 import protocol as v2p
        from app.paper.me_long_v2_inverse_short_v3 import protocol as v3p

        assert v3p.TAKE_PROFIT_PCT == 0.10
        assert v3p.STOP_LOSS_PCT == 0.10
        assert v3p.MAX_HOLD_MINUTES == 7200
        assert v3p.ENTRY_MAX_AGE_MINUTES == 120
        assert v3p.FEE_PER_SIDE == 0.00055
        assert v3p.SLIPPAGE_PER_SIDE == 0.0005
        assert v3p.INTRABAR_POLICY == "STOP_FIRST"
        assert tuple(v3p.EXIT_PRIORITY) == ("STOP_LOSS", "TAKE_PROFIT_1", "TIMEOUT")
        # Geometry parity with the frozen V2 protocol, evaluated point-wise.
        for price in (0.5, 1.0, 100.0, 250_000.0):
            assert v3p.short_geometry(price) == v2p.short_geometry(price)


# ---------------------------------------------------------------------------
# 3. V2 / OOS-2 read-only + fail-closed from the V3 code
# ---------------------------------------------------------------------------
class TestCrossContourReadOnly:
    def test_store_never_writes_v2_tables(self):
        source = (
            REPO_ROOT / "app" / "paper" / "me_long_v2_inverse_short_v3"
            / "store.py"
        ).read_text(encoding="utf-8")
        for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER "):
            for line in source.splitlines():
                if verb in line and "me_long_v2_inverse_short_v2_" in line:
                    pytest.fail(
                        f"V3 store must only READ the V2 tables, found: "
                        f"{line.strip()!r}"
                    )

    def test_v2_readonly_sql_is_select_only(self):
        from app.paper.me_long_v2_inverse_short_v3 import store as v3store

        assert "SELECT" in v3store.V2_STATUS_FOR_ADMISSION_SQL
        assert "SELECT" in v3store.CROSS_CONTOUR_STATE_SQL
        for sql in (
            v3store.V2_STATUS_FOR_ADMISSION_SQL,
            v3store.CROSS_CONTOUR_STATE_SQL,
            v3store.CROSS_CONTOUR_SYMBOLS_SQL,
        ):
            assert not re.search(
                r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE)\b",
                sql,
                re.I,
            ), f"V2 read must be SELECT-only: {sql}"

    def test_v2_status_gate_is_readonly_reference(self):
        from app.paper.me_long_v2_inverse_short_v3 import store as v3store

        assert v3store.V2_POSITION_TABLE == (
            "dds.me_long_v2_inverse_short_v2_position")
        assert v3store.V2_ACTIVATION_TABLE == (
            "dds.me_long_v2_inverse_short_v2_activation")
        assert v3store.V2_NO_NEW_ADMISSION_STATUSES == ("DRAINING", "CANCELLED")


# ---------------------------------------------------------------------------
# 4. V3 source capture: wired and independent
# ---------------------------------------------------------------------------
class TestV3SourceCaptureWiring:
    def test_source_path_audit_passes(self):
        from app.paper.me_long_v2_inverse_short_v3.source_path import (
            audit_source_path,
        )

        audit = audit_source_path(REPO_ROOT)
        assert audit["wired"] is True, audit

    def test_scanner_runner_has_the_v3_hook_and_wiring(self):
        source = (REPO_ROOT / "scanner_runner.py").read_text(encoding="utf-8")
        assert "def _capture_me_inverse_short_v3_source(" in source
        assert "def me_inverse_short_v3_source_wiring(" in source
        assert "_capture_me_inverse_short_v3_source(" in source
        assert "ME_INVERSE_SHORT_V3_SOURCE_CAPTURE_ERROR" in source

    def test_flags_are_independent(self):
        from app.paper.me_long_v2_inverse_short_oos2_v1.activation import (
            ACTIVATION_FLAG as OOS2_FLAG,
        )
        from app.paper.me_long_v2_inverse_short_v2.activation import (
            ACTIVATION_FLAG as V2_FLAG,
        )
        from app.paper.me_long_v2_inverse_short_v3.activation import (
            ACTIVATION_FLAG as V3_FLAG,
        )

        assert V3_FLAG == "ME_INVERSE_SHORT_V3_ENABLED"
        assert len({V2_FLAG, OOS2_FLAG, V3_FLAG}) == 3

    def test_event_disposition_model(self):
        from app.paper.me_long_v2_inverse_short_v3.models import (
            EVENT_STATUS_REASONS,
            EVENT_STATUS_SYMBOL_REJECTED,
        )

        assert EVENT_STATUS_SYMBOL_REJECTED == "SYMBOL_REJECTED"
        assert set(EVENT_STATUS_REASONS) == {
            "ENTRY_TTL", "ENTRY_CAP", "SYMBOL_ALREADY_OPEN",
            "SYMBOL_COOLDOWN_12H",
        }


# ---------------------------------------------------------------------------
# 5. Production runtime / runner guard-config wiring
# ---------------------------------------------------------------------------
class TestRuntimeGuardWiring:
    def test_production_runtime_validates_guard_config_at_startup(self):
        source = (
            REPO_ROOT / "app" / "paper" / "me_long_v2_inverse_short_v3"
            / "production_runtime.py"
        ).read_text(encoding="utf-8")
        assert "load_v3_guard_config()" in source
        assert "ME_INVERSE_SHORT_V3_ENABLED" in source

    def test_v3_package_has_no_v2_identity(self):
        """Every V3 module must speak V3 identity, never V2 identity."""
        package = REPO_ROOT / "app" / "paper" / "me_long_v2_inverse_short_v3"
        for path in package.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert 'experiment_id="ME_LONG_V2_INVERSE_SHORT_V2"' not in source
            assert 'STRATEGY_ID = "ME_LONG_V2_INVERSE_SHORT_V2"' not in source
            assert "ME_INVERSE_SHORT_V2_ENABLED" not in source
