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
"""
from __future__ import annotations

import json
from pathlib import Path

from app.paper.me_long_v2_inverse_short_oos2_v1.protocol import (
    protocol_fingerprint,
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
