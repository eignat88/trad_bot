"""Issue #322 — legacy identity formation guard (review B1, offline).

The legacy ``source_signal_id`` of a dual-identity capture must come from ONE
of exactly two sources:

* a CONFIRMED original legacy id (persisted historical value, used verbatim), or
* a validated ``legacy_signal_time`` — the exact frozen key input.

A missing, empty, naive or malformed signal time must NEVER yield a legacy id:
no ``None``/``""``/now()/zero fallback, no silently-aliased wrong identity.
Negative and regression coverage for the required scenario table rows:
missing ``legacy_signal_time``, invalid legacy timestamp, rescan stability,
new candle -> new id, historical OOS alias matching, DEFAULT-OFF unchanged.

No database, no network, no production writes.
"""

from datetime import datetime, timezone

import pytest

from app.research.scanner_signal_identity_migration_v1 import (
    PHASE_0_DEFAULT_OFF,
    PHASE_1_SHADOW_DUAL_IDENTITY,
    AliasIntegrityError,
    IdentityError,
    MigrationConfig,
    MigrationIdentityCore,
    legacy_source_signal_id,
    resolve_legacy_identity,
    stable_source_signal_id,
    validate_legacy_signal_time,
)

CANDLE = 1795680000000
CANDLE_NEXT = 1795680300000
SCANNER = "SUPPORT_RESISTANCE_REACTION"
ISO_TIME = "2026-11-25T10:00:07.123456+00:00"
AWARE_DT = datetime(2026, 11, 25, 10, 0, 7, 123456, tzinfo=timezone.utc)


def enabled_core(persistence=None):
    return MigrationIdentityCore(
        MigrationConfig(
            phase=PHASE_1_SHADOW_DUAL_IDENTITY,
            families_enabled=frozenset({"srr"}),
            dual_write=True,
        ),
        persistence=persistence,
    )


class TripwireHook:
    """Atomic-contract tripwire: any batch delivery is a test failure."""

    def __init__(self):
        self.calls = 0

    def save_identity_batch(self, batch):
        self.calls += 1
        raise AssertionError("persistence must not be reached on reject")


def capture(core, **kwargs):
    return core.capture_dual(
        SCANNER, "BTCUSDT", "SHORT", "5m", CANDLE, **kwargs
    )


# --- B1: missing / empty / malformed legacy input is rejected -------------

def test_missing_legacy_signal_time_is_rejected():
    core = enabled_core()
    with pytest.raises(IdentityError, match="MISSING_LEGACY_SIGNAL_TIME"):
        capture(core)  # omitted kwarg -> None
    with pytest.raises(IdentityError, match="MISSING_LEGACY_SIGNAL_TIME"):
        capture(core, legacy_signal_time=None)
    assert len(core.aliases) == 0


@pytest.mark.parametrize("empty", ["", "   ", "\t\n", "  \x20 "])
def test_empty_legacy_signal_time_is_rejected(empty):
    core = enabled_core()
    with pytest.raises(IdentityError, match="EMPTY_LEGACY_SIGNAL_TIME"):
        capture(core, legacy_signal_time=empty)
    assert len(core.aliases) == 0


@pytest.mark.parametrize("bad", [
    "not-a-timestamp",
    "25.11.2026 10:00:07",
    "1795680000000",
    "2026-11-25T99:99:99+00:00",
    "2026-13-45T10:00:07+00:00",
])
def test_malformed_legacy_timestamp_is_rejected(bad):
    core = enabled_core()
    with pytest.raises(IdentityError, match="INVALID_LEGACY_SIGNAL_TIME"):
        capture(core, legacy_signal_time=bad)
    assert len(core.aliases) == 0


@pytest.mark.parametrize("bad", [0, 1, 1795680000000, 1.5, True, b"2026-11-25", object()])
def test_non_timestamp_legacy_signal_time_is_rejected(bad):
    core = enabled_core()
    with pytest.raises(IdentityError, match="INVALID_LEGACY_SIGNAL_TIME"):
        capture(core, legacy_signal_time=bad)
    assert len(core.aliases) == 0


