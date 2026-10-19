"""Issue #176 research-only multi-level pending repair.

Each level is advanced on every closed 5m bar. Confirmation can occur after
contact ends. No arbitrary timeout: unconfirmable pending remains UNRESOLVED.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from hashlib import sha256

STEP = 300000

@dataclass
class MultiLevelEpisodes:
    symbol: str
    last_close_ms: int | None = None
    episodes: dict = field(default_factory=dict)
    active: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    sequence: dict = field(default_factory=dict)

    def checkpoint(self):
        return {key: getattr(self, key) for key in
                ("symbol", "last_close_ms", "episodes", "active", "pending",
                 "events", "sequence")}

    @classmethod
    def restore(cls, data):
        return cls(**data)

    def advance(self, *, open_ms, level_contacts, confirmations=(),
                unavailable=()):
        """level_contacts: mapping level_id -> (price, available_ms, touched).
        confirmations: level IDs already proven by a strictly causal detector.
        unavailable: pending IDs whose original touch left the evidence window.
        """
        if open_ms % STEP:
            raise ValueError("unaligned 5m candle")
        if self.last_close_ms is not None:
            if open_ms < self.last_close_ms:
                raise ValueError("overlapping 5m bar")
            if open_ms > self.last_close_ms:
                raise ValueError("5m gap")
        valid = set(level_contacts)
        for level_id, (price, available_ms, touched) in level_contacts.items():
            if not level_id or price <= 0 or available_ms > open_ms:
                raise ValueError("level not available at candle open")
            if touched:
                if level_id not in self.active:
                    sequence = self.sequence.get(level_id, 0) + 1
                    self.sequence[level_id] = sequence
                    episode_id = sha256(
                        f"{self.symbol}|SHORT|{level_id}|{open_ms}".encode()
                    ).hexdigest()
                    if episode_id in self.episodes:
                        raise ValueError("duplicate causal episode identity")
                    self.episodes[episode_id] = {
                        "episode_id": episode_id, "level_id": level_id,
                        "first_touch_open_ms": open_ms,
                        "level_price_at_touch": price,
                        "level_available_ms": available_ms,
                        "first_confirmation_ms": None,
                        "state": "CONTACT",
                        "terminal_reason": None,
                    }
                    self.active[level_id] = episode_id
                    # Previous pending episode is unresolved rather than erased.
                    previous = self.pending.get(level_id)
                    if previous and previous != episode_id:
                        self.episodes[previous]["state"] = "UNRESOLVED"
                        self.episodes[previous]["terminal_reason"] = "RECONTACT_SUPERSEDED_PENDING"
                        self.episodes[previous]["terminal_at_ms"] = open_ms + STEP
                        self.pending.pop(level_id, None)
                    self.pending[level_id] = episode_id
            else:
                self.active.pop(level_id, None)
        for level_id in list(self.active):
            if level_id not in valid:
                self.active.pop(level_id)
        for level_id in unavailable:
            eid = self.pending.pop(level_id, None)
            if eid:
                self.episodes[eid]["state"] = "UNRESOLVED"
                self.episodes[eid]["terminal_reason"] = "SOURCE_WINDOW_EVICTED"
                self.episodes[eid]["terminal_at_ms"] = open_ms + STEP
                self.active.pop(level_id, None)
        emitted = []
        for level_id in confirmations:
            eid = self.pending.get(level_id)
            if not eid:
                raise ValueError("confirmation without pending episode")
            episode = self.episodes[eid]
            if episode["state"] == "UNRESOLVED":
                raise ValueError("cannot confirm unresolved episode")
            if not (episode["level_available_ms"] <= episode["first_touch_open_ms"] < open_ms + STEP):
                raise ValueError("noncausal level/touch/confirmation order")
            if episode["first_confirmation_ms"] is not None:
                raise ValueError("duplicate confirmation")
            episode["first_confirmation_ms"] = open_ms + STEP
            episode["state"] = "CONFIRMED"
            episode["terminal_reason"] = "CAUSAL_REACTION_CONFIRMED"
            event = {
                "event_id": eid, "episode_id": eid, "level_id": level_id,
                "known_at_ms": open_ms + STEP,
                "observed_at_ms": open_ms + STEP,
                "first_touch_open_ms": episode["first_touch_open_ms"],
                "level_available_ms": episode["level_available_ms"],
                "level_price_at_touch": episode["level_price_at_touch"],
            }
            emitted.append(event)
            self.events.append(event)
            self.pending.pop(level_id, None)
        self.last_close_ms = open_ms + STEP
        return emitted
