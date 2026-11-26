"""Issue #322 global identity migration — legacy↔stable alias mapping (offline).

Pure in-memory structures only: append-only alias edges, dual-read both ways,
the PHASE-1 dual-identity record shape, and the default-OFF core behaviour.
No database, no network, no production writes (persistence hooks stay abstract
and are exercised only as in-test stubs).
"""

from datetime import datetime, timezone

import pytest

from app.research.scanner_signal_identity_migration_v1 import (
    PHASE_0_DEFAULT_OFF,
    PHASE_1_SHADOW_DUAL_IDENTITY,
    PHASE_2_READ_MIGRATION,
    PHASE_3_CUTOVER,
    AliasIntegrityError,
    DualIdentityRecord,
    MigrationConfig,
    MigrationIdentityCore,
    SignalIdentityAliasMap,
    format_ts_us,
    legacy_source_signal_id,
    stable_source_signal_id,
)

CANDLE = 1795680000000
OBSERVED = datetime(2026, 11, 25, 10, 0, 0, 123456, tzinfo=timezone.utc)


def stable(slug="srr"):
    scanner = "SUPPORT_RESISTANCE_REACTION" if slug == "srr" else "MOMENTUM_EXHAUSTION"
    direction = "SHORT"
    return stable_source_signal_id(scanner, "BTCUSDT", direction, "5m", CANDLE, slug=slug)


# --- alias map: append-only, dual-read -----------------------------------

def test_alias_dual_read_both_directions():
    m = SignalIdentityAliasMap()
    s = stable()
    m.add_alias(s, 111)
    assert m.stable_for_legacy(111) == s
    assert m.legacy_for_stable(s) == (111,)


def test_one_stable_key_many_legacy_keys():
    m = SignalIdentityAliasMap()
    s = stable()
    m.add_alias(s, 111)
    m.add_alias(s, 222)  # a second historical process seed
    assert sorted(m.legacy_for_stable(s)) == [111, 222]
    assert len(m) == 2


def test_alias_is_idempotent():
    m = SignalIdentityAliasMap()
    s = stable()
    m.add_alias(s, 111)
    m.add_alias(s, 111)
    assert len(m) == 1


def test_alias_repointing_is_rejected():
    m = SignalIdentityAliasMap()
    s1, s2 = stable("srr"), stable("vc")
    m.add_alias(s1, 111)
    with pytest.raises(AliasIntegrityError):
        m.add_alias(s2, 111)  # retroactive re-keying is forbidden
    assert m.stable_for_legacy(111) == s1  # unchanged


def test_alias_map_rejects_non_int():
    m = SignalIdentityAliasMap()
    with pytest.raises(AliasIntegrityError):
        m.add_alias("123", 111)
    with pytest.raises(AliasIntegrityError):
        m.add_alias(stable(), "111")


def test_unknown_lookups_are_empty():
    m = SignalIdentityAliasMap()
    assert m.stable_for_legacy(999) is None
    assert m.legacy_for_stable(999) == ()
    assert len(m) == 0


# --- dual-identity record shape (PHASE-1 shadow) --------------------------

def test_dual_identity_record_shape():
    core = MigrationIdentityCore(
        MigrationConfig(
            phase=PHASE_1_SHADOW_DUAL_IDENTITY,
            families_enabled=frozenset({"srr"}),
            dual_write=True,
        )
    )
    rec = core.capture_dual(
        "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", "5m", CANDLE,
        legacy_signal_time="2026-11-25 10:00:07+00:00", observed_at=OBSERVED,
    )
    assert isinstance(rec, DualIdentityRecord)
    assert rec.family_slug == "srr"
    assert rec.identity_version == "issue322:srr:v1"
    assert rec.stable_source_signal_id == stable()
    assert rec.legacy_source_signal_id == legacy_source_signal_id(
        "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", "2026-11-25 10:00:07+00:00"
    )
    assert rec.signal_candle_open_time == CANDLE
    assert rec.observed_at_us == "2026-11-25T10:00:00.123456+00:00"
    assert rec.observed_at_us == format_ts_us(OBSERVED)
    assert rec.phase == PHASE_1_SHADOW_DUAL_IDENTITY
    # BOTH identity spaces recorded; the stable key is disjoint from legacy band
    assert rec.stable_source_signal_id < -(2**31)
    assert 0 <= rec.legacy_source_signal_id < 2**31


