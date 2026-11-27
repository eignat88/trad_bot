"""Issue #322 — regression for dual_write shadow-capture safety gate.

Offline only: no network, SQL, scanner wiring or production state.
"""
import pytest

from app.research.scanner_signal_identity_migration_v1 import (
    PHASE_0_DEFAULT_OFF,
    PHASE_1_SHADOW_DUAL_IDENTITY,
    PHASE_2_READ_MIGRATION,
    PHASE_3_CUTOVER,
    MigrationConfig,
    MigrationIdentityCore,
)

CANDLE = 1795680000000


@pytest.mark.parametrize("phase", [
    PHASE_1_SHADOW_DUAL_IDENTITY,
    PHASE_2_READ_MIGRATION,
    PHASE_3_CUTOVER,
])
def test_enabled_family_without_dual_write_is_rejected(phase):
    with pytest.raises(ValueError, match="enabled families require dual_write"):
        MigrationConfig(
            phase=phase,
            families_enabled=frozenset({"srr"}),
            dual_write=False,
        )


def test_default_off_never_captures_or_calls_persistence():
    writes = []

    class TripwirePersistence:
        def save_identity_batch(self, batch):
            writes.append(batch)

    core = MigrationIdentityCore(
        MigrationConfig.default_off(),
        persistence=TripwirePersistence(),
    )
    assert core.config.phase == PHASE_0_DEFAULT_OFF
    assert not core.is_family_enabled("srr")
    assert core.capture_dual(
        "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", "5m", CANDLE
    ) is None
    assert writes == []
    assert len(core.aliases) == 0


def test_phase_one_without_enabled_families_stays_inert():
    core = MigrationIdentityCore(MigrationConfig(
        phase=PHASE_1_SHADOW_DUAL_IDENTITY,
        dual_write=False,
    ))
    assert not core.is_family_enabled("srr")
    assert core.capture_dual(
        "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", "5m", CANDLE
    ) is None
    assert len(core.aliases) == 0


def test_explicit_dual_write_and_selected_family_allow_shadow_capture():
    core = MigrationIdentityCore(MigrationConfig(
        phase=PHASE_1_SHADOW_DUAL_IDENTITY,
        families_enabled=frozenset({"srr"}),
        dual_write=True,
    ))
    assert core.is_family_enabled("srr")
    assert not core.is_family_enabled("vc")
    record = core.capture_dual(
        "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", "5m", CANDLE,
        legacy_signal_time="2026-11-25T10:00:00+00:00",
    )
    assert record is not None
    assert record.family_slug == "srr"
    assert record.stable_source_signal_id < -(2 ** 31)
    assert len(core.aliases) == 1
    assert core.capture_dual(
        "VOLATILITY_COMPRESSION", "BTCUSDT", "SHORT", "5m", CANDLE
    ) is None


def test_read_migration_and_cutover_require_preceding_flags():
    with pytest.raises(ValueError):
        MigrationConfig(
            phase=PHASE_2_READ_MIGRATION,
            families_enabled=frozenset({"srr"}),
            dual_write=False,
            read_migration=True,
        )
    with pytest.raises(ValueError):
        MigrationConfig(
            phase=PHASE_3_CUTOVER,
            families_enabled=frozenset({"srr"}),
            dual_write=True,
            read_migration=False,
            cutover=True,
        )