@pytest.mark.parametrize("naive", [
    datetime(2026, 11, 25, 10, 0, 7),
    "2026-11-25 10:00:07",
    "2026-11-25T10:00:07",
    "2026-11-25",
])
def test_naive_legacy_signal_time_is_rejected(naive):
    core = enabled_core()
    with pytest.raises(IdentityError, match="NAIVE_LEGACY_SIGNAL_TIME"):
        capture(core, legacy_signal_time=naive)
    assert len(core.aliases) == 0


def test_reject_never_writes_alias_or_record():
    hook = TripwireHook()
    core = enabled_core(persistence=hook)
    for bad in (None, "", "garbage", 1795680000000):
        with pytest.raises(IdentityError):
            capture(core, legacy_signal_time=bad)
    assert hook.calls == 0  # never reached persistence
    assert len(core.aliases) == 0


# --- B1: confirmed original legacy id is used verbatim --------------------

def test_confirmed_legacy_id_is_used_verbatim():
    historical = 777_000_123  # e.g. persisted source_signal_id of a frozen OOS row
    core = enabled_core()
    rec = capture(core, legacy_source_signal_id=historical)
    assert rec.legacy_source_signal_id == historical  # never re-derived
    assert core.resolve_legacy(historical) == rec.stable_source_signal_id
    assert core.resolve_stable(rec.stable_source_signal_id) == (historical,)


def test_confirmed_legacy_id_preferred_over_signal_time():
    historical = 424_242
    core = enabled_core()
    rec = capture(
        core,
        legacy_signal_time=ISO_TIME,
        legacy_source_signal_id=historical,
    )
    assert rec.legacy_source_signal_id == historical
    assert rec.legacy_source_signal_id != legacy_source_signal_id(
        SCANNER, "BTCUSDT", "SHORT", ISO_TIME
    )


@pytest.mark.parametrize("bad", [-1, 2**31, 2**31 + 1, "123", 1.0, True, -(2**40)])
def test_invalid_confirmed_legacy_id_is_rejected(bad):
    core = enabled_core()
    with pytest.raises(IdentityError, match="INVALID_LEGACY_SOURCE_SIGNAL_ID"):
        capture(core, legacy_source_signal_id=bad)
    assert len(core.aliases) == 0


def test_confirmed_legacy_id_zero_is_valid_band_value():
    # 0 is a legal frozen-formula output and must not be treated as "missing".
    core = enabled_core()
    rec = capture(core, legacy_source_signal_id=0)
    assert rec.legacy_source_signal_id == 0


# --- B1: deterministic recovery from the exact frozen key inputs ----------

@pytest.mark.parametrize("signal_time", [ISO_TIME, AWARE_DT])
def test_derived_legacy_id_matches_frozen_replica(signal_time):
    core = enabled_core()
    rec = capture(core, legacy_signal_time=signal_time)
    assert rec.legacy_source_signal_id == legacy_source_signal_id(
        SCANNER, "BTCUSDT", "SHORT", signal_time
    )


def test_derived_legacy_id_uses_raw_input_formatting():
    # The frozen key stringifies the EXACT object observe() received, so the
    # replica must never normalize the input (e.g. "Z" vs "+00:00").
    core = enabled_core()
    rec = capture(core, legacy_signal_time="2026-11-25T10:00:07Z")
    assert rec.legacy_source_signal_id == legacy_source_signal_id(
        SCANNER, "BTCUSDT", "SHORT", "2026-11-25T10:00:07Z"
    )
    assert rec.legacy_source_signal_id != legacy_source_signal_id(
        SCANNER, "BTCUSDT", "SHORT", "2026-11-25T10:00:07+00:00"
    )


def test_validate_accepts_aware_datetime_and_iso_string():
    validate_legacy_signal_time(AWARE_DT)
    validate_legacy_signal_time(ISO_TIME)
    validate_legacy_signal_time("2026-11-25T10:00:07Z")


def test_resolve_legacy_identity_never_substitutes_fallbacks():
    # No current-time/zero/epoch fallback: insufficient data raises.
    for bad in (None, "", "junk", 0):
        with pytest.raises(IdentityError):
            resolve_legacy_identity(SCANNER, "BTCUSDT", "SHORT", legacy_signal_time=bad)
    # ...and nothing about "now" leaks into a derivation.
    with pytest.raises(IdentityError):
        resolve_legacy_identity(SCANNER, "BTCUSDT", "SHORT")


