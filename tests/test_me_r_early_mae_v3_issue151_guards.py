from datetime import datetime, timedelta, timezone

import pytest

from app.research.me_r_long_early_mae_v3 import guards
from app.research.me_r_long_early_mae_v3 import protocol
from app.research.me_r_long_early_mae_v3 import capture
from app.research.me_r_long_early_mae_v3 import repository


FROZEN = datetime(2026, 10, 1, tzinfo=timezone.utc)
ACTIVE = FROZEN + timedelta(minutes=5)
NOW = ACTIVE + timedelta(minutes=10)


def configure(monkeypatch, fingerprint):
    monkeypatch.setenv("ME_EARLY_MAE_V3_CAPTURE_ENABLED", "true")
    monkeypatch.setattr(protocol, "FROZEN_AT", FROZEN)
    monkeypatch.setattr(protocol, "ACTIVATION_AT", ACTIVE)
    monkeypatch.setattr(protocol, "APPROVED_FINGERPRINT", fingerprint)
    monkeypatch.setattr(guards, "FROZEN_AT", FROZEN)
    monkeypatch.setattr(guards, "ACTIVATION_AT", ACTIVE)
    monkeypatch.setattr(guards, "APPROVED_FINGERPRINT", fingerprint)


def valid_fingerprint():
    return protocol.calculate_protocol_fingerprint(
        protocol.protocol_payload(FROZEN, ACTIVE)
    )


def test_valid_fingerprint(monkeypatch):
    configure(monkeypatch, valid_fingerprint())
    guards.require_runtime_configuration(NOW)


def test_wrong_fingerprint_rejected(monkeypatch):
    configure(monkeypatch, "0" * 64)
    with pytest.raises(RuntimeError, match="FINGERPRINT_MISMATCH"):
        guards.require_runtime_configuration(NOW)


def test_missing_fingerprint_rejected(monkeypatch):
    configure(monkeypatch, None)
    with pytest.raises(RuntimeError, match="FINGERPRINT_NOT_APPROVED"):
        guards.require_runtime_configuration(NOW)


def test_invalid_freeze_order_rejected():
    with pytest.raises(RuntimeError, match="FREEZE_ORDER_INVALID"):
        protocol.validate_freeze_activation(ACTIVE, FROZEN, NOW)


def test_equal_freeze_activation_rejected():
    with pytest.raises(RuntimeError, match="FREEZE_ORDER_INVALID"):
        protocol.validate_freeze_activation(FROZEN, FROZEN, NOW)


def test_immutable_fingerprint_deterministic():
    payload = protocol.protocol_payload(FROZEN, ACTIVE)
    assert (
        protocol.calculate_protocol_fingerprint(payload)
        == protocol.calculate_protocol_fingerprint(payload)
    )


def test_activation_boundary_changes_fingerprint():
    first = protocol.calculate_protocol_fingerprint(
        protocol.protocol_payload(FROZEN, ACTIVE)
    )
    second = protocol.calculate_protocol_fingerprint(
        protocol.protocol_payload(FROZEN, ACTIVE + timedelta(seconds=1))
    )
    assert first != second


@pytest.mark.parametrize("offset_seconds", [-300, -1, 0])
def test_capture_rejects_historical_candle_before_db(
    monkeypatch, offset_seconds
):
    configure(monkeypatch, valid_fingerprint())

    monkeypatch.setattr(capture, "ACTIVATION_AT", ACTIVE)

    class ForbiddenRepository:
        def ensure_instrument(self, *args, **kwargs):
            raise AssertionError("HISTORICAL_INSTRUMENT_ACCESS")

        def insert_source_with_variants(self, *args, **kwargs):
            raise AssertionError("HISTORICAL_DB_INSERT")

    source = {
        "scanner_name": protocol.SCANNER_NAME,
        "scanner_version": protocol.SCANNER_VERSION,
        "direction": protocol.SOURCE_DIRECTION,
        "symbol": "BTCUSDT",
        "source_candle_close_at": ACTIVE + timedelta(
            seconds=offset_seconds
        ),
        "detected_at": ACTIVE + timedelta(seconds=1),
    }

    adapter = capture.MeEarlyMaeV3SourceAdapter(
        ForbiddenRepository()
    )

    assert adapter.capture(source) is None


@pytest.mark.parametrize("offset_seconds", [-300, -1, 0])
def test_repository_rejects_historical_candle_before_cursor(
    monkeypatch, offset_seconds
):
    configure(monkeypatch, valid_fingerprint())

    monkeypatch.setattr(repository, "FROZEN_AT", FROZEN)
    monkeypatch.setattr(repository, "ACTIVATION_AT", ACTIVE)

    class ForbiddenRepository(repository.MeEarlyMaeV3Repository):
        def _cursor(self):
            raise AssertionError("HISTORICAL_CURSOR_ACCESS")

    instance = object.__new__(ForbiddenRepository)

    close_at = ACTIVE + timedelta(seconds=offset_seconds)
    detected_at = max(close_at, ACTIVE) + timedelta(seconds=1)

    source = {
        "detected_at": detected_at,
        "source_candle_close_at": close_at,
        "source_available_at": detected_at,
    }

    with pytest.raises(
        ValueError, match="PRE_ACTIVATION_CANDLE_REJECTED"
    ):
        instance.insert_source_with_variants(source)


