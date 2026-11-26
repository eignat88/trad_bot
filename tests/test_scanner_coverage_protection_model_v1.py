"""Deterministic, no-network checks for Issue #304 hypothetical policy."""
import unittest

from app.research.scanner_coverage_protection_model_v1 import (
    Demand, RequestEvidence, RequestRole, classify_request, simulate_slots,
)


class TestClassification(unittest.TestCase):
    def test_open_position_bar_and_funding_protected(self):
        for endpoint in ("/v5/market/kline", "/v5/market/funding/history"):
            with self.subTest(endpoint=endpoint):
                evidence = RequestEvidence(
                    service="v3", endpoint=endpoint, symbol="BTCUSDT",
                    open_position_symbols=frozenset({"BTCUSDT"}),
                )
                self.assertEqual(classify_request(evidence), RequestRole.PROTECTIVE)

    def test_mixed_tickers_protected(self):
        e = RequestEvidence(
            service="oos2", endpoint="/v5/market/tickers",
            whole_market=True, open_position_symbols=frozenset({"ETHUSDT"}),
            entry_only_symbols=frozenset({"BTCUSDT"}),
        )
        self.assertEqual(classify_request(e), RequestRole.PROTECTIVE)

    def test_entry_only_tickers_are_not_falsely_deferred(self):
        e = RequestEvidence(
            service="v3", endpoint="/v5/market/tickers",
            whole_market=True, entry_only_symbols=frozenset({"BTCUSDT"}),
        )
        self.assertEqual(classify_request(e), RequestRole.UNKNOWN)

    def test_scanner_kline_is_identified(self):
        e = RequestEvidence("scanner", "/v5/market/kline", "ETHUSDT")
        self.assertEqual(classify_request(e), RequestRole.SCANNER)

    def test_unknown_caller_never_marked_entry_only(self):
        e = RequestEvidence("other", "/v5/market/kline", "ETHUSDT")
        self.assertEqual(classify_request(e), RequestRole.UNKNOWN)


class TestHypotheticalContention(unittest.TestCase):
    @staticmethod
    def scenario():
        # 2 protective, 4 scanner, 4 discretionary demand in 8 abstract
        # slots. The user-selected policy may shed discretionary work only.
        return [
            *(Demand(f"entry{i}", RequestRole.ENTRY_ONLY, 0, 0)
              for i in range(4)),
            *(Demand(f"exit{i}", RequestRole.PROTECTIVE, 0, 0)
              for i in range(2)),
            *(Demand(f"scan{i}", RequestRole.SCANNER, 0, 0)
              for i in range(4)),
        ]

    def test_candidate_preserves_protective_and_improves_scanner(self):
        legacy = simulate_slots(self.scenario(), [8])
        proposed = simulate_slots(self.scenario(), [8], policy_enabled=True)
        self.assertEqual(proposed.by_role_sent["PROTECTIVE"], 2)
        self.assertEqual(proposed.by_role_sent["SCANNER"], 4)
        self.assertEqual(legacy.by_role_sent["SCANNER"], 2)
        self.assertEqual(len(proposed.not_sent), 2)
        self.assertEqual(proposed.total, 10)

    def test_insufficient_budget_cannot_promise_coverage(self):
        result = simulate_slots(self.scenario(), [3], policy_enabled=True)
        self.assertEqual(result.by_role_sent["PROTECTIVE"], 2)
        self.assertEqual(result.by_role_sent["SCANNER"], 1)
        self.assertEqual(len(result.not_sent), 7)

    def test_unknown_is_never_sacrificed_for_scanner(self):
        result = simulate_slots(
            [Demand("scan", RequestRole.SCANNER, 0, 0),
             Demand("unknown", RequestRole.UNKNOWN, 0, 0)],
            [1], policy_enabled=True,
        )
        self.assertEqual(result.sent, ("unknown",))

    def test_default_off_preserves_arrival_order(self):
        jobs = self.scenario()
        result = simulate_slots(jobs, [1])
        self.assertEqual(result.sent, ("entry0",))

    def test_deadline_and_future_arrival_enforced(self):
        jobs = [
            Demand("late", RequestRole.PROTECTIVE, 2, 2),
            Demand("early", RequestRole.SCANNER, 0, 0),
        ]
        result = simulate_slots(jobs, [0, 1, 1], policy_enabled=True)
        self.assertEqual(result.sent, ("late",))
        self.assertEqual(result.not_sent, ("early",))

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):
            simulate_slots(
                [Demand("same", RequestRole.SCANNER, 0, 0),
                 Demand("same", RequestRole.SCANNER, 0, 0)], [1])
        with self.assertRaises(ValueError):
            simulate_slots([], [-1])



    def test_fifo_unsorted_input_default_off(self):
        jobs = [
            Demand("new", RequestRole.SCANNER, 1, 2),
            Demand("old", RequestRole.SCANNER, 0, 2),
        ]
        result = simulate_slots(jobs, [0, 1, 0])
        self.assertEqual(result.sent, ("old",))
        self.assertEqual(result.not_sent, ("new",))

    def test_fifo_unsorted_input_candidate_policy(self):
        jobs = [
            Demand("new", RequestRole.PROTECTIVE, 1, 2),
            Demand("old", RequestRole.PROTECTIVE, 0, 2),
        ]
        result = simulate_slots(
            jobs, [0, 1, 0], policy_enabled=True
        )
        self.assertEqual(result.sent, ("old",))
        self.assertEqual(result.not_sent, ("new",))


if __name__ == "__main__":
    unittest.main()