# --- regression: identity stability & historical compatibility ------------

def test_rescan_same_signal_same_identity_and_no_alias_duplication():
    core = enabled_core()
    rec1 = capture(core, legacy_signal_time=ISO_TIME)
    rec2 = capture(core, legacy_signal_time=ISO_TIME)
    assert rec1.stable_source_signal_id == rec2.stable_source_signal_id
    assert rec1.legacy_source_signal_id == rec2.legacy_source_signal_id
    assert rec1.canonical_key_hex == rec2.canonical_key_hex
    assert len(core.aliases) == 1  # one alias edge, no duplication


def test_new_signal_candle_yields_new_identity():
    core = enabled_core()
    rec1 = capture(core, legacy_signal_time=ISO_TIME)
    rec2 = core.capture_dual(
        SCANNER, "BTCUSDT", "SHORT", "5m", CANDLE_NEXT,
        legacy_signal_time="2026-11-25T10:05:07.123456+00:00",
    )
    assert rec2.stable_source_signal_id != rec1.stable_source_signal_id
    assert len(core.aliases) == 2
    assert core.resolve_legacy(rec1.legacy_source_signal_id) == rec1.stable_source_signal_id
    assert core.resolve_legacy(rec2.legacy_source_signal_id) == rec2.stable_source_signal_id


def test_legacy_id_collision_across_candles_is_rejected_not_repointed():
    # The frozen legacy key does not cover the candle; if the same legacy id is
    # ever derived for a DIFFERENT candle, the append-only contract wins: the
    # capture is rejected (AliasIntegrityError), never a silent re-point.
    core = enabled_core()
    rec1 = capture(core, legacy_signal_time=ISO_TIME)
    with pytest.raises(AliasIntegrityError):
        core.capture_dual(
            SCANNER, "BTCUSDT", "SHORT", "5m", CANDLE_NEXT,
            legacy_signal_time=ISO_TIME,
        )
    assert len(core.aliases) == 1
    assert core.resolve_legacy(rec1.legacy_source_signal_id) == rec1.stable_source_signal_id


def test_historical_alias_matching_keeps_frozen_oos_readable():
    # A frozen OOS row written by a historical process keeps its exact legacy
    # id; the alias edge makes it reachable from the stable key space while the
    # value itself is never rewritten.
    frozen_legacy = 1_234_567_890
    core = enabled_core()
    rec = capture(core, legacy_source_signal_id=frozen_legacy)
    assert rec.legacy_source_signal_id == frozen_legacy
    assert rec.stable_source_signal_id == stable_source_signal_id(
        SCANNER, "BTCUSDT", "SHORT", "5m", CANDLE
    )
    assert rec.stable_source_signal_id < -(2**31)  # disjoint from the legacy band
    assert 0 <= frozen_legacy < 2**31


# --- DEFAULT-OFF / off-scope behaviour unchanged --------------------------

def test_default_off_with_missing_legacy_input_stays_a_silent_noop():
    core = MigrationIdentityCore()  # PHASE-0, default-OFF
    assert core.config.phase == PHASE_0_DEFAULT_OFF
    assert capture(core) is None  # no raise: off-scope never mints, never rejects
    assert capture(core, legacy_signal_time="garbage") is None
    assert len(core.aliases) == 0


def test_disabled_family_with_missing_legacy_input_stays_a_noop():
    core = MigrationIdentityCore(MigrationConfig(
        phase=PHASE_1_SHADOW_DUAL_IDENTITY,
        families_enabled=frozenset({"srr"}),
        dual_write=True,
    ))
    assert core.capture_dual(
        "MOMENTUM_EXHAUSTION", "BTCUSDT", "LONG", "5m", CANDLE
    ) is None  # not enabled in this deployment
    assert len(core.aliases) == 0


def test_unknown_candle_lineage_still_fails_closed_with_none():
    core = enabled_core()
    assert core.capture_dual(
        SCANNER, "BTCUSDT", "SHORT", "5m", 0, legacy_signal_time=ISO_TIME
    ) is None
    assert len(core.aliases) == 0
