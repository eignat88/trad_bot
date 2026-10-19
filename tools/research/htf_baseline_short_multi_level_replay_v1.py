"""#176 research-only multi-level historical replay, no trading/DB writes."""
import argparse
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from app.research.htf_baseline_short_multi_level_lifecycle_v1 import MultiLevelEpisodes
from app.research.htf_baseline_short_causal_level_adapter_v1 import causal_contact
from app.research.htf_keylevel_point_b import _causal_atr_series, _find_reaction_end
from tools.research.htf_baseline_short_causal_coverage_v1 import (
    BAR_MS, closed_htf, load_bars, utc_ms,
)
from app.research.htf_baseline_short_causal_level_adapter_v1 import causal_resistance_snapshot

# Research-only level universe. Legacy selector returns one nearest level;
# this simulator independently retains level snapshots seen at earlier ASOFs.
# It is NOT a complete multi-level HTF source-discovery algorithm.


def replay(symbol, candles, start_ms, end_ms):
    state = MultiLevelEpisodes(symbol)
    known_levels = {}
    counters = Counter()
    for index, bar in enumerate(candles):
        asof = int(bar.timestamp) + BAR_MS
        if asof >= end_ms:
            break
        prefix = candles[max(0, index-199):index+1]
        htf = closed_htf(candles[:index+1], asof)[-200:]
        if len(prefix) >= 20 and len(htf) >= 26:
            selected = causal_resistance_snapshot(htf, prefix, asof)
            if selected is not None:
                known_levels.setdefault(selected.level_id, selected)
            atr = _causal_atr_series(prefix)
        else:
            selected = None
            atr = None
        contacts = {}
        confirmations = []
        unavailable = []
        for key, level in sorted(known_levels.items()):
            if level.available_ms > int(bar.timestamp):
                continue
            touch = causal_contact(bar, level, atr[-1]) if atr else False
            contacts[key] = (level.price, level.available_ms, touch)
            eid = state.pending.get(key)
            if eid and not touch and atr:
                record = state.episodes[eid]
                first = record["first_touch_open_ms"]
                matching = [i for i, c in enumerate(prefix)
                            if int(c.timestamp) == first]
                if matching:
                    candidate = _find_reaction_end(
                        closed=prefix, touch_index=matching[0],
                        level=SimpleNamespace(price=record["level_price_at_touch"]),
                        direction="SHORT", atr_by_index=atr,
                    )
                    if candidate is not None and candidate[0] == len(prefix)-1:
                        confirmations.append(key)
                else:
                    unavailable.append(key)
        emitted = state.advance(
            open_ms=int(bar.timestamp), level_contacts=contacts,
            confirmations=confirmations, unavailable=unavailable,
        )
        if start_ms <= asof < end_ms:
            counters["decisions"] += 1
            counters["levels_known"] = max(counters["levels_known"], len(known_levels))
            counters["contacts"] += sum(x[2] for x in contacts.values())
            counters["confirmations"] += len(emitted)
            counters["unavailable_pending_prefixes"] += len(unavailable)
    scope = [e for e in state.events if start_ms <= e["known_at_ms"] < end_ms]
    statuses = Counter(x["state"] for x in state.episodes.values())
    integrity_errors = []
    seen = set()
    for e in scope:
        if e["episode_id"] in seen:
            integrity_errors.append("duplicate episode")
        seen.add(e["episode_id"])
        if not (e["level_available_ms"] <= e["first_touch_open_ms"] < e["known_at_ms"] <= e["observed_at_ms"]):
            integrity_errors.append("causality ordering")
        if state.episodes[e["episode_id"]]["state"] != "CONFIRMED":
            integrity_errors.append("terminal inconsistency")
    if integrity_errors:
        raise ValueError(f"event integrity failed: {integrity_errors[:5]}")
    return {
        "symbol": symbol, "counters": dict(counters),
        "unique_emission_ids": len({e["event_id"] for e in scope}),
        "emissions": scope,
        "total_episodes_including_warmup": len(state.episodes),
        "episode_state_counts_including_warmup": dict(statuses),
        "pending_at_end": len(state.pending),
        "integrity": {"checked": len(scope), "violations": len(integrity_errors), "result": "PASS"},
        "unresolved_reason_counts": dict(Counter(x.get("terminal_reason") for x in state.episodes.values() if x["state"] == "UNRESOLVED")),
        "verdict": "DIAGNOSTIC_MULTI_LEVEL_NOT_OOS",
        "limitations": [
            "level universe consists only of snapshots selected at least once",
            "unselected eligible levels not discovered by current selector",
            "no proven invalidation/expiry/retirement geometry",
            "unresolved pending remain, no production-safe cleanup",
            "no independently verified restart + full prefix replay",
        ],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    start, end = utc_ms(a.start), utc_ms(a.end)
    if end <= start:
        raise ValueError("end <= start")
    result = {
        "issue": 176, "research_only": True,
        "symbols": [
            replay(s, load_bars(a.data_dir / f"{s}_5m.csv"), start, end)
            for s in a.symbols
        ],
    }
    a.out.write_text(json.dumps(result, indent=2, sort_keys=True))
    for item in result["symbols"]:
        print(item["symbol"], item["counters"],
              item["episode_state_counts_including_warmup"])
    print("SAVED", a.out)


if __name__ == "__main__":
    main()
