"""Issue #176: offline same-market-reaction clustering and restart equivalence.

Cluster comparisons are diagnostics, not a deduplication protocol. No trading.
"""
from __future__ import annotations
import argparse
import json
from collections import defaultdict
from pathlib import Path
from tools.research.htf_baseline_short_causal_coverage_v1 import load_bars, utc_ms
from tools.research.htf_baseline_short_multi_level_replay_v1 import replay


def cluster_events(events, episodes=None, *, proximity_ms=300000, price_bps=20):
    """Pairwise candidate collisions; same-confirmation or adjacent 5m;
    level price within 20bps. This does NOT prove event dependence.
    """
    if episodes is None:
        episodes = {}
    pairs = []
    ordered = sorted(events, key=lambda x: (x["known_at_ms"], x["event_id"]))
    for i, a in enumerate(ordered):
        pa = a.get("level_price_at_touch")
        if pa is None:
            pa = episodes.get(a["episode_id"], {}).get("level_price_at_touch")
        for b in ordered[i+1:]:
            if b["known_at_ms"] - a["known_at_ms"] > proximity_ms:
                break
            pb = b.get("level_price_at_touch")
            if pb is None:
                pb = episodes.get(b["episode_id"], {}).get("level_price_at_touch")
            if pa is None or pb is None or min(pa, pb) <= 0:
                continue
            gap_bps = 10000 * abs(pa-pb)/min(pa,pb)
            if gap_bps <= price_bps:
                pairs.append({
                    "first_event_id": a["event_id"],
                    "second_event_id": b["event_id"],
                    "same_level": a["level_id"] == b["level_id"],
                    "time_gap_ms": b["known_at_ms"]-a["known_at_ms"],
                    "level_gap_bps": round(gap_bps, 4),
                })
    ids = {v for pair in pairs for v in (pair["first_event_id"], pair["second_event_id"])}
    return {"candidate_pairs": len(pairs), "events_in_pairs": len(ids),
            "pairs": pairs, "parameters": {"proximity_ms": proximity_ms, "price_bps": price_bps},
            "verdict": "POTENTIAL_OVERLAP_ONLY_NOT_INDEPENDENCE_PROOF"}


def analyze(symbol, candles, start_ms, end_ms):
    baseline = replay(symbol, candles, start_ms, end_ms)
    restart_at = start_ms + (end_ms-start_ms)//2
    restarted = replay(symbol, candles, start_ms, end_ms, restart_at_ms=restart_at)
    if not restarted["restart_performed"]:
        raise AssertionError("checkpoint restart not executed")
    keys = ["emissions", "counters", "total_episodes_including_warmup",
            "episode_state_counts_including_warmup", "pending_at_end",
            "unresolved_reason_counts", "integrity"]
    mismatches = [k for k in keys if baseline[k] != restarted[k]]
    if mismatches:
        raise AssertionError(f"restart replay mismatch: {mismatches}")
    cluster = cluster_events(baseline["emissions"])
    return {"symbol": symbol, "restart_equivalence": "PASS",
            "restart_at_ms": restart_at, "compared_fields": keys,
            "confirmed_events": len(baseline["emissions"]), "cluster": cluster,
            "verdict": "HISTORICAL_RESEARCH_ONLY_NOT_OOS"}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--data-dir", required=True, type=Path)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out", required=True, type=Path)
    a=p.parse_args()
    start, end=utc_ms(a.start), utc_ms(a.end)
    if end<=start: raise ValueError("invalid time window")
    results=[analyze(s, load_bars(a.data_dir / f"{s}_5m.csv"), start, end) for s in a.symbols]
    a.out.write_text(json.dumps({"issue":176, "research_only":True, "results":results},indent=2))
    for result in results:
        print(result["symbol"],result["restart_equivalence"],"events",result["confirmed_events"],"pairs",result["cluster"]["candidate_pairs"])
    print("SAVED",a.out)


if __name__=="__main__":
    main()
