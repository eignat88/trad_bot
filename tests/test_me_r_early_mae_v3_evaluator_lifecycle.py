from datetime import datetime, timedelta, timezone
from app.research.me_r_long_early_mae_v3.candles import ClosedCandle
from app.research.me_r_long_early_mae_v3 import protocol as _protocol
from app.research.me_r_long_early_mae_v3.evaluator import MeEarlyMaeV3Evaluator

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
A = T - timedelta(minutes=1)

class Repo:
    def __init__(self):
        self.state = "ENTRY_PENDING"
        self.entry = None
        self.saved = None
        self.candles = []
        self.marked = None
    def load_pending_sources(self):
        if self.state == "ENTRY_FILLED" and self.entry is not None:
            return [{
                "source_event_id": 1, "instrument_id": 7,
                "entry_filled_at": self.entry["entry_filled_at"],
                "entry_price": self.entry["entry_price"],
                "stop_price": self.entry["stop_price"],
                "target_price": self.entry["target_price"],
                "baseline_deadline_at": self.entry["baseline_deadline_at"],
                "source_status": "ENTRY_FILLED",
                "entry_eligible_at": T, "detected_at": T,
            }]
        if self.state == "ENTRY_PENDING":
            return [{
                "source_event_id": 1, "instrument_id": 7,
                "entry_filled_at": None, "entry_price": None,
                "stop_price": None, "target_price": None,
                "baseline_deadline_at": None, "source_status": "ENTRY_PENDING",
                "entry_eligible_at": T, "detected_at": T,
            }]
        return [{
            "source_event_id": 1, "instrument_id": 7,
            "entry_filled_at": self.entry.get("entry_filled_at") if self.entry else None,
            "entry_price": self.entry.get("entry_price") if self.entry else None,
            "stop_price": self.entry.get("stop_price") if self.entry else None,
            "target_price": self.entry.get("target_price") if self.entry else None,
            "baseline_deadline_at": self.entry.get("baseline_deadline_at") if self.entry else None,
            "source_status": self.state,
            "entry_eligible_at": T,
            "detected_at": T,
        }]
    def fetch_candles(self, instrument_id, start, end):
        return self.candles
    def update_source_entry(self, source_event_id, entry):
        assert self.state in ("DETECTED", "ENTRY_PENDING")
        self.entry = entry
        self.state = "ENTRY_FILLED"
    def mark_source_not_filled(self, source_event_id, reason):
        self.marked = ("NOT_FILLED", reason); self.state = "ENTRY_NOT_FILLED"
    def mark_source_incomplete(self, source_event_id, reason):
        self.marked = ("INCOMPLETE", reason)
    def mark_source_invalid(self, source_event_id, reason):
        self.marked = ("INVALID", reason); self.state = "INVALID"
    def save_paired_outcome(self, source_event_id, baseline, outcomes):
        assert len(outcomes) == 7
        self.saved = (baseline, outcomes); self.state = "FINALIZED"

def candles():
    return [ClosedCandle(o := T + timedelta(minutes=5 * i), o + timedelta(minutes=5), 100, 101, 99, 100) for i in range(49)]

def test_pending_without_fill_fields_is_safe():
    repo = Repo(); repo.candles = candles()
    _protocol.ACTIVATION_AT = A
    MeEarlyMaeV3Evaluator(repo).run_cycle(T + timedelta(minutes=10))
    assert repo.state == "ENTRY_FILLED"

def test_baseline_and_seven_variants_finalize_once():
    repo = Repo(); repo.candles = candles()
    _protocol.ACTIVATION_AT = A
    MeEarlyMaeV3Evaluator(repo).run_cycle(T + timedelta(minutes=10))
    assert repo.state == "ENTRY_FILLED"
    print(repo.load_pending_sources())
    MeEarlyMaeV3Evaluator(repo).run_cycle(T + timedelta(minutes=246))
    assert repo.state == "FINALIZED" and repo.saved is not None
    assert len(repo.saved[1]) == 7

def test_incomplete_source_cannot_finalize():
    repo = Repo(); repo.candles = []
    MeEarlyMaeV3Evaluator(repo).run_cycle(T + timedelta(minutes=10))
    assert repo.state != "FINALIZED" and repo.marked[0] == "INCOMPLETE"
