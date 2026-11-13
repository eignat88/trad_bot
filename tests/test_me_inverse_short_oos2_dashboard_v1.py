"""Grafana dashboard contract for the OOS-2 contour (Issue #276).

Validates monitoring/grafana/dashboards/me-inverse-short-oos-2.json:
  * identity: uid ``me-inverse-short-oos-2``, title
    ``Trad Bot — ME Inverse SHORT OOS-2``, datasource ``trad_bot_postgres``;
  * all 10 required panels exist (Activation/Freeze/Fingerprint; Open
    Positions of 12; Open SHORT table; TP Progress; Partial TP Executions;
    Closed Positions; Realized/Unrealized Net PnL; Equity Curve;
    Wins/Losses by symbol; Source Capture/Expired/Capacity Rejected/Errors);
  * every SQL target references ONLY this contour's tables (plus the
    scanner error sink) — never a V1/V2/production table;
  * partial TP legs are NEVER counted as separate closed trades: the
    partial-leg panel filters ``NOT is_terminal`` and the closed-trade
    panels count position rows;
  * the dashboard description embeds the CURRENT protocol fingerprint.

JSON structure is checked directly (no Grafana instance in CI).

Round 6 (Codex r4555636165): ``TestDashboardSqlOnPostgres`` additionally
executes the REAL panel ``rawSql`` on PostgreSQL 17 against engine-built
snapshots for every required scenario (OPEN pre-TP / after funding / after
TP1 / after TP1+TP2 / after several funding periods / CLOSED by TP3, STOP,
TIMEOUT / ambiguous terminal COMMIT / UNKNOWN or unresolved funding) and
compares the Equity PnL row with the engine's own financial result on the
SAME snapshot — field-presence checks alone are not enough.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.paper.me_long_v2_inverse_short_oos2_v1.protocol import (
    protocol_fingerprint,
)
from test_me_long_v2_inverse_short_oos2_pg_admission_it_v1 import (
    _PG_ONLY,
    _Clock,
    _capture_event,
    _connect_store,
    _contour,
    pg_conn,  # noqa: F401 - fixture: real PostgreSQL scenario snapshots
    running,  # noqa: F401 - fixture: activation RUNNING + pinned fingerprint
)

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = (
    ROOT / "monitoring" / "grafana" / "dashboards"
    / "me-inverse-short-oos-2.json"
)

REQUIRED_PANELS = {
    "Activation / Freeze / Fingerprint",
    "Open Positions (of 12)",
    "Realized / Unrealized Net PnL (USDT)",
    "Open SHORT Positions",
    "TP Progress",
    "Partial TP Executions (legs, NOT separate trades)",
    "Closed Positions (full trades)",
    "Wins / Losses by symbol (closed positions)",
    "Equity Curve (realized PnL over time)",
    "Source Capture / Expired / Capacity Rejected / Errors",
}

TABLE_PREFIX = "dds.me_long_v2_inverse_short_oos2_v1_"
FORBIDDEN_TABLE_TOKENS = (
    "me_long_v2_inverse_short_v1",
    "me_long_v2_inverse_short_v2",
    "dds.paper_trade",
    "dds.paper_shadow_trade",
    "dds.scanner_setup",
    "research.",
)


def _load() -> dict:
    assert DASHBOARD_PATH.is_file(), f"missing dashboard: {DASHBOARD_PATH}"
    return json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))


def _queries(dashboard: dict) -> list[tuple[int, str, str]]:
    """(panel_id, panel_title, rawSql) for every target of every panel."""
    out: list[tuple[int, str, str]] = []
    for panel in dashboard.get("panels", []):
        for target in panel.get("targets", []):
            sql = target.get("rawSql", "")
            if sql:
                out.append((panel["id"], panel["title"], sql))
    return out


class TestDashboardIdentity:
    def test_file_uid_title_and_datasource(self):
        dashboard = _load()
        assert dashboard["uid"] == "me-inverse-short-oos-2"
        assert dashboard["title"] == "Trad Bot — ME Inverse SHORT OOS-2"
        assert dashboard["timezone"] == "utc"

    def test_all_panels_use_the_trad_bot_postgres_datasource(self):
        dashboard = _load()
        assert dashboard["panels"], "dashboard has no panels"
        for panel in dashboard["panels"]:
            ds = panel.get("datasource", {})
            assert ds.get("uid") == "trad_bot_postgres", panel["title"]
            for target in panel.get("targets", []):
                assert target["datasource"]["uid"] == "trad_bot_postgres", (
                    panel["title"],
                )

    def test_all_ten_required_panels_present(self):
        titles = {p["title"] for p in _load()["panels"]}
        missing = REQUIRED_PANELS - titles
        assert not missing, f"missing required panels: {sorted(missing)}"

    def test_description_embeds_the_current_fingerprint(self):
        description = _load().get("description", "")
        assert protocol_fingerprint() in description


class TestDashboardQueries:
    def test_every_query_targets_oos2_tables_only(self):
        for panel_id, title, sql in _queries(_load()):
            lowered = sql.lower()
            is_error_sink = (
                "dds.scanner_error" in lowered
                and "me_inverse_short_oos2_source_capture_error" in lowered
            )
            assert TABLE_PREFIX in lowered or is_error_sink, (
                panel_id, title, sql,
            )
            for token in FORBIDDEN_TABLE_TOKENS:
                assert token not in lowered, (panel_id, title, token)

    def test_capacity_panel_is_of_12(self):
        dash = _load()
        panel = next(
            p for p in dash["panels"]
            if p["title"] == "Open Positions (of 12)"
        )
        sql = panel["targets"][0]["rawSql"]
        assert "status='OPEN'" in sql
        assert panel["fieldConfig"]["defaults"].get("max") == 12

    def test_activation_panel_reports_status_freeze_and_fingerprint(self):
        dash = _load()
        panel = next(
            p for p in dash["panels"]
            if p["title"] == "Activation / Freeze / Fingerprint"
        )
        sql = panel["targets"][0]["rawSql"].lower()
        assert "freeze_ts" in sql
        assert "protocol_fingerprint" in sql
        assert "status" in sql

    def test_partial_tp_panel_counts_legs_never_trades(self):
        dash = _load()
        panel = next(
            p for p in dash["panels"]
            if p["title"].startswith("Partial TP Executions")
        )
        sql = panel["targets"][0]["rawSql"]
        assert "close_leg" in sql
        assert "NOT l.is_terminal" in sql
        # never the position table's CLOSED rows
        assert "status='CLOSED'" not in sql

    def test_closed_positions_panel_counts_position_rows(self):
        dash = _load()
        panel = next(
            p for p in dash["panels"]
            if p["title"].startswith("Closed Positions")
        )
        sql = panel["targets"][0]["rawSql"]
        assert "position" in sql
        assert "status='CLOSED'" in sql
        assert "close_leg" not in sql

    def test_wins_losses_group_closed_positions_not_legs(self):
        dash = _load()
        panel = next(
            p for p in dash["panels"]
            if p["title"].startswith("Wins / Losses")
        )
        sql = panel["targets"][0]["rawSql"]
        assert "status='CLOSED'" in sql
        assert "group by symbol" in sql.lower()
        assert "close_leg" not in sql

    def test_pnl_panel_has_realized_and_unrealized(self):
        dash = _load()
        panel = next(
            p for p in dash["panels"]
            if p["title"].startswith("Realized / Unrealized")
        )
        sql = panel["targets"][0]["rawSql"].lower()
        assert "realized_legs_net" in sql          # committed partial TP legs
        assert "last_mark_price" in sql            # unrealized from the mark
        assert "remaining_size" in sql

    def test_equity_curve_is_a_single_cumulative_series(self):
        dash = _load()
        panel = next(
            p for p in dash["panels"] if p["title"].startswith("Equity Curve")
        )
        target = panel["targets"][0]
        assert target["format"] == "time_series"
        sql = target["rawSql"]
        assert "sum(delta) over" in sql.lower()
        # no double counting: partial legs only while their position is OPEN
        assert "p.status = 'OPEN'" in sql

    def test_source_pipeline_panel_covers_all_four_counters(self):
        dash = _load()
        panel = next(
            p for p in dash["panels"]
            if p["title"].startswith("Source Capture")
        )
        sqls = [t["rawSql"] for t in panel["targets"]]
        joined = "\n".join(sqls)
        assert "status='CAPTURED'" in joined
        assert "status='ENTERED'" in joined
        assert "status='EXPIRED'" in joined
        assert "status='CAPACITY_REJECTED'" in joined
        assert "ME_INVERSE_SHORT_OOS2_SOURCE_CAPTURE_ERROR" in joined
        assert len(sqls) == 5


# ---------------------------------------------------------------------------
# Round 6 (Codex r4555636165) — REAL PostgreSQL execution of the panel SQL
# against engine-built snapshots: on the SAME snapshot the dashboard's
# Equity PnL row must equal the engine's financial result (tolerance:
# the SQL rounds to 4 decimals).
# ---------------------------------------------------------------------------

def _pnl_panel_sql() -> str:
    """The REAL rawSql of the \"Realized / Unrealized Net PnL\" panel."""
    dashboard = _load()
    panel = next(
        p for p in dashboard["panels"]
        if p["title"] == "Realized / Unrealized Net PnL (USDT)"
    )
    return panel["targets"][0]["rawSql"]


