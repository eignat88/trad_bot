"""Offline regression checks for Issue #304 contention audit."""
import unittest

from scripts.audit_scanner_api_contention_v1 import analyze, parse_line


def event(second, *, endpoint="/v5/market/kline", result="OK",
          request="id1", attempt="1", symbol="BTCUSDT",
          interval="5", limit="3"):
    return (
        f"2026-11-26T07:45:{second:02d}+0000 host python: "
        f"BYBIT_API_CALL endpoint={endpoint} symbol={symbol} "
        f"interval={interval} limit={limit} result={result} "
        f"logical_request_id={request} attempt_index={attempt}"
    )


class TestAudit(unittest.TestCase):
    def test_parses_api_log_and_does_not_echo_unrelated_fields(self):
        row = parse_line("v3", event(12) + " secret_token=never_echo")
        self.assertEqual(row["second"], "2026-11-26T07:45:12")
        self.assertNotIn("secret_token", row)
        self.assertEqual(row["result"], "OK")

    def test_sent_not_confused_with_pre_send_skips(self):
        output = analyze({
            "scanner": [
                event(12, result="RATE_LIMIT"),
                event(13, result="BYBIT_API_SKIP"),
                event(14, result="ERROR"),
                "2026-11-26T07:45:15+0000 cycle #297 incomplete: "
                "completion_status=FAILED run_status=PARTIAL expected=50 "
                "processed=0 failed=0 skipped=50",
            ],
            "v3": [event(12), event(13, request="retry", attempt="1"),
                   event(14, request="retry", attempt="2")],
        })
        self.assertEqual(output["by_service"]["scanner"]["sent_confirmed"], 1)
        self.assertEqual(output["by_service"]["scanner"]["pre_send_skip"], 1)
        self.assertEqual(output["by_service"]["v3"]["multi_attempt_logical_ids"], 1)
        self.assertEqual(output["scanner_incomplete_cycles"][0]["expected"], 50)

    def test_overlap_is_matching_shape_not_dedup_claim(self):
        out = analyze({
            "v3": [event(10), event(11), event(12, symbol="ETHUSDT")],
            "oos2": [event(13), event(14, symbol="ETHUSDT", limit="5")],
        })
        self.assertEqual(
            out["five_minute_request_shape_overlap"]["/v5/market/kline"], 1
        )
        self.assertIn("not complete query", out["methodology"])

    def test_no_timestamp_no_event(self):
        self.assertIsNone(parse_line("scanner", "BYBIT_API_CALL result=OK"))

def test_real_pre_send_skip_event_is_counted():
    line = (
        "2026-11-26T08:00:20+0000 host python: "
        "BYBIT_API_SKIP endpoint=/v5/market/kline "
        "symbol=BTCUSDT reason=BREAKER_OPEN "
        "detail=breaker_cooldown_exceeds_deadline"
    )
    parsed = parse_line("scanner", line)
    assert parsed is not None
    assert parsed["result"] == "BYBIT_API_SKIP"

    report = analyze({"scanner": [line]})
    assert report["by_service"]["scanner"]["pre_send_skip"] == 1
    assert report["by_service"]["scanner"]["sent_confirmed"] == 0

if __name__ == "__main__":
    unittest.main()
