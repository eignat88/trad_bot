"""Issue #322 — ME_SHORT legacy candle identity compatibility (review P2 B4).

Family-aware legacy identity for the ME_SHORT ``candle`` family (PR #323
``issue322:candle:v1``):

* confirmed negative candle ids are accepted VERBATIM and never replaced by a
  positive ``hash()`` id;
* confirmed positive historical ``hash()`` ids stay valid in the confirmed
  (persisted) context;
* without a CONFIRMED identity ME_SHORT fails closed — ``legacy_signal_time``-
  based hash derivation would fabricate an id unrelated to the persisted
  candle id;
* negative ids are rejected for every non-candle family;
* no fallback id (now/0/epoch) is ever minted; conflicting lineage is rejected
  before any write.

Scenario table T01–T12 plus malformed/naive/missing-time and band-boundary
coverage.  Offline: no database, no network, no production writes.
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from app.research.scanner_signal_identity_migration_v1 import (
    CANDLE_KEY_BAND_MAX,
    CANDLE_KEY_BAND_MIN,
    LEGACY_HASH_BAND_MAX,
    PHASE_0_DEFAULT_OFF,
    PHASE_1_SHADOW_DUAL_IDENTITY,
    AliasEdge,
    AliasIntegrityError,
    IdentityBatch,
    IdentityError,
    IdentityPersistenceError,
    MigrationConfig,
    MigrationIdentityCore,
    DualIdentityRecord,
    SignalIdentityAliasMap,
    is_candle_key,
    is_legacy_hash_id,
    legacy_source_signal_id,
    resolve_legacy_identity,
    stable_source_signal_id,
)
from app.research.scanner_signal_integrity_observer import (
    ScannerSignalIntegrityObserver,
)

ROOT = Path(__file__).resolve().parents[1]

SCANNER = "MOMENTUM_EXHAUSTION"
DIRECTION = "SHORT"
TF = "5m"
CANDLE = 1795680000000
CANDLE_NEXT = 1795680300000
ISO_TIME = "2026-11-25T10:00:07.123456+00:00"


def candle_id(candle=CANDLE):
    """The real PR #323 candle key via the shipped adapter implementation."""
    return ScannerSignalIntegrityObserver._make_candle_source_key(
        SCANNER, "BTCUSDT", DIRECTION, TF, candle
    )


def stable(candle=CANDLE):
    return stable_source_signal_id(
        SCANNER, "BTCUSDT", DIRECTION, TF, candle, slug="candle"
    )


def core(persistence=None, alias_map=None):
    return MigrationIdentityCore(
        MigrationConfig(
            phase=PHASE_1_SHADOW_DUAL_IDENTITY,
            families_enabled=frozenset({"candle"}),
            dual_write=True,
        ),
        alias_map=alias_map,
        persistence=persistence,
    )


def capture(cores, *, candle=CANDLE, **kwargs):
    return cores.capture_dual(
        SCANNER, "BTCUSDT", DIRECTION, TF, candle, **kwargs
    )


class TripwireHook:
    """Atomic-contract tripwire: reaching persistence is a test failure."""

    def __init__(self):
        self.calls = 0

    def save_identity_batch(self, batch):
        self.calls += 1
        raise AssertionError("persistence must not be reached")


class AtomicStore:
    """Minimal ALL_OR_NOTHING idempotent store (fault-injectable)."""

    def __init__(self):
        self.edges = {}
        self.records = {}
        self.calls = 0
        self.fail = False

    def save_identity_batch(self, batch):
        self.calls += 1
        if batch.idempotency_key in self.records:
            return
        if self.fail:
            raise RuntimeError("write failed")
        for edge in batch.alias_edges:
            self.edges[(edge.stable_key, edge.legacy_key)] = edge.family_slug
        self.records[batch.idempotency_key] = batch.record

    def rebuild_alias_map(self):
        restored = SignalIdentityAliasMap()
        for (stable_key, legacy_key) in self.edges:
            restored.add_alias(stable_key, legacy_key)
        return restored