def _dash_metrics(conn) -> dict[str, float]:
    """Execute the panel SQL on PostgreSQL (live SQL, not a field check)."""
    cursor = conn.cursor()
    cursor.execute(_pnl_panel_sql())
    rows = cursor.fetchall()
    conn.rollback()
    return {str(metric): float(value) for metric, value in rows}


def _metric(metrics: dict[str, float], prefix: str) -> float:
    return next(v for k, v in metrics.items() if k.startswith(prefix))


def _engine_equity_pnl(contour, prices) -> float:
    """The engine's own equity result for the SAME snapshot."""
    account = contour.engine.mark_to_market(prices)
    return float(account["equity"]) - float(contour.engine.initial_balance)


def _open_scenario(candle_steps: int = 0):
    """Fresh store + contour + one ENTERED position at reference 100."""
    store = _connect_store()
    clock = _Clock(datetime.now(timezone.utc))
    contour = _contour(store, clock=clock)
    event = _capture_event(
        store, age=timedelta(seconds=5), candle_steps=candle_steps,
    )
    contour.run_cycle({"BTCUSDT": 100.0})
    position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event}"
    row = store.get_position(position_key)
    assert row is not None, "the entry must be durable on PostgreSQL"
    return store, clock, contour, position_key


def _open_equity(row, account) -> float:
    """Engine-equivalent equity for an OPEN row: realized legs − full
    entry costs − settled funding + market-reference unrealized."""
    return (
        row.realized_legs_net
        - row.entry_fee
        - row.entry_slippage_cost
        - row.funding_paid
        + account["unrealized_pnl"]
    )


