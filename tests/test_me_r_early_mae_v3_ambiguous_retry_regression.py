from datetime import datetime, timedelta, timezone

from app.research.me_r_long_early_mae_v3.candles import ClosedCandle
from app.research.me_r_long_early_mae_v3.evaluator import (
    MeEarlyMaeV3Evaluator,
)

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeRepository:
    def __init__(self):
        self.state = "ENTRY_FILLED"
        self.fetch_count = 0
        self.saved = None

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
            candles.append(
                ClosedCandle(
                    opened,
                    opened + timedelta(minutes=5),
                    100.0,
                    103.0 if i == 0 else 100.5,
                    98.0 if i == 0 else 99.5,
                    100.0,
                )
            )
        return candles

    def save_paired_outcome(self, source_id, baseline, outcomes):
        raise AssertionError("AMBIGUOUS_MUST_NOT_FINALIZE")

    def save_ambiguous_outcome(self, source_id, baseline, outcomes):
        assert source_id == 1
        assert baseline["source_status"] == "AMBIGUOUS_NONFINAL"
        assert len(outcomes) == 7
        assert any(row["ambiguous_ordering"] for row in outcomes)
        assert all(
            row["variant_status"] != "FINALIZED"
            for row in outcomes
        )
        self.saved = (baseline, outcomes)
        self.state = "AMBIGUOUS_NONFINAL"


def test_ambiguous_source_is_nonfinal_and_not_retried():
    repo = FakeRepository()
    evaluator = MeEarlyMaeV3Evaluator(repo)

    first = evaluator.run_cycle(T + timedelta(minutes=241))
    second = evaluator.run_cycle(T + timedelta(minutes=242))

    assert first["errors"] == 0
    assert second["errors"] == 0
    assert repo.fetch_count == 1
    assert repo.state == "AMBIGUOUS_NONFINAL"
    assert repo.saved is not None
    assert len(repo.saved[1]) == 7
    assert any(
        row["ambiguous_ordering"]
        for row in repo.saved[1]
    )
    assert all(
        row["variant_status"] != "FINALIZED"
        for row in repo.saved[1]
    )