def test_capture_registers_alias_edges():
    core = MigrationIdentityCore(
        MigrationConfig(
            phase=PHASE_1_SHADOW_DUAL_IDENTITY,
            families_enabled=frozenset({"srr"}),
            dual_write=True,
        )
    )
    rec = core.capture_dual(
        "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", "5m", CANDLE,
        legacy_signal_time=1795680000000,
    )
    assert core.resolve_legacy(rec.legacy_source_signal_id) == rec.stable_source_signal_id
    assert core.resolve_stable(rec.stable_source_signal_id) == (rec.legacy_source_signal_id,)


# --- default-OFF behaviour -----------------------------------------------

def test_default_config_is_phase_0_off():
    cfg = MigrationConfig.default_off()
    assert cfg.phase == PHASE_0_DEFAULT_OFF
    assert cfg.families_enabled == frozenset()
    assert cfg.dual_write is False
    assert cfg.read_migration is False
    assert cfg.cutover is False


def test_default_off_core_captures_nothing():
    core = MigrationIdentityCore()
    assert core.capture_dual(
        "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", "5m", CANDLE
    ) is None
    assert len(core.aliases) == 0
    assert core.is_family_enabled("srr") is False


def test_disabled_family_is_a_noop_even_in_phase_1():
    core = MigrationIdentityCore(
        MigrationConfig(
            phase=PHASE_1_SHADOW_DUAL_IDENTITY,
            families_enabled=frozenset({"srr"}),
            dual_write=True,
        )
    )
    assert core.capture_dual(
        "MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "5m", CANDLE
    ) is None  # candle family NOT enabled in this deployment


def test_capture_fails_closed_on_unknown_candle():
    core = MigrationIdentityCore(
        MigrationConfig(
            phase=PHASE_1_SHADOW_DUAL_IDENTITY,
            families_enabled=frozenset({"srr"}),
            dual_write=True,
        )
    )
    assert core.capture_dual(
        "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", "5m", 0
    ) is None
    assert len(core.aliases) == 0


# --- config surface consistency ------------------------------------------

def test_config_rejects_inconsistent_flags():
    with pytest.raises(ValueError):
        MigrationConfig(phase=PHASE_1_SHADOW_DUAL_IDENTITY, dual_write=True, read_migration=False, cutover=True)
    with pytest.raises(ValueError):
        MigrationConfig(phase=PHASE_1_SHADOW_DUAL_IDENTITY, dual_write=False, read_migration=True)
    with pytest.raises(ValueError):
        MigrationConfig(phase=PHASE_0_DEFAULT_OFF, dual_write=True)
    with pytest.raises(ValueError):
        MigrationConfig(phase=PHASE_2_READ_MIGRATION, dual_write=False, read_migration=True)
    with pytest.raises(ValueError):
        MigrationConfig(phase=99)
    with pytest.raises(ValueError):
        MigrationConfig(families_enabled=frozenset({"srr"}))  # phase 0 with families


def test_config_phase_gate_ordering():
    ok = MigrationConfig(
        phase=PHASE_3_CUTOVER,
        families_enabled=frozenset({"srr"}),
        dual_write=True,
        read_migration=True,
        cutover=True,
    )
    assert ok.cutover is True
    ok2 = MigrationConfig(
        phase=PHASE_2_READ_MIGRATION,
        families_enabled=frozenset({"srr"}),
        dual_write=True,
        read_migration=True,
    )
    assert ok2.cutover is False


# --- persistence hooks: abstract only ------------------------------------

def test_persistence_hook_stub_is_append_only_boundary():
    saved = {"aliases": [], "records": []}

    class StubHook:
        def save_alias(self, stable_key, legacy_key, family_slug):
            saved["aliases"].append((stable_key, legacy_key, family_slug))

        def save_dual_record(self, record):
            saved["records"].append(record)

    core = MigrationIdentityCore(
        MigrationConfig(
            phase=PHASE_1_SHADOW_DUAL_IDENTITY,
            families_enabled=frozenset({"srr"}),
            dual_write=True,
        ),
        persistence=StubHook(),
    )
    rec = core.capture_dual(
        "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", "5m", CANDLE,
        legacy_signal_time=1,
    )
    assert saved["aliases"] == [(rec.stable_source_signal_id, rec.legacy_source_signal_id, "srr")]
    assert saved["records"] == [rec]
