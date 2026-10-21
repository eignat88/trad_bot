"""#176 research-only causal episode state machine, no exchange/DB/trading calls.

Caller supplies *one* fully closed 5m bar and its causally available level
snapshot per step. Level formation/eligibility is an upstream responsibility;
this module never rewrites a prior event. JSON snapshot supports offline restart.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

STEP_MS = 300_000


@dataclass(frozen=True)
class LevelSnapshot:
    level_id: str
    price: float
    available_ms: int


@dataclass
class EpisodeState:
    symbol: str
    direction: str = "SHORT"
    last_close_ms: int | None = None
    sequence: dict[str, int] = field(default_factory=dict)
    active: dict[str, str] = field(default_factory=dict)
    pending: dict[str, str] = field(default_factory=dict)
    episodes: dict[str, dict] = field(default_factory=dict)
    emitted: list[dict] = field(default_factory=list)

    def snapshot(self) -> dict:
        return {
            "symbol": self.symbol, "direction": self.direction,
            "last_close_ms": self.last_close_ms,
            "sequence": self.sequence, "active": self.active,
            "pending": self.pending,
            "episodes": self.episodes, "emitted": self.emitted,
        }

    @classmethod
    def restore(cls, data: dict) -> "EpisodeState":
        if not data.get("symbol"):
            raise ValueError("snapshot missing symbol")
        return cls(**data)

    def advance(self, *, open_ms: int, high: float, low: float,
                level: LevelSnapshot | None, touch: bool,
                reaction_confirmed: bool = False) -> list[dict]:
        """Advance exactly once on the latest closed bar.

        touch and reaction_confirmed MUST come from a causal external detector.
        A confirmation is permitted only for an already-observed active episode.
        The step is atomic (P2-10): it is applied to a copy and committed only
        on success, so a rejected/invalid step never corrupts recoverable state.
        """
        working = deepcopy(self)
        emitted = working._advance_mutating(
            open_ms=open_ms, high=high, low=low, level=level, touch=touch,
            reaction_confirmed=reaction_confirmed,
        )
        self.__dict__.update(working.__dict__)
        return emitted

    def _advance_mutating(self, *, open_ms, high, low, level, touch,
                          reaction_confirmed):
        if open_ms % STEP_MS:
            raise ValueError("unaligned 5m candle")
        close_ms = open_ms + STEP_MS
        if self.last_close_ms is not None:
            if close_ms <= self.last_close_ms:
                raise ValueError("overlapping or repeated bar; use persisted cursor")
            if close_ms != self.last_close_ms + STEP_MS:
                raise ValueError("missing 5m bar; halt until gap resolved")
        if not (0 < low <= high):
            raise ValueError("invalid candle range")
        if touch and (level is None or not level.level_id or
                      not 0 < level.price or level.available_ms > open_ms):
            raise ValueError("touch without causally available level")
        if reaction_confirmed and not level:
            raise ValueError("confirmation without level")
        key = level.level_id if level else None
        if reaction_confirmed and self.pending.get(key) is None:
            raise ValueError("confirmation without previously observed contact")
        emitted: list[dict] = []
        # P2-10: a same-step re-contact (a touch that begins a NEW run because
        # the prior run ended) must not hijack the pending identity before the
        # reaction confirmation is validated. Reject that combination: the
        # confirmation is neither emitted nor attributed to the re-contact.
        is_recontact = bool(touch) and key not in self.active
        if reaction_confirmed and is_recontact:
            reaction_confirmed = False
        # End active contact runs on first non-contact bar. This is a structural
        # event, not a timer-based cooldown; terminated episodes remain retained.
        for active_key in list(self.active):
            if not touch or active_key != key:
                self.active.pop(active_key)
        if touch:
            if key not in self.active:
                count = self.sequence.get(key, 0) + 1
                eid = sha256(
                    f"{self.symbol}|SHORT|{key}|{count}|{open_ms}".encode()
                ).hexdigest()
                self.sequence[key] = count
                record = {
                    "episode_id": eid, "level_id": key,
                    "level_price_at_touch": level.price,
                    "first_touch_open_ms": open_ms,
                    "level_available_ms": level.available_ms,
                    "first_confirmation_ms": None,
                    "state": "CONTACT",
                }
                self.episodes[eid] = record
                self.active[key] = eid
                # P2-10: supersede the prior pending episode rather than
                # orphaning it in CONTACT. It is terminal and never re-attributed.
                previous = self.pending.get(key)
                if previous and previous != eid:
                    self.episodes[previous]["state"] = "UNRESOLVED"
                    self.episodes[previous]["terminal_reason"] = "RECONTACT_SUPERSEDED_PENDING"
                    self.episodes[previous]["terminal_at_ms"] = close_ms
                self.pending[key] = eid
        if reaction_confirmed:
            eid = self.pending[key]
            record = self.episodes[eid]
            if record["first_confirmation_ms"] is None:
                record["first_confirmation_ms"] = close_ms
                record["state"] = "CONFIRMED"
                emitted.append({
                    "event_id": eid, "episode_id": eid, "level_id": key,
                    "known_at_ms": close_ms, "observed_at_ms": close_ms,
                })
                self.emitted.extend(emitted)
                self.pending.pop(key, None)
        self.last_close_ms = close_ms
        return emitted
