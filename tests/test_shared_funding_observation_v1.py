"""Offline-only contract tests; no integration with trade engines or network."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from app.research.shared_funding_observation_v1 import (
    FundingObservation,
    FundingStatus,
    parse_funding_history_observation,
    unobserved_funding,
)


NOW = datetime(2026, 11, 26, 8, 45, tzinfo=timezone.utc)


def raw(rate, timestamp=None):
    row = {"fundingRate": rate}
    if timestamp is not None:
        row["fundingRateTimestamp"] = timestamp
    return {"result": {"list": [row]}}


def test_confirmed_zero_is_distinct_from_unknown():
    zero = parse_funding_history_observation(symbol="ETHUSDT", payload=raw("0"), observed_at=NOW)
    missing = parse_funding_history_observation(symbol="ETHUSDT", payload={"result": {"list": []}}, observed_at=NOW)
    assert zero.status == FundingStatus.OK
    assert zero.rate_percent == 0.0
    assert missing.status == FundingStatus.MISSING
    assert missing.rate_percent is None


def test_percent_conversion_and_provenance_preserved():
    observation = parse_funding_history_observation(
        symbol="ADAUSDT", payload=raw("0.0001", "1795680000000"), observed_at=NOW
    )
    assert observation.rate_percent == pytest.approx(0.01)
    assert observation.funding_event_ms == 1795680000000
    assert observation.observed_at == NOW
    assert observation.identity == ("linear", "ADAUSDT", 1, 1795680000000)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", None, "oops"])
def test_nonfinite_and_invalid_rates_do_not_become_known(bad):
    obs = parse_funding_history_observation(
        symbol="SOLUSDT", payload=raw(bad), observed_at=NOW
    )
    assert obs.status == FundingStatus.INVALID
    assert obs.rate_percent is None


@pytest.mark.parametrize("payload", [{}, {"result": {}}, {"result": {"list": None}}, {"result": {"list": ["0.01"]}}])
def test_malformed_payload_is_invalid(payload):
    obs = parse_funding_history_observation(symbol="SOLUSDT", payload=payload, observed_at=NOW)
    assert obs.status == FundingStatus.INVALID
    assert obs.rate_percent is None


def test_unobserved_api_error_never_implies_zero():
    obs = unobserved_funding("ETHUSDT", FundingStatus.API_ERROR, NOW)
    assert obs.rate_percent is None
    with pytest.raises(ValueError):
        unobserved_funding("ETHUSDT", FundingStatus.OK, NOW)


def test_unknown_cannot_carry_a_numeric_rate():
    with pytest.raises(ValueError):
        FundingObservation("ETHUSDT", FundingStatus.MISSING, NOW, 0.0)


def test_utc_is_required():
    with pytest.raises(ValueError):
        FundingObservation("ETHUSDT", FundingStatus.OK, NOW.replace(tzinfo=None), 0.01)
    with pytest.raises(ValueError):
        FundingObservation("ETHUSDT", FundingStatus.OK, NOW.astimezone(timezone(timedelta(hours=3))), 0.01)


def test_observations_are_immutable():
    obj = FundingObservation("ETHUSDT", FundingStatus.OK, NOW, 0.01)
    with pytest.raises(FrozenInstanceError):
        obj.rate_percent = 0.02


@pytest.mark.parametrize("timestamp", ["not-a-time", "-1", "1.2", True])
def test_invalid_period_stamp_is_not_silently_accepted(timestamp):
    obs = parse_funding_history_observation(
        symbol="ETHUSDT", payload=raw("0.0001", timestamp), observed_at=NOW
    )
    assert obs.status == FundingStatus.INVALID
    assert obs.rate_percent is None


def test_missing_period_stamp_is_explicitly_unknown():
    obs = parse_funding_history_observation(
        symbol="ETHUSDT", payload=raw("0.0001"), observed_at=NOW
    )
    assert obs.status == FundingStatus.OK
    assert obs.funding_event_ms is None


def test_default_provenance_cannot_be_reassigned():
    with pytest.raises(ValueError):
        FundingObservation("ETHUSDT", FundingStatus.OK, NOW, 0.01, source="arbitrary")
