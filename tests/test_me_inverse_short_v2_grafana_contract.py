"""Offline contract tests for read-only ME Inverse SHORT V2 Grafana dashboard."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

PATH = (
    Path(__file__).resolve().parents[1]
    / "monitoring/grafana/dashboards/me-inverse-short-v2-oos.json"
)
DATA_SOURCE = "trad_bot_postgres"
TABLE_PREFIX = "dds.me_long_v2_inverse_short_v2_"
ALLOWED = {
    TABLE_PREFIX + "activation",
    TABLE_PREFIX + "event",
    TABLE_PREFIX + "position",
}
EXPECTED = {
    "Frozen V2 Protocol and Prospective Boundary",
    "Prospective Integrity",
    "New Source Events / Hour",
    "Cumulative Realized Net PnL (selected range)",
    "Open SHORT Positions — Details",
    "Source Event Disposition Summary",
    "Terminal Entry Refusal Reasons",
    "Win Rate — CLOSED (%)",
    "Profit Factor — CLOSED",
    "Realized Net PnL by Symbol — CLOSED",
}


class DashboardContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dashboard = json.loads(PATH.read_text(encoding="utf-8"))

    def test_existing_uid_and_datasource_are_stable(self) -> None:
        d = self.dashboard
        self.assertEqual(d["uid"], "me-inverse-short-v2-oos")
        self.assertEqual(d["title"], "Trad Bot — ME Inverse SHORT V2 OOS")
        self.assertEqual(d["refresh"], "30s")
        self.assertEqual({p["id"] for p in d["panels"][:12]}, set(range(1, 13)))
        self.assertTrue(EXPECTED.issubset({p["title"] for p in d["panels"]}))
        for p in d["panels"]:
            self.assertEqual(p["datasource"]["uid"], DATA_SOURCE)
            for t in p["targets"]:
                self.assertEqual(t["datasource"]["uid"], DATA_SOURCE)

    def test_unique_panels_no_overlaps(self) -> None:
        seen_ids = set()
        taken = set()
        for p in self.dashboard["panels"]:
            self.assertNotIn(p["id"], seen_ids)
            seen_ids.add(p["id"])
            g = p["gridPos"]
            self.assertGreater(g["w"], 0)
            self.assertGreater(g["h"], 0)
            self.assertLessEqual(g["x"] + g["w"], 24)
            for x in range(g["x"], g["x"] + g["w"]):
                for y in range(g["y"], g["y"] + g["h"]):
                    self.assertNotIn((x, y), taken, p["title"])
                    taken.add((x, y))

    def test_all_queries_read_only_and_isolated(self) -> None:
        for p in self.dashboard["panels"]:
            for t in p["targets"]:
                sql = t["rawSql"].strip()
                self.assertRegex(sql.upper(), r"^(SELECT|WITH)\b", p["title"])
                self.assertNotRegex(
                    sql.upper(),
                    r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|TRUNCATE|COPY|DO|CALL|GRANT|REVOKE)\b",
                )
                tables = set(
                    re.findall(
                        r"\bdds\.[a-zA-Z_][a-zA-Z0-9_]*\b",
                        sql,
                        flags=re.IGNORECASE,
                    )
                )
                self.assertTrue(tables, p["title"])
                self.assertTrue(tables.issubset(ALLOWED), (p["title"], tables))
                self.assertNotIn("research.", sql.lower())

    def test_time_series_and_empty_cohort_contract(self) -> None:
        titles = {p["title"]: p for p in self.dashboard["panels"]}
        for name in ("New Source Events / Hour", "Cumulative Realized Net PnL (selected range)"):
            panel = titles[name]
            self.assertEqual(panel["type"], "timeseries")
            self.assertEqual(panel["targets"][0]["format"], "time_series")
            self.assertIn("$__timeFilter(", panel["targets"][0]["rawSql"])
        self.assertIn(
            "THEN NULL",
            titles["Win Rate — CLOSED (%)"]["targets"][0]["rawSql"],
        )
        self.assertIn(
            "THEN NULL",
            titles["Profit Factor — CLOSED"]["targets"][0]["rawSql"],
        )


if __name__ == "__main__":
    unittest.main()
