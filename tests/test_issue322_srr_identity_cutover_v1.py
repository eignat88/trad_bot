"""Issue #322: prospective-only SRR identity cutover."""

from datetime import datetime, timezone

import pytest

from app.research.prospective_observer import ProspectiveOOSObserver


CUTOVER = "2026-11-26T12:00:00Z"


@pytest.mark.parametrize(
    "candle_ms, expected",
    [
        (1795694100000, False),  # Before
        (1795694400000, False),  # Exactly at cutover
        (1795694700000, True),   # After
    ],
)
def test_strict_candle_cutover(candle_ms, expected):
    assert ProspectiveOOSObserver._srr_identity_cutover_allows(
        candle_ms, CUTOVER
    ) is expected


@pytest.mark.parametrize(
    "cutover",
    [None, "", "invalid", "2026-11-26T12:00:00"],
)
def test_missing_or_invalid_cutover_fails_closed(cutover):
    assert not ProspectiveOOSObserver._srr_identity_cutover_allows(
        1795694700000, cutover
    )


@pytest.mark.parametrize(
    "candle",
    [None, 0, -1, True, "1795694700000"],
)
def test_invalid_candle_fails_closed(candle):
    assert not ProspectiveOOSObserver._srr_identity_cutover_allows(
        candle, CUTOVER
    )


def test_timezone_equivalence():
    assert ProspectiveOOSObserver._srr_identity_cutover_allows(
        1795694700000, "2026-11-26T15:00:00+03:00"
    )


def test_original_freeze_remains_independent():
    original_freeze = datetime.fromisoformat(
        "2026-10-07T08:17:50+00:00"
    )
    cutover = datetime.fromisoformat(
        CUTOVER.replace("Z", "+00:00")
    )

    assert original_freeze < cutover
    assert original_freeze.tzinfo == timezone.utc
