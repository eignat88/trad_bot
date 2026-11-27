"""Issue #322 global identity migration — canonical key derivation (offline).

No database, no network, no production writes.  The stable key must be
deterministic, domain-separated per family, µs-precision timestamp hygiene,
and fail-closed on unknown lineage.
"""

from datetime import datetime, timezone

import pytest

from app.research.scanner_signal_identity_migration_v1 import (
    FAMILY_SLUGS,
    IdentityError,
    canonical_key_hex,
    canonical_payload,
    family_slug,
    format_ts_us,
    legacy_source_signal_id,
    stable_source_signal_id,
)


def key(scanner="MOMENTUM_EXHAUSTION", symbol="BTCUSDT", direction="SHORT",
        tf="5m", candle=1795680000000, slug=None):
    return stable_source_signal_id(
        scanner, symbol, direction, tf, candle, slug=slug
    )


# --- determinism & identity shape ----------------------------------------

def test_same_candle_same_identity():
    assert key() == key()


def test_new_candle_new_identity():
    assert key(candle=1795680000000) != key(candle=1795680300000)


def test_identity_components_each_change_the_key():
    base = key()
    assert base != key(symbol="ETHUSDT")
    assert base != key(direction="LONG")
    assert base != key(tf="15m")
    assert base != key(scanner="SUPPORT_RESISTANCE_REACTION")


def test_identity_is_negative_bigint_below_legacy_band():
    value = key()
    assert isinstance(value, int)
    assert -(2**63) <= value < -(2**31)


# --- domain separation ---------------------------------------------------

def test_family_slug_registry():
    assert family_slug("MOMENTUM_EXHAUSTION", "SHORT") == "candle"
    assert family_slug("MOMENTUM_EXHAUSTION", "LONG") == "me-long"
    assert family_slug("SUPPORT_RESISTANCE_REACTION", "SHORT") == "srr"
    assert family_slug("MOMENTUM_EXHAUSTION", "MID") is None
    assert family_slug("", "SHORT") is None


def test_domain_separation_prefix_present():
    payload = canonical_payload(
        "srr", "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", "5m", 1795680000000
    )
    assert b'"issue322:srr:v1"' in payload


def test_same_fields_different_family_slug_different_key():
    a = key(scanner="SUPPORT_RESISTANCE_REACTION", slug="srr")
    b = key(scanner="SUPPORT_RESISTANCE_REACTION", slug="vc")
    assert a != b
    # auto-derived slug matches the explicit registry slug
    assert a == key(scanner="SUPPORT_RESISTANCE_REACTION")


def test_unregistered_family_fails_closed():
    assert key(scanner="NOT_A_SCANNER") is None
    assert key(direction="SIDEWAYS") is None


def test_family_registry_covers_audited_families():
    for expected in ("candle", "me-long", "srr", "vc", "lr", "fvg",
                     "breakout-retest", "tp-v3", "me-cl", "mer", "merl-v1"):
        assert expected in FAMILY_SLUGS.values()


# --- fail-closed on invalid lineage --------------------------------------

@pytest.mark.parametrize("bad_candle", [None, 0, -1, True, "0", 1795680000000.0])
def test_invalid_candle_fails_closed(bad_candle):
    assert key(candle=bad_candle) is None


@pytest.mark.parametrize("bad_tf", [None, "", "  ", 5])
def test_invalid_timeframe_fails_closed(bad_tf):
    assert key(tf=bad_tf) is None


# --- canonical hex form --------------------------------------------------

def test_canonical_key_hex_is_stable_64_hex():
    hex_key = canonical_key_hex(
        "candle", "MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "5m", 1795680000000
    )
    assert len(hex_key) == 64
    assert int(hex_key, 16) >= 0
    assert hex_key == canonical_key_hex(
        "candle", "MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "5m", 1795680000000
    )


# --- legacy key replica (alias mapping aid only) -------------------------

def test_legacy_key_matches_frozen_formula():
    expected = -abs(hash("MOMENTUM_EXHAUSTION:BTCUSDT:SHORT:2026-11-25 10:00:00+00:00")) % (2**31)
    assert legacy_source_signal_id(
        "MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "2026-11-25 10:00:00+00:00"
    ) == expected


def test_legacy_key_stays_inside_31_bit_band():
    value = legacy_source_signal_id("SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "LONG", 1795680000000)
    assert 0 <= value < 2**31


# --- µs-precision timestamps ---------------------------------------------

def test_format_ts_us_microsecond_precision():
    dt = datetime(2026, 11, 25, 10, 0, 0, 123456, tzinfo=timezone.utc)
    assert format_ts_us(dt) == "2026-11-25T10:00:00.123456+00:00"


def test_format_ts_us_roundtrips_with_fromisoformat():
    dt = datetime(2026, 11, 25, 10, 0, 0, 987654, tzinfo=timezone.utc)
    assert datetime.fromisoformat(format_ts_us(dt)) == dt


def test_format_ts_us_normalizes_to_utc():
    dt = datetime(2026, 11, 25, 12, 0, 0, 1, tzinfo=timezone.utc).astimezone(
        timezone.utc
    )
    assert format_ts_us(dt).endswith("+00:00")


def test_format_ts_us_rejects_naive_and_invalid():
    with pytest.raises(IdentityError):
        format_ts_us(datetime(2026, 11, 25, 10, 0, 0))
    with pytest.raises(IdentityError):
        format_ts_us("2026-11-25T10:00:00")