# --- parity: PR #323 candle key == migration canonical key -----------------

def test_candle_key_parity_with_pr323_adapter():
    # The compatibility anchor: for the SAME lineage the PR #323 candle key and
    # the migration stable key are one and the same deterministic value.
    assert candle_id() == stable()
    assert candle_id() == stable_source_signal_id(
        SCANNER, "BTCUSDT", DIRECTION, TF, CANDLE
    )  # auto-derived slug ("candle") matches
    assert candle_id(CANDLE_NEXT) == stable(CANDLE_NEXT)
    assert candle_id() != candle_id(CANDLE_NEXT)


def test_candle_key_band_matches_pr323_contract():
    value = candle_id()
    assert is_candle_key(value)
    assert not is_legacy_hash_id(value)
    assert CANDLE_KEY_BAND_MIN <= value <= CANDLE_KEY_BAND_MAX
    assert value < -(2**31) and -(2**63) <= value  # inside signed BIGINT


# --- T01: ME_SHORT with confirmed negative candle ID ----------------------

def test_t01_confirmed_negative_candle_id_is_preserved():
    cores = core()
    rec = capture(cores, legacy_source_signal_id=candle_id())
    assert rec.legacy_source_signal_id == candle_id()  # preserved verbatim
    assert rec.legacy_source_signal_id < 0
    assert rec.stable_source_signal_id == stable()
    assert cores.resolve_legacy(candle_id()) == stable()
    assert cores.resolve_stable(stable()) == (candle_id(),)


def test_t01_negative_candle_id_is_never_replaced_by_hash():
    # The P2 defect: passing the persisted candle id must not be rejected nor
    # silently re-derived into a positive hash id.
    rec = capture(core(), legacy_source_signal_id=candle_id())
    assert rec.legacy_source_signal_id != legacy_source_signal_id(
        SCANNER, "BTCUSDT", DIRECTION, ISO_TIME
    )
    assert is_candle_key(rec.legacy_source_signal_id)


# --- T02: ME_SHORT with positive historical hash ID (confirmed context) ---

def test_t02_confirmed_positive_historical_hash_id_passes():
    historical = legacy_source_signal_id(SCANNER, "BTCUSDT", DIRECTION, ISO_TIME)
    assert 0 <= historical < LEGACY_HASH_BAND_MAX
    cores = core()
    rec = capture(cores, legacy_source_signal_id=historical)
    assert rec.legacy_source_signal_id == historical  # verbatim
    assert rec.stable_source_signal_id == stable()  # canonical key unchanged
    assert cores.resolve_legacy(historical) == stable()


def test_t02_arbitrary_confirmed_positive_id_passes():
    rec = capture(core(), legacy_source_signal_id=777_000_123)
    assert rec.legacy_source_signal_id == 777_000_123


# --- T03: ME_SHORT without confirmed identity -> FAIL-CLOSED --------------

def test_t03_without_confirmed_identity_fails_closed():
    hook = TripwireHook()
    cores = core(persistence=hook)
    with pytest.raises(IdentityError, match="CONFIRMED_LEGACY_ID_REQUIRED"):
        capture(cores, legacy_signal_time=ISO_TIME)  # valid time must NOT mint
    with pytest.raises(IdentityError, match="CONFIRMED_LEGACY_ID_REQUIRED"):
        capture(cores)  # nothing at all
    assert hook.calls == 0
    assert len(cores.aliases) == 0


@pytest.mark.parametrize("bad_time", [None, "", "garbage",
                                      datetime(2026, 11, 25, 10, 0, 7),
                                      1795680000000])
def test_t03_signal_time_is_never_derived_for_me_short(bad_time):
    # malformed/naive/missing time never produces an ME_SHORT identity either
    with pytest.raises(IdentityError):
        capture(core(), legacy_signal_time=bad_time)
    assert is_candle_key(candle_id())  # the candle id is lineage-derived only


# --- T04: negative ID for a different family -> REJECT --------------------

