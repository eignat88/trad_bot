from datetime import datetime, timedelta, timezone

import pytest

from app.research.me_r_long_early_mae_v3 import capture
from app.research.me_r_long_early_mae_v3.protocol import (
    SCANNER_NAME,
    SCANNER_VERSION,
    SOURCE_DIRECTION,
)


T = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class FakeRepository:
    def __init__(self):
        self.calls = []

    def ensure_instrument(self, symbol):
        self.calls.append(("ensure_instrument", symbol))
        return 1

    def insert_source_with_variants(self, payload):
        self.calls.append(("insert", payload))
        return 42


@pytest.fixture
def adapter(monkeypatch):
    # Test-only bypass, never modifies production configuration.
    monkeypatch.setattr(
        capture,
        "require_runtime_configuration",
        lambda now: None,
    )
    monkeypatch.setattr(
        capture,
        "ACTIVATION_AT",
        T - timedelta(days=1),
    )
    repo = FakeRepository()
    return capture.MeEarlyMaeV3SourceAdapter(repo), repo


def source(*, completed, available=None, detected=None):
    if available is None:
        available = T + timedelta(seconds=3)
    if detected is None:
        detected = T + timedelta(seconds=5)

    payload = {
        "scanner_name": SCANNER_NAME,
        "scanner_version": SCANNER_VERSION,
        "direction": SOURCE_DIRECTION,
        "symbol": "BTCUSDT",
        "source_candle_close_at": T,
        "source_fetch_started_at": T + timedelta(seconds=1),
        "source_available_at": available,
        "detected_at": detected,
        "reference_price": 100,
    }
    if completed is not ...:
        payload["source_fetch_completed_at"] = completed
    return payload


def test_missing_completion_rejected_before_repository(adapter):
    instance, repo = adapter

    with pytest.raises(
        ValueError,
        match="SOURCE_FETCH_COMPLETION_PROVENANCE_REQUIRED",
    ):
        instance.capture(source(completed=...))

    assert repo.calls == []


def test_fetch_completed_after_detection_rejected(adapter):
    instance, repo = adapter

    with pytest.raises(
        ValueError,
        match="DETECTION_BEFORE_FETCH_COMPLETION",
    ):
        instance.capture(
            source(completed=T + timedelta(seconds=6))
        )

    assert repo.calls == []


def test_claimed_availability_before_completion_rejected(adapter):
    instance, repo = adapter

    with pytest.raises(
        ValueError,
        match="SOURCE_AVAILABILITY_BEFORE_FETCH_COMPLETION",
    ):
        instance.capture(
            source(
                completed=T + timedelta(seconds=4),
                available=T + timedelta(seconds=3),
            )
        )

    assert repo.calls == []


def test_valid_fetch_completion_accepted(adapter):
    instance, repo = adapter

    result = instance.capture(
        source(
            completed=T + timedelta(seconds=3),
            available=T + timedelta(seconds=3),
        )
    )

    assert result == 42
    assert len(repo.calls) == 2

    payload = repo.calls[-1][1]
    assert payload["source_fetch_completed_at"] == (
        T + timedelta(seconds=3)
    )
    assert payload["entry_eligible_at"] == (
        T + timedelta(seconds=5)
    )