@_PG_ONLY
class TestDashboardSqlOnPostgres:
    """Scenario suite: dashboard SQL vs engine, one snapshot each."""

    # -- OPEN scenarios ------------------------------------------------

    def test_open_pre_tp_flat_price(self, pg_conn, running):
        """OPEN, mark == entry: entry costs (full quantity) + funding
        (0) + unrealized — dashboard == engine."""
        store, clock, contour, key = _open_scenario()
        try:
            contour.run_cycle({"BTCUSDT": 100.0})   # flat mark
            row = store.get_position(key)
            account = contour.engine.mark_to_market({"BTCUSDT": 100.0})
            metrics = _dash_metrics(pg_conn)
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _engine_equity_pnl(contour, {"BTCUSDT": 100.0}), abs=5e-4,
            )
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _open_equity(row, account), abs=5e-4,
            )
            assert _metric(metrics, "Realized") == pytest.approx(
                row.realized_legs_net, abs=5e-4,
            )
            assert _metric(metrics, "Unrealized") == pytest.approx(
                account["unrealized_pnl"], abs=5e-4,
            )
            assert _metric(metrics, "FLAG: unsettled") == 0
            assert _metric(metrics, "FLAG: durable funding") == 0
            assert _metric(metrics, "FLAG: staged terminal") == 0
        finally:
            store.close()

    def test_open_after_8h_funding_settle_finding_repro(
        self, pg_conn, running,
    ):
        """r4555636165 repro: mark $100 unchanged, 8h, rate −0.01% — the
        dashboard equity MUST carry the settled funding (the engine did:
        its −0.575125 vs the dashboard's −0.250125 gap)."""
        store, clock, contour, key = _open_scenario()
        try:
            clock.advance(hours=8)
            contour.run_cycle(
                {"BTCUSDT": 100.0},
                funding_rates_percent={"BTCUSDT": -0.01},
            )
            row = store.get_position(key)
            assert row.status == "OPEN"
            assert row.funding_periods_charged == 1
            assert row.funding_paid != 0.0, "the observed rate settled"
            account = contour.engine.mark_to_market({"BTCUSDT": 100.0})
            metrics = _dash_metrics(pg_conn)
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _engine_equity_pnl(contour, {"BTCUSDT": 100.0}), abs=5e-4,
            )
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _open_equity(row, account), abs=5e-4,
            ), "settled funding must be inside the dashboard equity"
            # nothing unresolved: the interval is charged
            assert _metric(metrics, "FLAG: unsettled") == 0
            assert _metric(metrics, "FLAG: durable funding") == 0
        finally:
            store.close()

    def test_open_after_tp1(self, pg_conn, running):
        store, clock, contour, key = _open_scenario()
        try:
            contour.run_cycle({"BTCUSDT": 94.5})     # TP1 committed
            row = store.get_position(key)
            assert row.status == "OPEN" and row.tp1_executed
            account = contour.engine.mark_to_market({"BTCUSDT": 94.5})
            metrics = _dash_metrics(pg_conn)
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _engine_equity_pnl(contour, {"BTCUSDT": 94.5}), abs=5e-4,
            )
            # partial TP legs are NOT separate trades: Realized carries
            # exactly the committed leg net (once), never + closed trades
            assert _metric(metrics, "Realized") == pytest.approx(
                row.realized_legs_net, abs=5e-4,
            )
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _open_equity(row, account), abs=5e-4,
            )
        finally:
            store.close()

    def test_open_after_tp1_and_tp2(self, pg_conn, running):
        store, clock, contour, key = _open_scenario()
        try:
            contour.run_cycle({"BTCUSDT": 94.5})     # TP1
            contour.run_cycle({"BTCUSDT": 92.5})     # TP2
            row = store.get_position(key)
            assert row.status == "OPEN" and row.tp2_executed
            account = contour.engine.mark_to_market({"BTCUSDT": 92.5})
            metrics = _dash_metrics(pg_conn)
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _engine_equity_pnl(contour, {"BTCUSDT": 92.5}), abs=5e-4,
            )
            assert _metric(metrics, "Realized") == pytest.approx(
                row.realized_legs_net, abs=5e-4,
            )
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _open_equity(row, account), abs=5e-4,
            )
        finally:
            store.close()

    def test_open_after_tp1_then_three_funding_periods(
        self, pg_conn, running,
    ):
        """TP1 committed, +25h (3 frozen-8h intervals) settled on the
        remaining quantity — dashboard == engine, no unresolved flags."""
        store, clock, contour, key = _open_scenario()
        try:
            contour.run_cycle({"BTCUSDT": 94.5})     # TP1 committed
            clock.advance(hours=25)
            contour.run_cycle(
                {"BTCUSDT": 94.5},
                funding_rates_percent={"BTCUSDT": 0.01},
            )
            row = store.get_position(key)
            assert row.status == "OPEN"
            assert row.funding_periods_charged == 3
            assert row.funding_paid != 0.0
            account = contour.engine.mark_to_market({"BTCUSDT": 94.5})
            metrics = _dash_metrics(pg_conn)
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _engine_equity_pnl(contour, {"BTCUSDT": 94.5}), abs=5e-4,
            )
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _open_equity(row, account), abs=5e-4,
            )
            assert _metric(metrics, "FLAG: unsettled") == 0
        finally:
            store.close()

    # -- CLOSED scenarios ----------------------------------------------

    def test_closed_by_tp3(self, pg_conn, running):
        store, clock, contour, key = _open_scenario()
        try:
            contour.run_cycle({"BTCUSDT": 94.5})     # TP1
            contour.run_cycle({"BTCUSDT": 92.5})     # TP2
            contour.run_cycle({"BTCUSDT": 89.0})     # TP3 terminal
            row = store.get_position(key)
            assert row.status == "CLOSED"
            assert row.exit_reason.startswith("TAKE_PROFIT")
            assert [leg.leg_seq for leg in _legs(pg_conn, key)] == [1, 2, 3]
            metrics = _dash_metrics(pg_conn)
            # a CLOSED row counts exactly ONCE via net_pnl, no open terms
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                row.net_pnl, abs=5e-4,
            )
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _engine_equity_pnl(contour, {}), abs=5e-4,
            )
            assert _metric(metrics, "Unrealized") == 0
            assert _metric(metrics, "FLAG: unsettled") == 0
        finally:
            store.close()

    def test_closed_by_stop(self, pg_conn, running):
        store, clock, contour, key = _open_scenario()
        try:
            contour.run_cycle({"BTCUSDT": 94.5})     # TP1 committed
            row0 = store.get_position(key)
            bars = {
                "BTCUSDT": {
                    "high": row0.stop_price * 1.001,
                    "low": row0.entry_price * 0.99,
                },
            }
            cycle = contour.run_cycle({"BTCUSDT": row0.entry_price}, bars=bars)
            assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
            row = store.get_position(key)
            assert row.status == "CLOSED"
            assert [leg.leg_seq for leg in _legs(pg_conn, key)] == [1, 3]
            metrics = _dash_metrics(pg_conn)
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                row.net_pnl, abs=5e-4,
            )
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _engine_equity_pnl(contour, {}), abs=5e-4,
            )
        finally:
            store.close()

    def test_closed_by_timeout_with_full_funding(self, pg_conn, running):
        """+121h: 15 intervals settle on the full remaining quantity and
        THEN the protective close fires — one CLOSED row, exactly once."""
        store, clock, contour, key = _open_scenario()
        try:
            clock.advance(hours=121)
            cycle = contour.run_cycle(
                {"BTCUSDT": 100.0},
                funding_rates_percent={"BTCUSDT": 0.01},
            )
            assert cycle["exit_reasons"] == ["TIMEOUT"], cycle
            row = store.get_position(key)
            assert row.status == "CLOSED"
            assert row.funding_periods_charged == 15
            assert row.funding_paid != 0.0
            metrics = _dash_metrics(pg_conn)
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                row.net_pnl, abs=5e-4,
            )
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _engine_equity_pnl(contour, {}), abs=5e-4,
            )
            assert _metric(metrics, "FLAG: unsettled") == 0
            assert _metric(metrics, "FLAG: durable funding") == 0
        finally:
            store.close()

    # -- unresolved scenarios ------------------------------------------

    def test_ambiguous_terminal_commit_is_flagged(
        self, pg_conn, running,
    ):
        """Terminal COMMIT applied with a lost acknowledgement + failed
        read-back: the dashboard shows ONLY durable facts (the applied
        CLOSED row), the lingering close-outbox row raises the explicit
        FLAG, and the engine reports the unresolved write (never a silent
        confirmed claim)."""
        store, clock, contour, key = _open_scenario()
        try:
            contour.run_cycle({"BTCUSDT": 94.5})     # TP1 committed
            row0 = store.get_position(key)
            entry = row0.entry_price
            bars = {
                "BTCUSDT": {"high": row0.stop_price * 1.001,
                            "low": entry * 0.99},
            }
            original_update = store.update_position
            original_get = store.get_position

            def update_losing_ack(position):
                original_update(position)   # PostgreSQL applied it
                raise RuntimeError("terminal COMMIT ack lost")

            def read_back_down(k):
                if k == key:
                    raise RuntimeError("durable read-back unavailable")
                return original_get(k)

            store.update_position = update_losing_ack
            store.get_position = read_back_down
            cycle = contour.run_cycle({"BTCUSDT": entry}, bars=bars)
            assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
            engine = contour.engine

            # engine-side unresolved flags (rule 1-2)
            assert engine.ambiguous_write_keys == (key,)
            assert engine.accounting_consistent is False
            assert key in engine.pending_closes

            durable = original_get(key)
            assert durable.status == "CLOSED"
            metrics = _dash_metrics(pg_conn)
            # durable-only equity == the reconciled engine balance
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                durable.net_pnl, abs=5e-4,
            )
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _engine_equity_pnl(contour, {}), abs=5e-4,
            )
            # the unresolved close is EXPLICIT: outbox row still staged
            assert _metric(metrics, "FLAG: staged terminal") == 1

            # heal -> read-back classifies CONFIRMED -> outbox cleared
            store.update_position = original_update
            store.get_position = original_get
            contour.run_cycle({"BTCUSDT": entry})
            assert engine.ambiguous_write_keys == ()
            assert engine.pending_closes == {}
            metrics = _dash_metrics(pg_conn)
            assert _metric(metrics, "FLAG: staged terminal") == 0
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _engine_equity_pnl(contour, {}), abs=5e-4,
            )
        finally:
            store.close()

    def test_unknown_funding_is_flagged_never_booked(
        self, pg_conn, running,
    ):
        """Rate UNKNOWN at a protective close: nothing booked, no
        obligation invented, and the dashboard raises the unresolved-
        funding FLAG instead of pretending the PnL is final."""
        store, clock, contour, key = _open_scenario()
        try:
            clock.advance(hours=9)
            row0 = store.get_position(key)
            bars = {
                "BTCUSDT": {"high": row0.stop_price * 1.001,
                            "low": row0.entry_price * 0.99},
            }
            cycle = contour.run_cycle({"BTCUSDT": row0.entry_price}, bars=bars)
            assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
            row = store.get_position(key)
            assert row.status == "CLOSED"
            assert row.funding_periods_charged == 0
            assert row.funding_paid == 0.0, "no approximate booking"
            assert store.get_funding_obligation(key) is None, (
                "UNKNOWN rate is not an obligation (history was readable)",
            )
            assert not contour.engine.funding_obligation_keys
            metrics = _dash_metrics(pg_conn)
            # dashboard == engine (neither booked the unknown rate) ...
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                row.net_pnl, abs=5e-4,
            )
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _engine_equity_pnl(contour, {}), abs=5e-4,
            )
            # ... and the gap is flagged, never silently finalized
            assert _metric(metrics, "FLAG: unsettled") == 1
            assert _metric(metrics, "FLAG: durable funding") == 0
        finally:
            store.close()

    def test_history_unavailable_obligation_flagged_then_recovered(
        self, pg_conn, running,
    ):
        """HISTORY_UNAVAILABLE close: the observed rate lives in the
        DURABLE obligation — the dashboard flags both the unsettled
        interval and the open obligation, and does NOT count the funding
        until it is durably settled; after recovery the flag clears and
        both sides converge."""
        store, clock, contour, key = _open_scenario()
        try:
            clock.advance(hours=9)
            row0 = store.get_position(key)
            bars = {
                "BTCUSDT": {"high": row0.stop_price * 1.001,
                            "low": row0.entry_price * 0.99},
            }
            original_list = store.list_close_legs

            def history_down(k):
                raise RuntimeError("close-leg history unavailable")

            store.list_close_legs = history_down
            cycle = contour.run_cycle(
                {"BTCUSDT": row0.entry_price}, bars=bars,
                funding_rates_percent={"BTCUSDT": 0.01},
            )
            assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
            store.list_close_legs = original_list
            row = store.get_position(key)
            assert row.status == "CLOSED"
            assert row.funding_periods_charged == 0, "not settled yet"
            obligation = store.get_funding_obligation(key)
            assert obligation is not None and obligation.status == "OPEN"

            metrics = _dash_metrics(pg_conn)
            # phase 1: the observed rate is EXCLUDED from Net PnL ...
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                row.net_pnl, abs=5e-4,
            )
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _engine_equity_pnl(contour, {}), abs=5e-4,
            )
            # ... and both unresolved states are flagged explicitly
            assert _metric(metrics, "FLAG: unsettled") == 1
            assert _metric(metrics, "FLAG: durable funding") == 1

            # recovery: history back -> settle exactly once
            contour.run_cycle({"BTCUSDT": row0.entry_price})
            settled = store.get_position(key)
            assert settled.funding_periods_charged == 1
            assert settled.funding_paid != 0.0
            assert store.get_funding_obligation(key).status != "OPEN"

            metrics = _dash_metrics(pg_conn)
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                settled.net_pnl, abs=5e-4,
            )
            assert _metric(metrics, "Equity PnL") == pytest.approx(
                _engine_equity_pnl(contour, {}), abs=5e-4,
            )
            assert _metric(metrics, "FLAG: unsettled") == 0
            assert _metric(metrics, "FLAG: durable funding") == 0
            # idempotent: nothing changes on further cycles
            for _ in range(3):
                contour.run_cycle({"BTCUSDT": row0.entry_price})
            assert store.get_position(key).funding_paid == pytest.approx(
                settled.funding_paid, abs=1e-9,
            )
        finally:
            store.close()


def _legs(conn, position_key: str) -> list:
    """Close-leg row numbers read through the raw test connection."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT leg_seq FROM dds.me_long_v2_inverse_short_oos2_v1_close_leg "
        "WHERE position_key = %s ORDER BY leg_seq",
        (position_key,),
    )
    rows = [seq for (seq,) in cursor.fetchall()]
    conn.rollback()
    return [_LegRef(seq) for seq in rows]


class _LegRef:
    def __init__(self, leg_seq: int):
        self.leg_seq = leg_seq
