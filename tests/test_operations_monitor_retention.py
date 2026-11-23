"""Safe offline checks for bounded monitoring retention (no PostgreSQL calls)."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

MODULE = (Path(__file__).resolve().parents[1] /
          "monitoring/operations/retention.py")
spec = importlib.util.spec_from_file_location("operations_retention", MODULE)
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)


class RetentionTests(unittest.TestCase):
    def test_table_allowlist(self):
        self.assertEqual(
            {table for table, _ in retention.TABLES},
            {"api_metric_sec", "service_sample"})
        for table in ("journal_cursor", "source_gap_audit", "trading_orders"):
            with self.assertRaises(ValueError):
                retention.delete_sql(table, "2026-10-24T00:00:00+00:00", 100)

    def test_indexed_timestamp_columns(self):
        for name, stamp in retention.TABLES:
            sql = retention.delete_sql(name, "2026-10-24T00:00:00+00:00", 2000)
            self.assertIn(f"FROM monitoring.{name}", sql)
            self.assertIn(f"WHERE {stamp} <", sql)
            self.assertIn(f"ORDER BY {stamp} ASC", sql)

    def test_batch_is_bounded(self):
        sql = retention.delete_sql("api_metric_sec",
                                   "2026-10-24T00:00:00+00:00", 2000)
        for clause in ("LIMIT 2000", "FOR UPDATE SKIP LOCKED",
                       "lock_timeout", "statement_timeout",
                       "RETURNING 1"):
            self.assertIn(clause, sql)
        with self.assertRaises(ValueError):
            retention.delete_sql("api_metric_sec", "cutoff", 0)
        with self.assertRaises(ValueError):
            retention.delete_sql("api_metric_sec", "cutoff", 999999)

    def test_default_dry_run_never_deletes(self):
        def fake_pg(sql):
            self.assertNotIn("DELETE FROM", sql)
            return "0|"
        with mock.patch.object(retention, "pg", side_effect=fake_pg):
            self.assertEqual(retention.main([]), 0)

    def test_apply_zero_old_rows(self):
        sqls = []
        def fake_pg(sql):
            sqls.append(sql)
            return "0|" if "SELECT COUNT(*)::bigint," in sql else "0"
        with mock.patch.object(retention, "pg", side_effect=fake_pg):
            self.assertEqual(retention.main(["--apply"]), 0)
        self.assertEqual(sum("DELETE FROM" in sql for sql in sqls), 2)


if __name__ == "__main__":
    unittest.main()
