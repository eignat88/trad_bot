"""Issue #174: offline episode first-confirmation and rolling identity audit.

Does not alter frozen Point-B or trading routes. Strict fail-closed identity:
an episode whose start touches the left edge is not eligible for approved
prospective capture. Cross-level ambiguity is surfaced, not silently merged.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Mapping


def audit_episode_identity(events: list[dict]) -> dict:
    chronological = sorted(events, key=lambda e: (e["first_seen_ms"], e["event_id"]))
    by_episode: dict[str, list[dict]] = defaultdict(list)
    seen_event_ids: set[str] = set()
    for e in chronological:
        if e["event_id"] in seen_event_ids:
            raise ValueError("duplicate first-seen event_id")
        seen_event_ids.add(e["event_id"])
        by_episode[e["episode_id"]].append(e)

    accepted: list[dict] = []
    suppressed: list[dict] = []
    unresolved: list[dict] = []
    for episode, group in by_episode.items():
        # Different levels under a single episode would be an identity collision.
        if len({e["level_id"] for e in group}) != 1:
            raise ValueError("episode_id collision across HTF levels")
        starts = {e["episode_start_ms"] for e in group}
        if len(starts) != 1:
            unresolved.extend(group)
            continue
        if any(e["episode_left_censored"] for e in group):
            unresolved.extend(group)
            continue
        accepted.append(group[0])
        suppressed.extend(group[1:])

    # Cross-level candidates at the same reaction timestamp are ambiguous:
    # log, but do not automatically deduplicate unlike-level signals.
    by_confirmation: dict[tuple[str, int], set[str]] = defaultdict(set)
    for e in chronological:
        by_confirmation[(e["symbol"], e["known_at_ms"])].add(e["level_id"])
    cross_level_times = [
        {"symbol": symbol, "known_at_ms": time, "level_count": len(levels)}
        for (symbol, time), levels in sorted(by_confirmation.items())
        if len(levels) > 1
    ]
    return {
        "candidate_confirmations": len(chronological),
        "episode_groups": len(by_episode),
        "first_confirmations_per_episode": len(accepted),
        "suppressed_same_episode": len(suppressed),
        "unresolved_identity_records": len(unresolved),
        "cross_level_same_confirmation": cross_level_times,
        "first_confirmation_events": [
            {"event_id": e["event_id"], "episode_id": e["episode_id"],
             "known_at_ms": e["known_at_ms"]} for e in accepted
        ],
        "verdict": "DIAGNOSTIC_ONLY_NOT_PROSPECTIVE",
    }


def sliding_window_audit(candles, event, window=200) -> dict:
    """Check visibility of the ledger's historical episode start as ASOF advances.

    This does not guarantee a level remains eligible or unchanged: the
    detector must be invoked separately at each prospective ASOF.
    """
    start = event["episode_start_ms"]
    touch = event["touch_open_ms"]
    seen = event["first_seen_ms"]
    asofs = [int(c.timestamp) + 300_000 for c in candles if int(c.timestamp) + 300_000 >= seen]
    records = []
    for asof in asofs:
        visible = [c for c in candles if int(c.timestamp) + 300_000 <= asof][-window:]
        if not visible:
            continue
        left = int(visible[0].timestamp)
        records.append({"asof_ms": asof, "window_start_ms": left,
                        "episode_start_visible": left <= start,
                        "touch_visible": left <= touch})
    transitions = [
        records[i] for i in range(1, len(records))
        if records[i]["episode_start_visible"] != records[i-1]["episode_start_visible"]
    ]
    return {
        "event_id": event["event_id"], "episode_id": event["episode_id"],
        "window": window, "asof_checked": len(records),
        "episode_start_exit_count": len(transitions),
        "episode_start_exit": transitions,
        "identity_not_guaranteed_if_start_evicted": bool(transitions),
    }