def test_capture_rejects_pre_activation_detection(monkeypatch):
    configure(monkeypatch, valid_fingerprint())
    monkeypatch.setattr(capture, "ACTIVATION_AT", ACTIVE)

    class ForbiddenRepository:
        def ensure_instrument(self, *args, **kwargs):
            raise AssertionError("UNEXPECTED_DB_ACCESS")

        def insert_source_with_variants(self, *args, **kwargs):
            raise AssertionError("UNEXPECTED_DB_INSERT")

    source = {
        "scanner_name": protocol.SCANNER_NAME,
        "scanner_version": protocol.SCANNER_VERSION,
        "direction": protocol.SOURCE_DIRECTION,
        "symbol": "BTCUSDT",
        "source_candle_close_at": ACTIVE + timedelta(seconds=1),
        "detected_at": ACTIVE,
    }

    adapter = capture.MeEarlyMaeV3SourceAdapter(
        ForbiddenRepository()
    )

    assert adapter.capture(source) is None


def test_disabled_capture_never_reaches_repository(monkeypatch):
    monkeypatch.setenv("ME_EARLY_MAE_V3_CAPTURE_ENABLED", "false")

    class ForbiddenRepository:
        def __getattr__(self, name):
            raise AssertionError("REPOSITORY_ACCESS:" + name)

    adapter = capture.MeEarlyMaeV3SourceAdapter(
        ForbiddenRepository()
    )

    with pytest.raises(RuntimeError, match="CAPTURE_DISABLED"):
        adapter.capture({})


def test_valid_post_activation_source_reaches_repository(monkeypatch):
    configure(monkeypatch, valid_fingerprint())
    monkeypatch.setattr(capture, "ACTIVATION_AT", ACTIVE)
    monkeypatch.setattr(
        capture,
        "require_runtime_configuration",
        lambda now: None,
    )

    close_at = ACTIVE + timedelta(minutes=5)
    fetch_started = close_at + timedelta(seconds=1)
    fetch_completed = close_at + timedelta(seconds=2)
    available_at = close_at + timedelta(seconds=3)
    detected_at = close_at + timedelta(seconds=4)

    class RecordingRepository:
        def __init__(self):
            self.calls = []

        def insert_source_with_variants(self, payload):
            self.calls.append(payload)
            return 123

    repo = RecordingRepository()

    source = {
        "scanner_name": protocol.SCANNER_NAME,
        "scanner_version": protocol.SCANNER_VERSION,
        "direction": protocol.SOURCE_DIRECTION,
        "symbol": "BTCUSDT",
        "instrument_id": 1,
        "source_candle_close_at": close_at,
        "source_fetch_started_at": fetch_started,
        "source_fetch_completed_at": fetch_completed,
        "source_available_at": available_at,
        "detected_at": detected_at,
        "reference_price": 100.0,
    }

    result = capture.MeEarlyMaeV3SourceAdapter(repo).capture(source)

    assert result == 123
    assert len(repo.calls) == 1

    payload = repo.calls[0]
    assert payload["entry_eligible_at"] > ACTIVE
    assert payload["entry_eligible_at"] == detected_at
    assert payload["stop_price"] == pytest.approx(97.5)
    assert payload["target_price"] == pytest.approx(103.0)


@pytest.mark.parametrize("raw", [
    "invalid",
    "2026-10-15T10:00:00",
    "2026-10-15T10:00:00+03:00",
])
def test_invalid_utc_boundary_fail_closed(monkeypatch, raw):
    monkeypatch.setenv("ME_EARLY_MAE_V3_FROZEN_AT", raw)
    with pytest.raises((ValueError, RuntimeError)):
        protocol._read_utc_boundary(
            "ME_EARLY_MAE_V3_FROZEN_AT"
        )


def test_fingerprint_changes_with_freeze_timestamp():
    first = protocol.calculate_protocol_fingerprint(
        protocol.protocol_payload(FROZEN, ACTIVE)
    )
    second = protocol.calculate_protocol_fingerprint(
        protocol.protocol_payload(
            FROZEN + timedelta(seconds=1), ACTIVE
        )
    )
    assert first != second


def test_runtime_guard_rejects_missing_activation(monkeypatch):
    configure(monkeypatch, valid_fingerprint())
    monkeypatch.setattr(guards, "ACTIVATION_AT", None)

    with pytest.raises(RuntimeError, match="FREEZE_NOT_CONFIGURED"):
        guards.require_runtime_configuration(NOW)


def test_runtime_guard_rejects_disabled_capture(monkeypatch):
    configure(monkeypatch, valid_fingerprint())
    monkeypatch.setenv("ME_EARLY_MAE_V3_CAPTURE_ENABLED", "false")

    with pytest.raises(RuntimeError, match="CAPTURE_DISABLED"):
        guards.require_runtime_configuration(NOW)
