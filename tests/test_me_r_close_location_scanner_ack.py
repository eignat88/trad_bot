"""ME dedicated capture ACK propagation in scanner_runner."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import scanner_runner
from app.scanners.models import SetupCandidate
from app.shadow.me_r_long_close_location_oos_runner import (
    MERLongCLoOosAck,
)


def make_candidate():
    return SetupCandidate(
        scanner_name="ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1",
        scanner_version="1.2.0",
        symbol="BTCUSDT",
        direction="LONG",
        detected_at=datetime(2026, 10, 9, 13, 35, tzinfo=timezone.utc),
        features={"oos_clean_observer_version": "1.2.0"},
    )


@pytest.mark.parametrize(
    "ack",
    [
        MERLongCLoOosAck.INSERTED,
        MERLongCLoOosAck.DUPLICATE,
        MERLongCLoOosAck.ERROR,
        MERLongCLoOosAck.INVALID_SOURCE,
        MERLongCLoOosAck.SKIPPED_VERSION,
    ],
)
def test_scanner_runner_returns_ack_and_logs_failures(ack, caplog):
    repository = MagicMock()
    repository._conn = MagicMock()

    with patch(
        "app.shadow.me_r_long_close_location_oos_runner.observe_oos_candidate",
        return_value=ack,
    ) as observe:
        with caplog.at_level("DEBUG"):
            result = scanner_runner._observe_me_r_long_cl_oos(
                repository, make_candidate()
            )

    assert result == ack
    observe.assert_called_once()

    if ack in (
        MERLongCLoOosAck.ERROR,
        MERLongCLoOosAck.INVALID_SOURCE,
    ):
        assert "capture not acknowledged" in caplog.text

    elif ack == MERLongCLoOosAck.SKIPPED_VERSION:
        assert "capture skipped version" in caplog.text

    else:
        assert "capture not acknowledged" not in caplog.text


def test_ack_function_does_not_modify_candidate_state():
    candidate = make_candidate()
    original_state = candidate.state

    repository = MagicMock()
    repository._conn = MagicMock()

    with patch(
        "app.shadow.me_r_long_close_location_oos_runner.observe_oos_candidate",
        return_value=MERLongCLoOosAck.ERROR,
    ):
        result = scanner_runner._observe_me_r_long_cl_oos(
            repository, candidate
        )

    assert result == MERLongCLoOosAck.ERROR
    assert candidate.state == original_state
