"""#176 offline incremental causal replay: READ ONLY, no orders/DB/network.

Uses the NEW as-of HTF selector, frozen contact/reaction thresholds and
checkpointable EpisodeState. This is exploratory and not approved OOS.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.research.htf_baseline_short_stateful_episode_v1 import EpisodeState
from app.research.htf_baseline_short_causal_level_adapter_v1 import (
    causal_resistance_snapshot, causal_contact,
)
from app.research.htf_keylevel_point_b import (
    _causal_atr_series, _find_reaction_end,
)
from tools.research.htf_baseline_short_causal_coverage_v1 import (
    BAR_MS, closed_htf, load_bars, utc_ms,
)


def replay(symbol, candles, start_ms: int, end_ms: int):
    state = EpisodeState(symbol=symbol)
    counters = Counter()
    # Advance from initial archive bar to guarantee causal state warmup and
    # continuity. Reporting is restricted to the requested decision interval.
    for index, bar in enumerate(candles):
        asof = int(bar.timestamp) + BAR_MS
        if asof >= end_ms:
            break
        prefix = candles[max(0, index-199):index+1]
        htf = closed_htf(candles[:index+1], asof)[-200:]
        if len(prefix) < 20 or len(htf) < 26:
            level = None
            touch = False
            confirm = False
        else:
            level = causal_resistance_snapshot(htf, prefix, asof)
            atr = _causal_atr_series(prefix)
            touch = causal_contact(bar, level, atr[-1])
            confirm = False
            # P2-09: mirror the multi-level active-vs-recontact distinction. A
            # reaction confirmation may be evaluated on a bar that still touches
            # the level when it continues an active contact run; a re-contact
            # (touch after the run broke) is handled by the state machine as a
            # supersession and must not confirm the superseded pending episode.
            if level is not None and (not touch or level.level_id in state.active):
                pending_id = state.pending.get(level.level_id)
                if pending_id:
                    record = state.episodes[pending_id]
                    first = int(record["first_touch_open_ms"])
                    matching = [i for i, c in enumerate(prefix) if int(c.timestamp) == first]
                    if matching:
                        candidate = _find_reaction_end(
                            closed=prefix, touch_index=matching[0],
                            level=SimpleNamespace(price=record["level_price_at_touch"]),
                            direction="SHORT", atr_by_index=atr,
                        )
                        confirm = candidate is not None and candidate[0] == len(prefix)-1
                    else:
                        counters["pending_touch_outside_200"] += 1
            if level is not None and state.pending and level.level_id not in state.pending:
                counters["unselected_pending_level_prefixes"] += 1
        emitted = state.advance(
            open_ms=int(bar.timestamp), high=float(bar.high), low=float(bar.low),
            level=level, touch=touch, reaction_confirmed=confirm,
        )
        if start_ms <= asof < end_ms:
            counters["decisions"] += 1
            counters["with_level"] += bool(level)
            counters["contacts"] += touch
            counters["new_confirmations"] += len(emitted)
    emitted_in_scope = [
        e for e in state.emitted
        if start_ms <= e["known_at_ms"] < end_ms
    ]
    return {
        "symbol": symbol, "counters": dict(counters),
        "emissions": emitted_in_scope, "all_episodes": len(state.episodes),
        "state_last_close_ms": state.last_close_ms,
        "verdict": "HISTORICAL_DIAGNOSTIC_NOT_OOS",
        "unresolved": [
            "non-selected HTF levels and pending reactions need independent lifecycle",
            "touch boundary and pending expiry not finalized",
            "state snapshots not yet transactionally persisted",
        ],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", required=True, type=Path)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()
    start, end = utc_ms(args.start), utc_ms(args.end)
    if end <= start:
        raise ValueError("end must exceed start")
    result = {
        "issue": 176, "research_only": True,
        "period": {"start_ms": start, "end_ms": end},
        "symbols": [replay(s, load_bars(args.data_dir / f"{s}_5m.csv"), start, end)
                    for s in args.symbols],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    for s in result["symbols"]:
        print(s["symbol"], s["counters"], "episodes", s["all_episodes"])
    print("SAVED", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
