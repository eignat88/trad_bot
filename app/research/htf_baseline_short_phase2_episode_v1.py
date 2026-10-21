"""Issue #176 Phase-2 unified stateful episode machine (Blocker B4).

One identity rule, one lifecycle for both comparison and full-universe modes:

    episode_id = sha256(experiment_id|symbol|direction|level_id|first_touch_open_ms)

Identity depends ONLY on immutable causal parameters — never on mutable run
counters (fixes audit defect warmup-sensitive single-level identity), checkpoint
generation, restart counts or rolling-window indices.

Lifecycle semantics are exactly the audited multi-level semantics and preserve
the non-regressable P2-06..P2-11 behavior:

* P2-06/P2-09: a reaction confirmation is NOT suppressed merely because the
  confirming bar also touches the level while the contact run is active;
* P2-10: a same-step recontact supersedes the pending episode to
  UNRESOLVED/RECONTACT_SUPERSEDED_PENDING and the same-step confirmation is
  rejected (never emitted, never attributed);
* recontact after a broken run creates a NEW episode;
* pending may confirm after the contact run ends (FIRST_CONFIRM), no timeout;
* terminal episodes are never re-attributed; duplicate confirmation raises;
* each 5m step is atomic (deepcopy-commit);
* deterministic ordering (levels processed in sorted order).

Checkpoint format is versioned (``HTF_P2_EPISODE_STATE_V1``). Legacy diagnostic
checkpoints (``HTF_176_COMBINED_CHECKPOINT_V2`` and anything without the Phase-2
schema) are REJECTED explicitly — never silently reinterpreted.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256

STEP_MS = 300_000

STATE_SCHEMA = "HTF_P2_EPISODE_STATE_V1"
LEGACY_SCHEMA_REJECTED = "HTF_176_COMBINED_CHECKPOINT_V2"

STATE_CONTACT = "CONTACT"
STATE_CONFIRMED = "CONFIRMED"
STATE_UNRESOLVED = "UNRESOLVED"

REASON_CONFIRMED = "CAUSAL_REACTION_CONFIRMED"
REASON_RECONTACT_SUPERSEDED = "RECONTACT_SUPERSEDED_PENDING"
REASON_SOURCE_EVICTED = "SOURCE_WINDOW_EVICTED"

FIRST_CONFIRM_SUFFIX = ":FIRST_CONFIRM"


class LegacyCheckpointError(ValueError):
    """Raised when a checkpoint is not in the Phase-2 schema."""


def episode_id_for(
    experiment_id: str, symbol: str, direction: str, level_id: str, first_touch_open_ms: int
) -> str:
    raw = f"{experiment_id}|{symbol}|{direction}|{level_id}|{first_touch_open_ms}"
    return sha256(raw.encode("utf-8")).hexdigest()


def source_event_id_for(episode_id: str) -> str:
    return f"{episode_id}{FIRST_CONFIRM_SUFFIX}"


@dataclass
class Phase2Episodes:
    experiment_id: str
    symbol: str
    direction: str = "SHORT"
    last_close_ms: int | None = None
    episodes: dict = field(default_factory=dict)
    active: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    eligibility_loss_records: list = field(default_factory=list)
    known_level_ids: set = field(default_factory=set)

    # ------------------------------------------------------------------ state
    def checkpoint(self) -> dict:
        return {
            "schema": STATE_SCHEMA,
            "experiment_id": self.experiment_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "last_close_ms": self.last_close_ms,
            "episodes": deepcopy(self.episodes),
            "active": dict(self.active),
            "pending": dict(self.pending),
            "events": deepcopy(self.events),
            "eligibility_loss_records": deepcopy(self.eligibility_loss_records),
            "known_level_ids": sorted(self.known_level_ids),
        }

    @classmethod
    def restore(cls, data: dict) -> "Phase2Episodes":
        if not isinstance(data, dict):
            raise LegacyCheckpointError("checkpoint payload must be a mapping")
        schema = data.get("schema")
        if schema != STATE_SCHEMA:
            if schema == LEGACY_SCHEMA_REJECTED:
                raise LegacyCheckpointError(
                    f"legacy checkpoint schema {schema!r} rejected; "
                    f"expected {STATE_SCHEMA!r} (explicit migration required)"
                )
            raise LegacyCheckpointError(
                f"unknown checkpoint schema {schema!r}; expected {STATE_SCHEMA!r}"
            )
        if data.get("direction") != "SHORT":
            raise LegacyCheckpointError("phase-2 checkpoint must be SHORT")
        restored = cls(
            experiment_id=data["experiment_id"],
            symbol=data["symbol"],
            direction=data["direction"],
            last_close_ms=data.get("last_close_ms"),
            episodes=deepcopy(data.get("episodes", {})),
            active=dict(data.get("active", {})),
            pending=dict(data.get("pending", {})),
            events=deepcopy(data.get("events", [])),
            eligibility_loss_records=deepcopy(data.get("eligibility_loss_records", [])),
            known_level_ids=set(data.get("known_level_ids", ())),
        )
        restored._verify_invariants()
        return restored

    def _verify_invariants(self) -> None:
        for level_id, eid in self.pending.items():
            record = self.episodes.get(eid)
            if record is None:
                raise LegacyCheckpointError(f"pending references unknown episode {eid}")
            if record["state"] != STATE_CONTACT:
                raise LegacyCheckpointError(f"pending episode {eid} is not CONTACT")
            if record["level_id"] != level_id:
                raise LegacyCheckpointError(f"pending level mismatch for {eid}")
        # the active map is an authoritative P2-09 gate: a forged entry would
        # wrongly suppress re-contact episodes, so every entry must resolve to
        # a real, matching, non-terminal-UNRESOLVED episode
        for level_id, eid in self.active.items():
            record = self.episodes.get(eid)
            if record is None:
                raise LegacyCheckpointError(f"active references unknown episode {eid}")
            if record["level_id"] != level_id:
                raise LegacyCheckpointError(f"active level mismatch for {eid}")
            if record["state"] not in (STATE_CONTACT, STATE_CONFIRMED):
                raise LegacyCheckpointError(f"active episode {eid} is {record['state']}")
        for eid, record in self.episodes.items():
            expected = episode_id_for(
                self.experiment_id, self.symbol, self.direction,
                record["level_id"], record["first_touch_open_ms"],
            )
            if expected != eid:
                raise LegacyCheckpointError(f"episode identity mismatch for {eid}")
            if record["warmup_origin"] not in (True, False):
                raise LegacyCheckpointError(f"invalid warmup_origin for {eid}")

    # ---------------------------------------------------------------- advance
    def advance(
        self,
        *,
        open_ms: int,
        level_contacts: dict,
        confirmations=(),
        unavailable=(),
        eligibility_loss=(),
        warmup: bool = False,
    ) -> list[dict]:
        working = deepcopy(self)
        emitted = working._advance_mutating(
            open_ms=open_ms,
            level_contacts=level_contacts,
            confirmations=tuple(confirmations),
            unavailable=tuple(unavailable),
            eligibility_loss=tuple(eligibility_loss),
            warmup=warmup,
        )
        self.__dict__.update(working.__dict__)
        return emitted

    def _advance_mutating(
        self, *, open_ms, level_contacts, confirmations, unavailable, eligibility_loss, warmup
    ) -> list[dict]:
        if open_ms % STEP_MS:
            raise ValueError("unaligned 5m candle")
        if self.last_close_ms is not None:
            if open_ms < self.last_close_ms:
                raise ValueError("overlapping 5m bar")
            if open_ms > self.last_close_ms:
                raise ValueError("5m gap")

        lost_levels = set(eligibility_loss)
        for level_id in sorted(lost_levels):
            if level_id not in self.known_level_ids:
                raise ValueError("eligibility loss without known level")
            self.eligibility_loss_records.append({
                "level_id": level_id,
                "asof_ms": open_ms + STEP_MS,
                "pending": self.pending.get(level_id),
            })

        for key, (price, available_ms, touched) in level_contacts.items():
            if not key or price <= 0 or available_ms > open_ms:
                raise ValueError("level not available at candle open")

        valid = set(level_contacts)
        self.known_level_ids.update(valid)
        # P2-10: levels whose touch this step begins a NEW run (re-contact or
        # first contact) must not absorb a same-step reaction confirmation.
        recontacted = set()
        close_ms = open_ms + STEP_MS

        # deterministic ordering regardless of caller dict order
        for key in sorted(level_contacts):
            price, available_ms, touched = level_contacts[key]
            if touched:
                if key not in self.active:
                    recontacted.add(key)
                    self._open_episode(key, price, available_ms, open_ms, warmup, close_ms)
            else:
                self.active.pop(key, None)
        for key in list(self.active):
            if key not in valid:
                self.active.pop(key, None)

        for key in sorted(set(unavailable)):
            if key in recontacted:
                # fix to driver-equivalent semantics: a same-step re-contact
                # episode is fresh and must not be evicted as stale (audit R1)
                continue
            eid = self.pending.pop(key, None)
            if eid is None:
                raise ValueError("unavailable without pending episode")
            self.episodes[eid]["state"] = STATE_UNRESOLVED
            self.episodes[eid]["terminal_reason"] = REASON_SOURCE_EVICTED
            self.episodes[eid]["terminal_at_ms"] = close_ms
            self.active.pop(key, None)

        emitted: list[dict] = []
        for key in sorted(set(confirmations)):
            # P2-10: reject a same-step confirmation for a level whose touch this
            # step began a new run. It must not be attributed to the re-contact
            # episode; the prior pending is already superseded above.
            if key in recontacted:
                continue
            eid = self.pending.get(key)
            if not eid:
                raise ValueError("confirmation without pending episode")
            record = self.episodes[eid]
            if record["state"] == STATE_UNRESOLVED:
                raise ValueError("cannot confirm unresolved episode")
            if not (record["level_available_ms"] <= record["first_touch_open_ms"] < close_ms):
                raise ValueError("noncausal level/touch/confirmation order")
            if record["first_confirmation_ms"] is not None:
                raise ValueError("duplicate confirmation")
            record["first_confirmation_ms"] = close_ms
            record["state"] = STATE_CONFIRMED
            record["terminal_reason"] = REASON_CONFIRMED
            record["terminal_at_ms"] = close_ms
            event = {
                "event_id": eid,
                "source_event_id": source_event_id_for(eid),
                "episode_id": eid,
                "experiment_id": self.experiment_id,
                "symbol": self.symbol,
                "direction": self.direction,
                "level_id": key,
                "known_at_ms": close_ms,
                "observed_at_ms": close_ms,
                "first_touch_open_ms": record["first_touch_open_ms"],
                "level_available_ms": record["level_available_ms"],
                "level_price_at_touch": record["level_price_at_touch"],
                "warmup_origin": record["warmup_origin"],
            }
            emitted.append(event)
            self.events.append(event)
            self.pending.pop(key, None)
        self.last_close_ms = close_ms
        return emitted

    def _open_episode(
        self, key: str, price: float, available_ms: int, open_ms: int, warmup: bool, close_ms: int
    ) -> None:
        eid = episode_id_for(self.experiment_id, self.symbol, self.direction, key, open_ms)
        if eid in self.episodes:
            raise ValueError("duplicate causal episode identity")
        self.episodes[eid] = {
            "episode_id": eid,
            "level_id": key,
            "first_touch_open_ms": open_ms,
            "level_price_at_touch": price,
            "level_available_ms": available_ms,
            "first_confirmation_ms": None,
            "state": STATE_CONTACT,
            "terminal_reason": None,
            "terminal_at_ms": None,
            "warmup_origin": bool(warmup),
        }
        self.active[key] = eid
        # P2-10: previous pending episode is superseded to UNRESOLVED, never
        # erased and never orphaned in CONTACT.
        previous = self.pending.get(key)
        if previous and previous != eid:
            self.episodes[previous]["state"] = STATE_UNRESOLVED
            self.episodes[previous]["terminal_reason"] = REASON_RECONTACT_SUPERSEDED
            self.episodes[previous]["terminal_at_ms"] = close_ms
            self.pending.pop(key, None)
        self.pending[key] = eid

    # ------------------------------------------------------------- utilities
    @property
    def warmup_episode_ids(self) -> set:
        return {
            eid for eid, record in self.episodes.items() if record.get("warmup_origin")
        }

    @property
    def pending_at_end(self) -> int:
        return len(self.pending)