@pytest.mark.parametrize("bad", [-1, -(2**31 + 1), -2**40, candle_id()])
def test_t04_negative_ids_rejected_for_non_candle_families(bad):
    with pytest.raises(IdentityError, match="INVALID_LEGACY_SOURCE_SIGNAL_ID"):
        resolve_legacy_identity(
            "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT",
            legacy_source_signal_id=bad,
        )


def test_t04_negative_id_rejected_on_capture_path():
    with pytest.raises(IdentityError, match="INVALID_LEGACY_SOURCE_SIGNAL_ID"):
        MigrationIdentityCore(MigrationConfig(
            phase=PHASE_1_SHADOW_DUAL_IDENTITY,
            families_enabled=frozenset({"srr"}),
            dual_write=True,
        )).capture_dual(
            "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT", TF, CANDLE,
            legacy_source_signal_id=candle_id(),
        )


# --- T05: redelivery of a candle event -> no duplicates -------------------

def test_t05_candle_redelivery_is_idempotent():
    store = AtomicStore()
    cores = core(persistence=store)
    rec1 = capture(cores, legacy_source_signal_id=candle_id())
    rec2 = capture(cores, legacy_source_signal_id=candle_id())
    rec3 = capture(cores, legacy_source_signal_id=candle_id())
    assert rec1.canonical_key_hex == rec2.canonical_key_hex == rec3.canonical_key_hex
    assert len(store.records) == 1 and len(store.edges) == 1
    assert len(cores.aliases) == 1


# --- T06: legacy <-> canonical conflict -> REJECT -------------------------

def test_t06_conflicting_candle_lineage_is_rejected():
    # candle id of a DIFFERENT candle presented for this lineage
    hook = TripwireHook()
    cores = core(persistence=hook)
    with pytest.raises(IdentityError, match="CONFLICTING_LEGACY_CANDLE_ID"):
        capture(cores, legacy_source_signal_id=candle_id(CANDLE_NEXT))
    assert hook.calls == 0
    assert len(cores.aliases) == 0


def test_t06_alias_repointing_conflict_fails_closed():
    cores = core()
    rec = capture(cores, legacy_source_signal_id=candle_id())
    with pytest.raises(AliasIntegrityError):
        cores.aliases.add_alias(stable() - 1, rec.legacy_source_signal_id)
    assert cores.resolve_legacy(rec.legacy_source_signal_id) == rec.stable_source_signal_id


# --- T07: atomic persistence failure -> no partial write ------------------

def test_t07_persistence_failure_leaves_no_partial_state():
    store = AtomicStore()
    store.fail = True
    cores = core(persistence=store)
    with pytest.raises(IdentityPersistenceError):
        capture(cores, legacy_source_signal_id=candle_id())
    assert store.edges == {} and store.records == {}
    assert len(cores.aliases) == 0
    store.fail = False
    rec = capture(cores, legacy_source_signal_id=candle_id())
    assert set(store.edges) == {(rec.stable_source_signal_id, rec.legacy_source_signal_id)}
    assert len(cores.aliases) == 1


# --- T08: crash/restart replay -> identity unchanged ----------------------

def test_t08_restart_replay_preserves_identity():
    store = AtomicStore()
    rec1 = capture(core(persistence=store), legacy_source_signal_id=candle_id())
    # "restart": fresh core over the same store, alias map replayed
    cores2 = core(persistence=store, alias_map=store.rebuild_alias_map())
    assert cores2.resolve_legacy(rec1.legacy_source_signal_id) == rec1.stable_source_signal_id
    rec2 = capture(cores2, legacy_source_signal_id=candle_id())
    assert rec2.stable_source_signal_id == rec1.stable_source_signal_id
    assert rec2.legacy_source_signal_id == rec1.legacy_source_signal_id
    assert len(store.records) == 1 and len(store.edges) == 1  # no duplicates


# --- T09: different PYTHONHASHSEED -> canonical identity deterministic ----

