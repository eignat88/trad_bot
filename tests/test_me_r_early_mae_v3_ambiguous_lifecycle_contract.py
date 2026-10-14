from datetime import datetime, timedelta, timezone

from app.research.me_r_long_early_mae_v3.candles import ClosedCandle
from app.research.me_r_long_early_mae_v3.evaluator import (
    MeEarlyMaeV3Evaluator,
)
from app.research.me_r_long_early_mae_v3.lifecycle import can_finalize

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeRepository:
    def __init__(self):
        self.state = "ENTRY_FILLED"
        self.fetch_count = 0
        self.diagnostics = None
        self.finalized = None

    def load_pending_sources(self):
        if self.state != "ENTRY_FILLED":
            return []
        return [{
            "source_event_id": 1,
            "source_status": self.state,
            "entry_filled_at": T,
            "entry_price": 100.0,
            "stop_price": 99.0,
            "target_price": 102.0,
            "baseline_deadline_at": T + timedelta(minutes=240),
            "instrument_id": 1,
        }]

    def fetch_candles(self, instrument_id, start, end):
        self.fetch_count += 1
        candles = []
        for i in range(48):
            opened = T + timedelta(minutes=5 * i)
            candles.append(ClosedCandle(
                opened,
                opened + timedelta(minutes=5),
                100.0,
                103.0 if i == 0 else 100.5,
                98.0 if i == 0 else 99.5,
                100.0,
            ))
        return candles

    def save_paired_outcome(self, source_id, baseline, outcomes):
        self.finalized = (baseline, outcomes)
        self.state = "FINALIZED"

    def save_ambiguous_outcome(self, source_id, baseline, outcomes):
        assert source_id == 1
        assert len(outcomes) == 7
        assert any(row["ambiguous_ordering"] for row in outcomes)
        assert all(row["variant_status"] != "FINALIZED" for row in outcomes)
        self.diagnostics = (baseline, outcomes)
        self.state = "AMBIGUOUS_NONFINAL"


def test_ambiguous_outcome_uses_diagnostic_path():
    repo = FakeRepository()
    stats = MeEarlyMaeV3Evaluator(repo).run_cycle(
        T + timedelta(minutes=241)
    )

    assert stats["errors"] == 0
    assert repo.state == "AMBIGUOUS_NONFINAL"
    assert repo.diagnostics is not None
    assert repo.finalized is None


def test_ambiguous_terminal_state_is_not_retried():
    repo = FakeRepository()
    evaluator = MeEarlyMaeV3Evaluator(repo)

    evaluator.run_cycle(T + timedelta(minutes=241))
    evaluator.run_cycle(T + timedelta(minutes=242))

    assert repo.fetch_count == 1


def test_ambiguous_tuple_cannot_finalize():
    rows = [
        {
            "variant_id": variant.variant_id,
            "variant_status": "AMBIGUOUS",
            "ambiguous_ordering": True,
            "coverage_reason": None,
            "net_r_normal": 0.1,
            "net_r_elevated": 0.05,
            "delta_net_r_normal": 0.0,
            "delta_net_r_elevated": 0.0,
            "variant_exit_at": T,
        }
        for variant in __import__(
            "app.research.me_r_long_early_mae_v3.protocol",
            fromlist=["VARIANTS"],
        ).VARIANTS
    ]

    assert not can_finalize(rows)
