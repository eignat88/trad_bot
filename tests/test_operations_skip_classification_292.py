"""Regression coverage for #292 event type and offline V3 SQL correction."""
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitor = load("monitor_skip_292", "monitoring/operations/trad_bot_monitor.py")
dash = load("dashboard_skip_292", "monitoring/operations/prepare_skip_dashboard_v3.py")


class SkipFixTests(unittest.TestCase):
    def test_both_tokens_with_skip_result(self):
        msg = "BYBIT_API_CALL endpoint=/v5/market/kline result=BYBIT_API_SKIP"
        fields = dict(monitor.LOG_FIELDS.findall(msg))
        self.assertEqual(monitor.classify_event_kind(msg, fields), "SKIP")

    def test_normal_api_call(self):
        msg = "BYBIT_API_CALL endpoint=/v5/market/kline result=OK"
        self.assertEqual(monitor.classify_event_kind(
            msg, dict(monitor.LOG_FIELDS.findall(msg))), "CALL")

    def test_skip_marker_without_call(self):
        self.assertEqual(monitor.classify_event_kind(
            "BYBIT_API_SKIP result=none", {"result": "none"}), "SKIP")

    def test_rate_limit_remains_call(self):
        self.assertEqual(monitor.classify_event_kind(
            "BYBIT_API_CALL result=RATE_LIMIT ret_code=10006",
            {"result": "RATE_LIMIT", "ret_code": "10006"}), "CALL")

    def fixture(self):
        panels = [{"id": i, "targets": [{"rawSql":
            "SELECT 1 WHERE event_kind='CALL'"}] if i in dash.PANEL_IDS
            else [{"rawSql":"SELECT 1"}]} for i in range(1, 22)]
        panels[13]["targets"][0]["rawSql"] = (
            "SELECT 1 WHERE event_kind='SKIP' OR result='BYBIT_API_SKIP'")
        return {"uid": dash.DASHBOARD_UID, "panels": panels}

    def test_patch_four_panels_preserves_skip_panels(self):
        obj = self.fixture()
        before = copy.deepcopy(obj["panels"][13])
        dash.patch_dashboard(obj)
        self.assertEqual(obj["panels"][13], before)
        for panel in obj["panels"]:
            sql = panel["targets"][0]["rawSql"]
            if panel["id"] in dash.PANEL_IDS:
                self.assertIn(dash.NEW, sql)

    def test_bad_panel_count_rejected(self):
        obj = self.fixture()
        obj["panels"].pop()
        with self.assertRaises(ValueError):
            dash.patch_dashboard(obj)


if __name__ == "__main__":
    unittest.main()