_PROBE = """
import json
from app.research.scanner_signal_identity_migration_v1 import (
    legacy_source_signal_id, stable_source_signal_id,
)
from app.research.scanner_signal_integrity_observer import (
    ScannerSignalIntegrityObserver,
)
out = {
    "candle_stable": stable_source_signal_id(
        "MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "5m", 1795680000000),
    "candle_pr323": ScannerSignalIntegrityObserver._make_candle_source_key(
        "MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "5m", 1795680000000),
    "me_short_hash": legacy_source_signal_id(
        "MOMENTUM_EXHAUSTION", "BTCUSDT", "SHORT", "2026-11-25T10:00:07+00:00"),
}
print(json.dumps(out))
"""


def _probe(seed: str) -> dict:
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = seed
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(ROOT), env=env, capture_output=True, text=True, check=True,
        timeout=60,
    )
    import json

    return json.loads(completed.stdout.strip())


@pytest.fixture(scope="module")
def probes() -> dict:
    return {seed: _probe(seed) for seed in ("1", "2")}


def test_t09_canonical_identity_deterministic_across_hashseeds(probes):
    values = {probes[seed]["candle_stable"] for seed in probes}
    assert len(values) == 1
    assert next(iter(values)) == stable()  # == in-process derivation
    for seed in probes:
        assert probes[seed]["candle_pr323"] == probes[seed]["candle_stable"]
        assert is_candle_key(probes[seed]["candle_stable"])


def test_t09_legacy_hash_stays_process_dependent(probes):
    # sanity: the frozen hash id still varies per seed (that is the instability
    # the migration removes) while the candle identity does not
    assert probes["1"]["me_short_hash"] != probes["2"]["me_short_hash"]


# --- T10: default-OFF -> no capture, no persistence -----------------------

def test_t10_default_off_never_captures():
    hook = TripwireHook()
    cores = MigrationIdentityCore(MigrationConfig.default_off(), persistence=hook)
    assert cores.config.phase == PHASE_0_DEFAULT_OFF
    assert capture(cores, legacy_source_signal_id=candle_id()) is None
    assert capture(cores, legacy_signal_time=ISO_TIME) is None
    assert capture(cores, legacy_source_signal_id=-1) is None  # still no raise
    assert hook.calls == 0
    assert len(cores.aliases) == 0


# --- T11: dual_write=False -> capture forbidden ---------------------------

def test_t11_enabled_family_without_dual_write_is_rejected():
    with pytest.raises(ValueError, match="enabled families require dual_write"):
        MigrationConfig(
            phase=PHASE_1_SHADOW_DUAL_IDENTITY,
            families_enabled=frozenset({"candle"}),
            dual_write=False,
        )


def test_t11_phase_one_without_dual_write_captures_nothing():
    cores = MigrationIdentityCore(MigrationConfig(
        phase=PHASE_1_SHADOW_DUAL_IDENTITY, dual_write=False,
    ))
    assert not cores.is_family_enabled("candle")
    assert capture(cores, legacy_source_signal_id=candle_id()) is None
    assert len(cores.aliases) == 0


# --- T12: frozen PR #323 contracts untouched (run-level) ------------------

def test_t12_frozen_adapter_contract_is_untouched():
    adapter = ROOT / "app" / "research" / "scanner_signal_integrity_observer.py"
    text = adapter.read_text(encoding="utf-8")
    assert "issue322:candle:v1" in text
    assert "scanner_signal_identity_migration_v1" not in text
    # parity anchor doubles as the frozen-contract check (see test above)
    assert candle_id() == stable()


# --- ID type / band boundaries and malformed inputs -----------------------

@pytest.mark.parametrize("bad", ["123", True, 1.0, b"\x01", object()])
def test_invalid_confirmed_id_types_rejected_for_me_short(bad):
    with pytest.raises(IdentityError, match="INVALID_LEGACY_SOURCE_SIGNAL_ID"):
        capture(core(), legacy_source_signal_id=bad)


