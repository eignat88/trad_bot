"""Scanner Signal Identity Migration V1 — Issue #322 (DEFAULT-OFF).

A migration-capable, backward-compatible global signal-identity core for every
scanner family that still uses the unstable ``hash()``-based identity of the
frozen ``ProspectiveOOSObserver._make_source_key()``
(``app/research/prospective_observer.py:239-244``).  Design document:
``docs/research/322_global_signal_identity_migration_v1.md``.

Hard guarantees of this module
------------------------------
* **DEFAULT-OFF**: :class:`MigrationConfig` ships with ``phase=PHASE_0``,
  no families enabled, no dual-write, no read-migration, no cutover.  The
  configuration surface is a frozen dataclass / module constants — deliberately
  NOT env wiring and NOT imported by ``scanner_runner.py`` or any production
  module (asserted by ``tests/test_issue322_global_identity_migration_isolation_v1.py``).
* **All-new file**: this module never edits the frozen observer
  (``prospective_observer.py`` must stay byte-identical), the completed ME_SHORT
  adapter (``scanner_signal_integrity_observer.py``, ``issue322:candle:v1``),
  ``app/db/repository.py``, ``scanner_runner.py`` or any scanner.
* **No historical rewrites / no backfill / no retroactive re-keying**: legacy
  ``hash()`` ``source_signal_id`` values stay exactly as written and remain
  readable forever through the append-only alias map.  Persistence hooks here
  are ABSTRACT ONLY — no SQL, no migrations in this module.

Identity derivation (stable across processes and PYTHONHASHSEED values)
----------------------------------------------------------------------
``stable_source_signal_id = -(2**31 + 1 + int.from_bytes(sha256(payload)[:8], "big")
                             % (2**62 - 2**31))`` where ``payload`` is the compact
JSON array ``["issue322:<family-slug>:v1", scanner_name, symbol, direction,
entry_timeframe, signal_candle_open_time]`` — the same fold as the completed
ME_SHORT PR #323 key (``scanner_signal_integrity_observer.py:127-131``): negative,
strictly below ``-2**31``, inside signed BIGINT, disjoint from the legacy 31-bit
``hash()`` band.  The ``issue322:<family-slug>:v1`` namespace is the domain
separator: families can never collide.  Identity keys on
``signal_candle_open_time`` (ms epoch, ``app/scanners/models.py:86-89``) — never
on the cycle-dependent ``detected_at``.  All migration-owned timestamps use
microsecond precision (PostgreSQL ``timestamptz`` fidelity).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = [
    "PHASE_0_DEFAULT_OFF",
    "PHASE_1_SHADOW_DUAL_IDENTITY",
    "PHASE_2_READ_MIGRATION",
    "PHASE_3_CUTOVER",
    "FAMILY_SLUGS",
    "MigrationConfig",
    "DualIdentityRecord",
    "SignalIdentityAliasMap",
    "IdentityPersistenceHook",
    "MigrationIdentityCore",
    "IdentityError",
    "AliasIntegrityError",
    "family_slug",
    "canonical_payload",
    "canonical_key_hex",
    "stable_source_signal_id",
    "legacy_source_signal_id",
    "format_ts_us",
]

# ---------------------------------------------------------------------------
# Phases (frozen constants — the only rollout control surface)
# ---------------------------------------------------------------------------

#: PHASE-0 — code shipped, nothing captured, nothing written.  The default.
PHASE_0_DEFAULT_OFF = 0
#: PHASE-1 — shadow dual-identity capture: BOTH keys recorded, nothing consumed.
PHASE_1_SHADOW_DUAL_IDENTITY = 1
#: PHASE-2 — read-migration with verification (read-only use of stable keys).
PHASE_2_READ_MIGRATION = 2
#: PHASE-3 — per-family cutover of the dedup key space.  OWNER APPROVAL ONLY.
PHASE_3_CUTOVER = 3

_PHASES = frozenset(
    {PHASE_0_DEFAULT_OFF, PHASE_1_SHADOW_DUAL_IDENTITY, PHASE_2_READ_MIGRATION, PHASE_3_CUTOVER}
)

#: Namespace prefix shared by every family key (domain separation root).
NAMESPACE_PREFIX = "issue322"

# ---------------------------------------------------------------------------
# Family registry (frozen).  ``candle`` is ADOPTED VERBATIM from the completed
# ME_SHORT PR #323 key ``issue322:candle:v1`` — it is never re-keyed.
# ---------------------------------------------------------------------------

FAMILY_SLUGS: Mapping[str, str] = MappingProxyType(
    {
        "MOMENTUM_EXHAUSTION|SHORT": "candle",  # PR #323 — completed, adopted
        "MOMENTUM_EXHAUSTION|LONG": "me-long",
        "MOMENTUM_EXHAUSTION_R": "mer",
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1": "merl-v1",
        "SUPPORT_RESISTANCE_REACTION": "srr",
        "BREAKOUT_RETEST": "breakout-retest",
        "VOLATILITY_COMPRESSION": "vc",
        "LIQUIDITY_REVERSAL": "lr",
        "FVG_REACTION_LONG_LOCAL_STRUCT_V1": "fvg",
        "TREND_PULLBACK_V3": "tp-v3",
        "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1": "me-cl",
    }
)

#: Scope key that special-cases the ME_SHORT family (scanner + direction).
_ME_SHORT_SCOPE_KEY = "MOMENTUM_EXHAUSTION|SHORT"

# Fold into ``[-(2**62 - 2**31), -1]``-bounded values strictly below ``-2**31``,
# identical to PR #323 so the two key families never overlap.
_FOLD_MODULUS = 2**62 - 2**31
_FOLD_BASE = 2**31 + 1


class IdentityError(ValueError):
    """Invalid identity input (fail-closed: no identity is minted)."""


class AliasIntegrityError(ValueError):
    """An alias edge would rewrite history (append-only violation)."""


# ---------------------------------------------------------------------------
# Configuration surface (frozen dataclass — NOT env wiring)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationConfig:
    """Default-OFF rollout configuration for one migration deployment.

    Deliberately a frozen dataclass with conservative defaults instead of
    environment variables: enabling a phase for a family must be an explicit,
    code-reviewed constant change — never a runtime toggle that could flip the
    identity key space by accident.
    """

    phase: int = PHASE_0_DEFAULT_OFF
    #: Family slugs enabled for this deployment (empty = nothing runs).
    families_enabled: frozenset[str] = frozenset()
    #: PHASE-1: record BOTH identity spaces on capture.
    dual_write: bool = False
    #: PHASE-2: allow read-side resolution through the stable key space.
    read_migration: bool = False
    #: PHASE-3: cutover of the dedup key space (owner approval required).
    cutover: bool = False

    def __post_init__(self) -> None:
        if self.phase not in _PHASES:
            raise ValueError(f"unknown phase: {self.phase!r}")
        if self.cutover and not (self.dual_write and self.read_migration):
            raise ValueError("cutover requires dual_write and read_migration")
        if self.read_migration and not self.dual_write:
            raise ValueError("read_migration requires dual_write")
        if self.phase == PHASE_0_DEFAULT_OFF and (
            self.dual_write or self.read_migration or self.cutover
        ):
            raise ValueError("PHASE-0 is default-OFF: no capture flags may be set")
        if self.phase < PHASE_1_SHADOW_DUAL_IDENTITY and self.families_enabled:
            raise ValueError("families may only be enabled from PHASE-1 onward")
        if self.phase < PHASE_2_READ_MIGRATION and self.read_migration:
            raise ValueError("read_migration requires PHASE-2 or later")
        if self.phase < PHASE_3_CUTOVER and self.cutover:
            raise ValueError("cutover requires PHASE-3")

    @classmethod
    def default_off(cls) -> "MigrationConfig":
        return cls()


# ---------------------------------------------------------------------------
# Timestamps — microsecond precision (PostgreSQL timestamptz fidelity)
# ---------------------------------------------------------------------------


def format_ts_us(value: datetime) -> str:
    """ISO-8601 UTC with MICROSECOND precision for migration-owned timestamps.

    Identity never keys on timestamps, but every migration-owned record stores
    them at µs precision so values round-trip through ``timestamptz`` unchanged.
    """
    if not isinstance(value, datetime):
        raise IdentityError("INVALID_TIMESTAMP")
    if value.tzinfo is None or value.utcoffset() is None:
        raise IdentityError("NAIVE_TIMESTAMP")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Canonical key derivation
# ---------------------------------------------------------------------------


def family_slug(scanner_name: str, direction: str) -> str | None:
    """Return the family slug for a candidate, or ``None`` when unregistered.

    MOMENTUM_EXHAUSTION is scope-split by direction: SHORT is the completed
    PR #323 ``candle`` family; LONG is the separate ``me-long`` family.
    """
    if not all(
        isinstance(v, str) and v.strip() for v in (scanner_name, direction)
    ):
        return None
    if scanner_name == "MOMENTUM_EXHAUSTION":
        return FAMILY_SLUGS.get(f"{scanner_name}|{direction}")
    return FAMILY_SLUGS.get(scanner_name)


def _validated_fields(
    scanner_name: str,
    symbol: str,
    direction: str,
    entry_timeframe: str,
    signal_candle_open_time: int,
) -> None:
    if not all(
        isinstance(v, str) and v.strip()
        for v in (scanner_name, symbol, direction, entry_timeframe)
    ):
        raise IdentityError("MISSING_IDENTITY_FIELD")
    if type(signal_candle_open_time) is not int or signal_candle_open_time <= 0:
        raise IdentityError("INVALID_SIGNAL_CANDLE_OPEN_TIME")


def canonical_payload(
    slug: str,
    scanner_name: str,
    symbol: str,
    direction: str,
    entry_timeframe: str,
    signal_candle_open_time: int,
) -> bytes:
    """Canonical, domain-separated payload for one signal identity.

    Fixed element order, compact separators, UTF-8.  The first element
    ``issue322:<slug>:v1`` is the domain separator: per-family separation is
    structural, not accidental.
    """
    if not isinstance(slug, str) or not slug.strip():
        raise IdentityError("MISSING_FAMILY_SLUG")
    _validated_fields(scanner_name, symbol, direction, entry_timeframe, signal_candle_open_time)
    return json.dumps(
        [
            f"{NAMESPACE_PREFIX}:{slug}:v1",
            scanner_name,
            symbol,
            direction,
            entry_timeframe,
            signal_candle_open_time,
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def canonical_key_hex(
    slug: str,
    scanner_name: str,
    symbol: str,
    direction: str,
    entry_timeframe: str,
    signal_candle_open_time: int,
) -> str:
    """SHA-256 hex digest of the canonical payload (alias/report form)."""
    return hashlib.sha256(
        canonical_payload(
            slug, scanner_name, symbol, direction, entry_timeframe, signal_candle_open_time
        )
    ).hexdigest()


def stable_source_signal_id(
    scanner_name: str,
    symbol: str,
    direction: str,
    entry_timeframe: str,
    signal_candle_open_time: int,
    *,
    slug: str | None = None,
) -> int | None:
    """Restart-stable ``source_signal_id`` for one signal candle.

    Returns ``None`` (fail-closed — never mints a fresh identity) when the
    candle lineage is missing/invalid or the family is unregistered.  The value
    is identical across processes and PYTHONHASHSEED values and sits in the
    same BIGINT band as the completed ME_SHORT PR #323 key
    (``scanner_signal_integrity_observer.py:127-131``): negative, strictly
    below ``-2**31``, inside signed PostgreSQL BIGINT.
    """
    if slug is None:
        slug = family_slug(scanner_name, direction)
    if slug is None:
        return None
    try:
        payload = canonical_payload(
            slug, scanner_name, symbol, direction, entry_timeframe, signal_candle_open_time
        )
    except IdentityError:
        return None
    digest = hashlib.sha256(payload).digest()
    value = int.from_bytes(digest[:8], "big") % _FOLD_MODULUS
    return -(_FOLD_BASE + value)


def legacy_source_signal_id(
    scanner_name: str, symbol: str, direction: str, signal_time: Any
) -> int:
    """Byte-compatible replica of the FROZEN base key (for alias mapping only).

    ``-abs(hash(key_str)) % (2**31)`` exactly as
    ``app/research/prospective_observer.py:239-244``.  This value is
    PROCESS-DEPENDENT (PYTHONHASHSEED) and CYCLE-DEPENDENT (``signal_time`` is
    the cycle's ``ctx.evaluated_at``).  It exists only so the alias map can
    relate a freshly observed signal to legacy rows; it must never be used as a
    new identity.  Historical values can not be re-derived (the original hash
    seed is gone) — they remain readable through recorded aliases only.
    """
    key_str = f"{scanner_name}:{symbol}:{direction}:{signal_time}"
    return -abs(hash(key_str)) % (2**31)


# ---------------------------------------------------------------------------
# Dual-identity record (PHASE-1 shadow shape) — pure data, no SQL
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DualIdentityRecord:
    """One shadow-phase capture carrying BOTH identity spaces.

    ``legacy_source_signal_id`` keeps the pre-migration value (the dedup
    ``ON CONFLICT (experiment_id, source_signal_id)`` target stays unchanged
    during the shadow phase); ``stable_source_signal_id`` is the new canonical
    key, consumed by nothing until PHASE-2.
    """

    family_slug: str
    identity_version: str
    legacy_source_signal_id: int
    stable_source_signal_id: int
    canonical_key_hex: str
    scanner_name: str
    symbol: str
    direction: str
    entry_timeframe: str
    signal_candle_open_time: int
    #: µs-precision capture timestamp (never part of any identity).
    observed_at_us: str
    phase: int


# ---------------------------------------------------------------------------
# Alias map — pure in-memory, append-only, dual-read (persistence ABSTRACT)
# ---------------------------------------------------------------------------


class SignalIdentityAliasMap:
    """Append-only legacy↔stable alias mapping (pure in-memory).

    * dual-read: resolve a stable key to all known legacy keys and vice versa;
    * one stable key may own MANY legacy keys (one per historical process seed);
    * a legacy key is NEVER re-pointed: an attempt raises
      :class:`AliasIntegrityError` (retroactive re-keying is forbidden);
    * persisted aliases must go through :class:`IdentityPersistenceHook` —
      abstract only in V1 (no SQL / migrations ship with this module).
    """

    def __init__(self) -> None:
        self._legacy_to_stable: dict[int, int] = {}
        self._stable_to_legacy: dict[int, tuple[int, ...]] = {}
        self._edge_count = 0

    def add_alias(self, stable_key: int, legacy_key: int) -> None:
        if type(stable_key) is not int or type(legacy_key) is not int:
            raise AliasIntegrityError("alias keys must be int")
        existing = self._legacy_to_stable.get(legacy_key)
        if existing is not None and existing != stable_key:
            raise AliasIntegrityError(
                "append-only violation: legacy key already aliased to a "
                "different stable key"
            )
        if existing is None:
            self._legacy_to_stable[legacy_key] = stable_key
            current = self._stable_to_legacy.get(stable_key, ())
            if legacy_key not in current:
                self._stable_to_legacy[stable_key] = current + (legacy_key,)
                self._edge_count += 1

    def stable_for_legacy(self, legacy_key: int) -> int | None:
        return self._legacy_to_stable.get(legacy_key)

    def legacy_for_stable(self, stable_key: int) -> tuple[int, ...]:
        return self._stable_to_legacy.get(stable_key, ())

    def __len__(self) -> int:
        return self._edge_count


@runtime_checkable
class IdentityPersistenceHook(Protocol):
    """ABSTRACT persistence boundary (no SQL ships with this module).

    A future, separately-approved migration implements these against the real
    schema; until then every hook is optional and the core works in-memory.
    Implementations MUST be append-only (mirror :class:`SignalIdentityAliasMap`).
    """

    def save_alias(self, stable_key: int, legacy_key: int, family_slug: str) -> None:
        ...

    def save_dual_record(self, record: DualIdentityRecord) -> None:
        ...


# ---------------------------------------------------------------------------
# Migration core — default-OFF
# ---------------------------------------------------------------------------


class MigrationIdentityCore:
    """Default-OFF identity core for the global migration.

    With the default :class:`MigrationConfig` every entry point is a no-op:
    nothing is captured, nothing is written, no identity is minted.  Capture
    begins only for explicitly enabled family slugs from PHASE-1 onward.
    """

    def __init__(
        self,
        config: MigrationConfig | None = None,
        alias_map: SignalIdentityAliasMap | None = None,
        persistence: IdentityPersistenceHook | None = None,
    ) -> None:
        self._config = config if config is not None else MigrationConfig.default_off()
        self._aliases = alias_map if alias_map is not None else SignalIdentityAliasMap()
        self._persistence = persistence

    @property
    def config(self) -> MigrationConfig:
        return self._config

    @property
    def aliases(self) -> SignalIdentityAliasMap:
        return self._aliases

    def is_family_enabled(self, slug: str | None) -> bool:
        return (
            self._config.phase >= PHASE_1_SHADOW_DUAL_IDENTITY
            and slug is not None
            and slug in self._config.families_enabled
        )

    def capture_dual(
        self,
        scanner_name: str,
        symbol: str,
        direction: str,
        entry_timeframe: str,
        signal_candle_open_time: int,
        *,
        legacy_signal_time: Any = None,
        observed_at: datetime | None = None,
    ) -> DualIdentityRecord | None:
        """Shadow-phase dual-identity capture.  ``None`` when off-scope.

        Records BOTH identity spaces; consumes neither.  The legacy id mirrors
        the frozen base derivation (process-dependent — passed through as
        computed by the caller's process); the stable id is the canonical key.
        Default-OFF configuration returns ``None`` without minting anything.
        """
        slug = family_slug(scanner_name, direction)
        if not self.is_family_enabled(slug):
            return None
        stable = stable_source_signal_id(
            scanner_name,
            symbol,
            direction,
            entry_timeframe,
            signal_candle_open_time,
            slug=slug,
        )
        if stable is None:
            return None  # fail-closed: unknown candle lineage mints nothing
        legacy = legacy_source_signal_id(
            scanner_name, symbol, direction, legacy_signal_time
        )
        record = DualIdentityRecord(
            family_slug=slug,
            identity_version=f"{NAMESPACE_PREFIX}:{slug}:v1",
            legacy_source_signal_id=legacy,
            stable_source_signal_id=stable,
            canonical_key_hex=canonical_key_hex(
                slug, scanner_name, symbol, direction, entry_timeframe,
                signal_candle_open_time,
            ),
            scanner_name=scanner_name,
            symbol=symbol,
            direction=direction,
            entry_timeframe=entry_timeframe,
            signal_candle_open_time=signal_candle_open_time,
            observed_at_us=format_ts_us(
                observed_at if observed_at is not None else datetime.now(timezone.utc)
            ),
            phase=self._config.phase,
        )
        self._aliases.add_alias(stable, legacy)
        if self._persistence is not None:
            self._persistence.save_alias(stable, legacy, slug)
            self._persistence.save_dual_record(record)
        return record

    def resolve_stable(self, stable_key: int) -> tuple[int, ...]:
        """PHASE-2 dual-read: known legacy keys for a stable key."""
        return self._aliases.legacy_for_stable(stable_key)

    def resolve_legacy(self, legacy_key: int) -> int | None:
        """PHASE-2 dual-read: stable key for a known legacy key."""
        return self._aliases.stable_for_legacy(legacy_key)
