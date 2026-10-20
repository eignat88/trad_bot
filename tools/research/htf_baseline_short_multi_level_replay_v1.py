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
from app.research.htf_baseline_short_causal_level_adapter_v1 import causal_resistance_snapshot, causal_resistance_universe

# Research-only level universe. Legacy selector returns one nearest level;
# this simulator independently retains level snapshots seen at earlier ASOFs.
# It is NOT a complete multi-level HTF source-discovery algorithm.


def replay(symbol, candles, start_ms, end_ms, *, restart_at_ms=None,
           resume_checkpoint=None, save_checkpoint=None, stop_at_ms=None,
           full_universe=False, pending_impact=False):
    state = MultiLevelEpisodes(symbol)
    known_levels = {}
    counters = Counter()
    if resume_checkpoint is not None:
        checkpoint = json.loads(Path(resume_checkpoint).read_text())
        if checkpoint["symbol"] != symbol or checkpoint["end_ms"] != end_ms or checkpoint["start_ms"] != start_ms or checkpoint.get("full_universe", False) != full_universe:
            raise ValueError("checkpoint identity mismatch")
        state = MultiLevelEpisodes.restore(checkpoint["state"])
        from app.research.htf_baseline_short_stateful_episode_v1 import LevelSnapshot
        known_levels = {key: LevelSnapshot(**v) for key,v in checkpoint["known_levels"].items()}
        counters = Counter(checkpoint["counters"])
    impact = {"eligibility_loss_with_pending": [], "confirmations_after_eligibility_loss": [], "eligibility_changes": 0}
    previously_eligible = set()
    disappeared = set()
    lost_pending = {}
    prior_eligibility_losses = []
    if resume_checkpoint is not None and pending_impact:
        impact = checkpoint.get("pending_impact", impact)
        previously_eligible = set(checkpoint.get("previously_eligible", []))
        disappeared = set(checkpoint.get("disappeared", []))
        lost_pending = checkpoint.get("lost_pending", {})
        prior_eligibility_losses = list(state.eligibility_loss_records)
    restart_done = False
    for index, bar in enumerate(candles):
        asof = int(bar.timestamp) + BAR_MS
        if asof >= end_ms or (stop_at_ms is not None and asof >= stop_at_ms):
            break
        if state.last_close_ms is not None and asof <= state.last_close_ms:
            continue
        if restart_at_ms is not None and not restart_done and asof >= restart_at_ms:
            state = MultiLevelEpisodes.restore(json.loads(json.dumps(state.checkpoint())))
            known_levels = {k: type(v)(**json.loads(json.dumps(vars(v)))) for k, v in known_levels.items()}
            counters = Counter(json.loads(json.dumps(dict(counters))))
            restart_done = True
        prefix = candles[max(0, index-199):index+1]
        htf = closed_htf(candles[:index+1], asof)[-200:]
        if len(prefix) >= 20 and len(htf) >= 26:
            selected = causal_resistance_snapshot(htf, prefix, asof)
            if full_universe:
                discovered = causal_resistance_universe(htf, prefix, asof)
                for candidate in discovered:
                    known_levels.setdefault(candidate.level_id, candidate)
            elif selected is not None:
                known_levels.setdefault(selected.level_id, selected)
            atr = _causal_atr_series(prefix)
        else:
            selected = None
            discovered = ()
            atr = None
        if pending_impact and full_universe:
            eligible_ids = {x.level_id for x in discovered} if len(prefix) >= 20 and len(htf) >= 26 else set()
            disappeared = previously_eligible - eligible_ids
            for candidate in discovered:
                state.known_level_ids.add(candidate.level_id)
            for lid in sorted(disappeared):
                impact["eligibility_changes"] += 1
                if lid in state.pending:
                    eid = state.pending[lid]
                    lost_pending[eid] = {"level_id":lid, "lost_at_ms":asof}
                    impact["eligibility_loss_with_pending"].append({"asof_ms":asof,"level_id":lid,"episode_id":eid})
            previously_eligible = eligible_ids
        contacts = {}
        confirmations = []
        unavailable = []
        disappeared_pending = disappeared & set(state.pending) if pending_impact and full_universe else set()
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
            eligibility_loss=disappeared_pending,
        )
        if pending_impact:
            for e in emitted:
                if e["episode_id"] in lost_pending:
                    impact["confirmations_after_eligibility_loss"].append({"event_id":e["event_id"], **lost_pending[e["episode_id"]]})
        if start_ms <= asof < end_ms:
            counters["decisions"] += 1
            counters["levels_known"] = max(counters["levels_known"], len(known_levels))
            counters["contacts"] += sum(x[2] for x in contacts.values())
            counters["confirmations"] += len(emitted)
            counters["unavailable_pending_prefixes"] += len(unavailable)
    if save_checkpoint is not None:
        payload = {
            "symbol": symbol, "start_ms": start_ms, "end_ms": end_ms,
            "full_universe": full_universe,
            "state": state.checkpoint(), "known_levels": {k: vars(v) for k,v in known_levels.items()},
            "counters": dict(counters),
            "pending_impact": impact if pending_impact else None,
            "previously_eligible": sorted(previously_eligible),
            "disappeared": sorted(disappeared),
            "lost_pending": lost_pending,
        }
        Path(save_checkpoint).write_text(json.dumps(payload, sort_keys=True))
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
    if state.eligibility_loss_records[:len(prior_eligibility_losses)] != prior_eligibility_losses:
        integrity_errors.append("eligibility loss replay mismatch")
    if integrity_errors:
        raise ValueError(f"event integrity failed: {integrity_errors[:5]}")
    return {
        "symbol": symbol, "counters": dict(counters),
        "full_universe": full_universe,
        "pending_impact": impact if pending_impact else None,
        "restart_performed": restart_done,
        "unique_emission_ids": len({e["event_id"] for e in scope}),
        "emissions": scope,
        "total_episodes_including_warmup": len(state.episodes),
        "episode_state_counts_including_warmup": dict(statuses),
        "pending_at_end": len(state.pending),
        "integrity": {"checked": len(scope), "violations": len(integrity_errors), "result": "PASS"},
        "unresolved_reason_counts": dict(Counter(x.get("terminal_reason") for x in state.episodes.values() if x["state"] == "UNRESOLVED")),
        "semantic_change_pending_vps_ab": {
            "unavailable_condition": "level.available_ms > asof for missing pending-prefix evidence",
            "status": "SEMANTIC_CHANGE_PENDING_VPS_AB",
        },
        "eligibility_loss_records": list(state.eligibility_loss_records),
        "verdict": "DIAGNOSTIC_MULTI_LEVEL_NOT_OOS",
        "limitations": [
            "level universe mode: all point-in-time eligible candidates" if full_universe else "level universe consists only of snapshots selected at least once",
            "full universe applies proposed selection semantics; not frozen or production approved" if full_universe else "unselected eligible levels not discovered by current selector",
            "no proven invalidation/expiry/retirement geometry",
            "unresolved pending remain, no production-safe cleanup",
            "no independently verified restart + full prefix replay",
            "unavailable semantics is a proposed repair and requires VPS A/B validation",
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
