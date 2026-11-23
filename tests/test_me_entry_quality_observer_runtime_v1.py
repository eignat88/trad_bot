"""Issue #296: observer lifecycle unit tests."""

from datetime import datetime, timedelta, timezone

from app.research.me_entry_quality_v1.observer import (
    EntryQualityObserver,
)

from test_me_entry_quality_source_observer_v1 import (
    candidate,
)


UTC = timezone.utc
FREEZE = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)


class FakeRepository:
    def __init__(self):
        self.activation = {
            "status": "RUNNING",
            "freeze_ts": FREEZE,
        }
        self.rows = []
        self.saved = {}
        self.health = []
        self.rejected = {}

    def mark_invalid(self, event_key, reason_code):
        self.rejected[event_key] = reason_code

    def load_activation(self):
        return self.activation

    def fetch_pending_events(self, freeze_ts, limit):
        return self.rows[:limit]

    def save_event(self, event):
        if event.event_key in self.saved:
            return "DUPLICATE"
        self.saved[event.event_key] = event
        return "INSERTED"

    def record_health(self, **kwargs):
        self.health.append(kwargs)


def test_not_activated():
    repo = FakeRepository()
    repo.activation = None

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "NOT_ACTIVATED"


def test_not_running():
    repo = FakeRepository()
    repo.activation["status"] = "PAUSED"

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "NOT_RUNNING"


def test_insert_new_event():
    repo = FakeRepository()
    repo.rows = [candidate()]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "OK"
    assert result.events_inserted == 1
    assert len(repo.saved) == 1


def test_duplicate_delivery():
    repo = FakeRepository()
    repo.rows = [candidate()]
    observer = EntryQualityObserver(repo)

    first = observer.run_once()
    second = observer.run_once()

    assert first.events_inserted == 1
    assert second.events_inserted == 0
    assert second.duplicates == 1
    assert len(repo.saved) == 1


def test_pre_freeze_event():
    repo = FakeRepository()
    row = candidate()
    row["signal_time"] = FREEZE

    repo.rows = [row]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "SOURCE_INCOMPLETE"
    assert result.invalid == 1
    assert result.events_inserted == 0


def test_incomplete_lineage():
    repo = FakeRepository()
    row = candidate()
    row["lineage"].pop("source_features")

    repo.rows = [row]

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "SOURCE_INCOMPLETE"
    assert result.invalid == 1
    assert len(repo.saved) == 0


def test_empty_batch():
    repo = FakeRepository()

    result = EntryQualityObserver(repo).run_once()

    assert result.status == "OK"
    assert result.source_rows_seen == 0
    assert result.events_inserted == 0


def test_invalid_batch_limit():
    repo = FakeRepository()

    try:
        EntryQualityObserver(repo, batch_limit=0)
        assert False, "Expected ValueError"
    except ValueError as exc:
        assert str(exc) == "INVALID_BATCH_LIMIT"