def test_candle_band_edges_accepted_at_resolution_level():
    for edge in (CANDLE_KEY_BAND_MIN, CANDLE_KEY_BAND_MAX):
        assert resolve_legacy_identity(
            SCANNER, "BTCUSDT", DIRECTION, legacy_source_signal_id=edge
        ) == edge


@pytest.mark.parametrize("bad", [
    -(2**31),          # exactly the band boundary above the candle band
    CANDLE_KEY_BAND_MIN - 1,
    LEGACY_HASH_BAND_MAX,
    2**63 - 1,
])
def test_out_of_band_ids_rejected_for_me_short(bad):
    with pytest.raises(IdentityError, match="INVALID_LEGACY_SOURCE_SIGNAL_ID"):
        resolve_legacy_identity(
            SCANNER, "BTCUSDT", DIRECTION, legacy_source_signal_id=bad
        )


def test_hash_band_edges_accepted_for_every_family():
    for value in (0, LEGACY_HASH_BAND_MAX - 1):
        assert resolve_legacy_identity(
            SCANNER, "BTCUSDT", DIRECTION, legacy_source_signal_id=value
        ) == value
        assert resolve_legacy_identity(
            "SUPPORT_RESISTANCE_REACTION", "BTCUSDT", "SHORT",
            legacy_source_signal_id=value,
        ) == value


def test_no_fallback_id_is_ever_minted_for_me_short():
    # no confirmed id -> explicit error, never a now()/0/epoch substitution
    for kwargs in ({}, {"legacy_signal_time": ISO_TIME},
                   {"legacy_signal_time": 0},
                   {"legacy_signal_time": "1970-01-01T00:00:00+00:00"}):
        with pytest.raises(IdentityError):
            resolve_legacy_identity(SCANNER, "BTCUSDT", DIRECTION, **kwargs)


# --- AliasEdge / IdentityBatch family-aware invariants --------------------

def test_alias_edge_accepts_candle_key_for_candle_family_only():
    legacy = candle_id()
    AliasEdge(stable(), legacy, "candle").validate()
    with pytest.raises(AliasIntegrityError, match="ALIAS_EDGE_LEGACY_KEY_INVALID"):
        AliasEdge(stable(), legacy, "srr").validate()
    with pytest.raises(AliasIntegrityError, match="ALIAS_EDGE_LEGACY_KEY_INVALID"):
        AliasEdge(stable(), -1, "candle").validate()
    AliasEdge(stable(), 123, "candle").validate()
    AliasEdge(stable(), 123, "srr").validate()


def test_identity_batch_with_candle_legacy_validates_atomically():
    record = DualIdentityRecord(
        family_slug="candle",
        identity_version="issue322:candle:v1",
        legacy_source_signal_id=candle_id(),
        stable_source_signal_id=stable(),
        canonical_key_hex="0" * 64,
        scanner_name=SCANNER,
        symbol="BTCUSDT",
        direction=DIRECTION,
        entry_timeframe=TF,
        signal_candle_open_time=CANDLE,
        observed_at_us="2026-11-25T10:00:07.123456+00:00",
        phase=PHASE_1_SHADOW_DUAL_IDENTITY,
    )
    batch = IdentityBatch(
        record=record,
        alias_edges=(AliasEdge(stable(), candle_id(), "candle"),),
    )
    batch.validate()
    # a candle-band edge under a foreign family slug is rejected at edge level
    with pytest.raises(AliasIntegrityError, match="ALIAS_EDGE_LEGACY_KEY_INVALID"):
        IdentityBatch(
            record=record,
            alias_edges=(AliasEdge(stable(), candle_id(), "srr"),),
        ).validate()
    # a valid hash-band edge whose slug differs from the record's is a mismatch
    with pytest.raises(AliasIntegrityError, match="BATCH_FAMILY_SLUG_MISMATCH"):
        IdentityBatch(
            record=record,
            alias_edges=(AliasEdge(stable(), 123, "srr"),),
        ).validate()
