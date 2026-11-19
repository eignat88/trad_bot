"""Grafana guard-panel contract for the OOS-2 v2 contour (Issue #287).

Validates the Issue #287 work-package-5 additions to
``monitoring/grafana/dashboards/me-inverse-short-oos-2.json``:

  * the FROZEN OOS-2 v1 panel set (ids 1..10) is semantically byte-stable —
    pinned by canonical-JSON SHA-256 (any v1 panel edit fails here);
  * the appended guard panels (ids 11..18) carry exactly the mandated
    read-only metrics: OPEN logical X/12 over the OOS-2 FAMILY (v1+v2),
    per-symbol OPEN occupancy, last accepted ``entered_at`` + 12h cooldown
    remaining / next eligible, ``SYMBOL_ALREADY_OPEN`` /
    ``SYMBOL_COOLDOWN_12H`` terminal refusals, ``ENTRY_CAP`` pending, TTL
    expirations, rejected reasons, TP stage / outstanding qty, activation /
    fingerprint / freeze, close-outbox / funding / recovery, failures;
  * every guard query is FAMILY-scoped (legacy v1 + this contour) and never
    touches the OTHER contours (plain V1/V2/V3) or production/research data;
  * TP legs are never counted as separate trades (position rows only);
  * the real panel SQL executes on PostgreSQL (when TEST_DB_* is provided).

JSON structure is checked directly (no Grafana instance in CI).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = (
    ROOT / "monitoring" / "grafana" / "dashboards"
    / "me-inverse-short-oos-2.json"
)

# Canonical-JSON (sorted keys, compact separators, UTF-8) SHA-256 of the
# frozen v1 panel list (panels with id 1..10, in order).  Any edit to a
# frozen v1 panel changes this hash and fails test_frozen_v1_panels.
FROZEN_V1_PANELS_SHA256 = (
    "de4bdf162500d2ad1cf103bd07dbb31d8a3592cb97808c51dd259a4f63a75d6a"
)

GUARD_PANEL_TITLES = [
    "Guard: OPEN logical X/12 (family v1+v2)",
    "Guard: Occupied symbols (SINGLE_ACTIVE_SYMBOL)",
    "Guard: Last accepted entry / 12h cooldown (family)",
    "Guard: Refusals & pending (SYMBOL_*, ENTRY_CAP, TTL)",
    "Guard: TP stage / outstanding qty (family, position rows only)",
    "Activation / Freeze / Fingerprint (family)",
    "Outbox / funding / recovery (family)",
    "Capture errors / failures (family)",
]

FORBIDDEN_TABLE_TOKENS = (
    "me_long_v2_inverse_short_v1",
    "me_long_v2_inverse_short_v2",
    "me_long_v2_inverse_short_v3",
    "me_long_v2_inverse_short_oos",   # stripped below: OOS-2 family is fine
    "dds.paper_trade",
    "dds.paper_shadow_trade",
    "dds.scanner_setup",
    "research.",
)


def _load() -> dict:
    assert DASHBOARD_PATH.is_file(), f"missing dashboard: {DASHBOARD_PATH}"
    return json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))


def _guard_panels(dashboard: dict) -> list[dict]:
    return dashboard["panels"][10:]


def _guard_sql(panels: list[dict]) -> str:
    return "\n".join(
        t["rawSql"] for p in panels for t in p.get("targets", [])
    )


# ---------------------------------------------------------------------------
# 1. Frozen v1 panels stay byte-stable
# ---------------------------------------------------------------------------


class TestFrozenV1Panels:
    def test_frozen_v1_panels(self):
        panels = _load()["panels"]
        assert [p["id"] for p in panels[:10]] == list(range(1, 11))
        blob = json.dumps(
            panels[:10], sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        )
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        assert digest == FROZEN_V1_PANELS_SHA256, (
            "a frozen OOS-2 v1 panel changed — v1 monitoring is frozen; "
            "extend the dashboard with NEW panels instead"
        )


# ---------------------------------------------------------------------------
# 2. Guard panel set
# ---------------------------------------------------------------------------


class TestGuardPanels:
    def test_guard_panel_titles_ids_and_datasource(self):
        dash = _load()
        panels = dash["panels"]
        assert len(panels) == 18
        guard = _guard_panels(dash)
        assert [p["title"] for p in guard] == GUARD_PANEL_TITLES
        assert [p["id"] for p in guard] == list(range(11, 19))
        for panel in guard:
            assert panel["datasource"]["uid"] == "trad_bot_postgres", panel
            for target in panel.get("targets", []):
                assert target["datasource"]["uid"] == "trad_bot_postgres"
                assert target["rawSql"].strip()

    def test_capacity_panel_is_family_open_x_of_12(self):
        panel = next(
            p for p in _load()["panels"]
            if p["title"] == "Guard: OPEN logical X/12 (family v1+v2)"
        )
        sql = panel["targets"][0]["rawSql"]
        # family scope: BOTH contours' OPEN logical positions
        assert "me_long_v2_inverse_short_oos2_v1_position" in sql
        assert "me_long_v2_inverse_short_oos2_v2_position" in sql
        assert sql.count("status='OPEN'") == 2
        assert "12 AS cap" in sql
        assert panel["fieldConfig"]["defaults"]["max"] == 12

    def test_occupied_symbols_panel_is_per_symbol_open_only(self):
        panel = next(
            p for p in _load()["panels"]
            if p["title"].startswith("Guard: Occupied symbols")
        )
        sql = panel["targets"][0]["rawSql"]
        assert "me_long_v2_inverse_short_oos2_v1_position" in sql
        assert "me_long_v2_inverse_short_oos2_v2_position" in sql
        assert sql.count("status='OPEN'") == 2
        assert "symbol" in sql
        # one row per OPEN position per contour: TP legs never appear
        assert "close_leg" not in sql

    def test_cooldown_panel_boundary_matches_the_guard_contract(self):
        panel = next(
            p for p in _load()["panels"]
            if p["title"].startswith("Guard: Last accepted entry")
        )
        sql = panel["targets"][0]["rawSql"]
        # ACCEPTED entries only: entered_at lives on the position row
        # (refused/deferred signals never write one)
        assert "me_long_v2_inverse_short_oos2_v1_position" in sql
        assert "me_long_v2_inverse_short_oos2_v2_position" in sql
        assert "max(entered_at)" in sql
        # the SAME 12h cooldown boundary as the admission guard
        assert "interval '12 hours'" in sql
        assert "next_eligible_at" in sql
        assert "cooldown_remaining" in sql
        # cooldown cools ANY status (CLOSED keeps cooling its symbol), so
        # the per-symbol max(entered_at) must span OPEN + CLOSED rows —
        # the query never filters on position status
        assert "status" not in sql

    def test_refusal_panel_counts_terminal_refusals_and_pending(self):
        panel = next(
            p for p in _load()["panels"]
            if p["title"].startswith("Guard: Refusals & pending")
        )
        sql = panel["targets"][0]["rawSql"]
        for token in (
            "SYMBOL_ALREADY_OPEN",
            "SYMBOL_COOLDOWN_12H",
            "ENTRY_CAP",
            "ENTRY_TTL",
            "CAPACITY_REJECTED",
        ):
            assert token in sql, token
        # terminal symbol refusals are the five-state disposition
        assert "status='SYMBOL_REJECTED'" in sql
        # ENTRY_CAP stays pending while the event is still CAPTURED
        assert "status='CAPTURED'" in sql
        assert "status='EXPIRED'" in sql
        # family scope
        assert "me_long_v2_inverse_short_oos2_v1_event" in sql
        assert "me_long_v2_inverse_short_oos2_v2_event" in sql

    def test_tp_stage_panel_never_counts_legs_as_trades(self):
        panel = next(
            p for p in _load()["panels"]
            if p["title"].startswith("Guard: TP stage")
        )
        sql = panel["targets"][0]["rawSql"]
        assert "me_long_v2_inverse_short_oos2_v1_position" in sql
        assert "me_long_v2_inverse_short_oos2_v2_position" in sql
        # position rows only — TP legs are never separate trades
        assert "close_leg" not in sql
        for token in ("position_size", "remaining_size",
                      "tp1_executed", "tp2_executed", "tp3_executed"):
            assert token in sql, token

    def test_activation_panel_reports_both_contours_live(self):
        panel = next(
            p for p in _load()["panels"]
            if p["title"] == "Activation / Freeze / Fingerprint (family)"
        )
        sql = panel["targets"][0]["rawSql"]
        assert "me_long_v2_inverse_short_oos2_v1_activation" in sql
        assert "me_long_v2_inverse_short_oos2_v2_activation" in sql
        for token in ("status", "freeze_ts", "protocol_fingerprint"):
            assert token in sql, token

    def test_outbox_funding_recovery_panel_covers_both_ledgers(self):
        panel = next(
            p for p in _load()["panels"]
            if p["title"].startswith("Outbox / funding / recovery")
        )
        sql = panel["targets"][0]["rawSql"]
        for suffix in ("close_outbox", "funding_obligation"):
            for prefix in ("v1_", "v2_"):
                assert (
                    f"me_long_v2_inverse_short_oos2_{prefix}{suffix}" in sql
                ), (prefix, suffix)
        assert "settled_at IS NULL" in sql      # unsettled obligations

    def test_failures_panel_counts_both_family_capture_error_types(self):
        panel = next(
            p for p in _load()["panels"]
            if p["title"].startswith("Capture errors / failures")
        )
        sql = panel["targets"][0]["rawSql"]
        assert "dds.scanner_error" in sql
        assert "ME_INVERSE_SHORT_OOS2_SOURCE_CAPTURE_ERROR" in sql
        assert "ME_INVERSE_SHORT_OOS2_V2_SOURCE_CAPTURE_ERROR" in sql


# ---------------------------------------------------------------------------
# 3. Isolation: family scope only, no other contour, no production data
# ---------------------------------------------------------------------------


class TestGuardPanelIsolation:
    def test_guard_queries_touch_only_the_oos2_family(self):
        sql = _guard_sql(_guard_panels(_load()))
        assert "me_long_v2_inverse_short_oos2_v1_" in sql
        assert "me_long_v2_inverse_short_oos2_v2_" in sql
        for token in FORBIDDEN_TABLE_TOKENS:
            if token == "me_long_v2_inverse_short_oos":
                # allowed only as the OOS-2 family namespace
                stripped = sql.replace("me_long_v2_inverse_short_oos2_", "")
                assert token not in stripped.lower(), token
                continue
            assert token not in sql.lower(), token

    def test_v1_namespace_only_in_family_scoped_union_sides(self):
        # every guard query that touches the legacy v1 tables is a family
        # aggregation — a v1-only query would hide v2 guard state
        for panel in _guard_panels(_load()):
            for target in panel.get("targets", []):
                sql = target["rawSql"]
                if "dds.scanner_error" in sql:
                    assert "ME_INVERSE_SHORT_OOS2_V2_SOURCE_CAPTURE_ERROR" in sql
                    continue
                assert "me_long_v2_inverse_short_oos2_v2_" in sql, sql

    def test_no_pinned_v2_fingerprint_in_dashboard_text(self):
        import importlib
        protocol = importlib.import_module(
            "app.paper.me_long_v2_inverse_short_oos2_v2.protocol"
        )
        text = DASHBOARD_PATH.read_text(encoding="utf-8")
        # the activation panel reads the fingerprint LIVE from the DB; a
        # pinned copy in the JSON could silently drift from the code
        assert protocol.protocol_fingerprint() not in text


# ---------------------------------------------------------------------------
# 4. REAL PostgreSQL execution of the guard panel SQL
# ---------------------------------------------------------------------------

_PG_ENV = ("TEST_DB_HOST", "TEST_DB_PORT", "TEST_DB_NAME", "TEST_DB_USER")
_PG_ONLY = pytest.mark.skipif(
    not all(os.environ.get(key) for key in _PG_ENV),
    reason="TEST_DB_* not set — real-PostgreSQL panel execution disabled",
)

# The panel SQL must run against a FRESH TEST database exactly like it will
# against the production schema, so the fixture applies the OOS-2 family
# migrations itself.  088 is guarded (its CREATE TRIGGER is not idempotent,
# same pattern as the pg_admission IT suite); 089/091 are idempotent and run
# always.  CI starts from an empty database.
_SCHEMA_MIGRATIONS = (
    "089_me_long_v2_inverse_short_oos2_v1_funding_obligation.sql",
    "091_me_long_v2_inverse_short_oos2_v2.sql",
)
_MIGRATION_088 = "088_me_long_v2_inverse_short_oos2_v1.sql"


def _ensure_oos2_schema(conn) -> None:
    cursor = conn.cursor()
    cursor.execute("CREATE SCHEMA IF NOT EXISTS dds")
    cursor.execute(
        "SELECT to_regclass('dds.me_long_v2_inverse_short_oos2_v1_position')"
    )
    migrations = ROOT / "sql" / "migrations"
    if cursor.fetchone()[0] is None:
        cursor.execute(
            (migrations / _MIGRATION_088).read_text(encoding="utf-8")
        )
    for name in _SCHEMA_MIGRATIONS:
        cursor.execute((migrations / name).read_text(encoding="utf-8"))
    cursor.close()


@pytest.fixture()
def pg_conn():
    import pg8000
    conn = pg8000.connect(
        host=os.environ["TEST_DB_HOST"],
        port=int(os.environ.get("TEST_DB_PORT", "5432")),
        database=os.environ["TEST_DB_NAME"],
        user=os.environ["TEST_DB_USER"],
        password=os.environ.get("TEST_DB_PASSWORD", ""),
    )
    conn.autocommit = True
    _ensure_oos2_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


@_PG_ONLY
class TestGuardPanelSqlOnPostgres:
    def test_every_guard_query_executes(self, pg_conn):
        cursor = pg_conn.cursor()
        failures: list[tuple[str, str]] = []
        for panel in _guard_panels(_load()):
            for target in panel.get("targets", []):
                sql = target["rawSql"]
                if "dds.scanner_error" in sql:
                    # the shared error ledger is created by an earlier
                    # production migration; scratch test DBs may not carry it
                    cursor.execute(
                        "SELECT to_regclass('dds.scanner_error') IS NOT NULL"
                    )
                    if not cursor.fetchone()[0]:
                        continue
                try:
                    cursor.execute(sql)
                    cursor.fetchall()
                except Exception as exc:  # noqa: BLE001 - report every failure
                    failures.append((panel["title"], str(exc)))
        cursor.close()
        assert not failures, failures

    def test_family_cap_panel_sees_both_contours(self, pg_conn):
        panel = next(
            p for p in _load()["panels"]
            if p["title"] == "Guard: OPEN logical X/12 (family v1+v2)"
        )
        cursor = pg_conn.cursor()
        cursor.execute(panel["targets"][0]["rawSql"])
        row = cursor.fetchone()
        cursor.close()
        assert row[1] == 12
        assert row[0] >= 0

    def test_cooldown_panel_uses_db_time(self, pg_conn):
        panel = next(
            p for p in _load()["panels"]
            if p["title"].startswith("Guard: Last accepted entry")
        )
        cursor = pg_conn.cursor()
        cursor.execute(panel["targets"][0]["rawSql"])
        rows = cursor.fetchall()
        cursor.close()
        for row in rows:
            # next_eligible = last accepted + exactly 12h
            assert row[2] - row[1] == __import__("datetime").timedelta(hours=12)
