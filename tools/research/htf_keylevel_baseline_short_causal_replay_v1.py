"""Bounded, diagnostic-only causal BASELINE SHORT replay. Never OOS evidence.

No DB, credentials, persistence, order endpoints or production state changes.
Loads public Bybit 5m historical range, checks complete coverage and feeds
strictly closed 5m prefixes plus fully formed 1H history to the detector.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.models import Candle
from app.config.settings import Settings
from app.exchange.bybit_client import BybitClient
from app.research.htf_keylevel_baseline_short_causal_observer_v1 import (
    BAR_MS, HTF_MS, detect_causal_baseline_short,
)
from app.research.htf_keylevel_point_b import (
    _causal_atr_series, _find_htf_key_levels, _find_reaction_end,
    KEY_LEVEL_TOUCH_TOLERANCE_ATR, LOCAL_STRUCTURE_LOOKBACK,
)
from tools.research.htf_keylevel_baseline_short_reaction_mask_audit_v1 import (inspect_reaction_masking, masking_summary)
from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
    BybitHistoricalCandleSource, SOURCE_VALIDATED,
)

def assemble_complete_1h(candles: list[Candle], asof_ms: int) -> list[Candle]:
    by_hour: dict[int, list[Candle]] = defaultdict(list)
    for c in candles:
        if c.timestamp + BAR_MS <= asof_ms:
            by_hour[c.timestamp // HTF_MS * HTF_MS].append(c)
    result = []
    for hour, rows in sorted(by_hour.items()):
        if hour + HTF_MS > asof_ms:
            continue
        rows.sort(key=lambda c: c.timestamp)
        if len(rows) != 12 or [r.timestamp for r in rows] != [
            hour + i * BAR_MS for i in range(12)
        ]:
            continue
        result.append(Candle(
            hour, rows[0].open, max(x.high for x in rows),
            min(x.low for x in rows), rows[-1].close,
            sum(x.volume for x in rows),
        ))
    return result



def diagnostic_funnel_stage(execution: list[Candle], htf: list[Candle], asof_ms: int) -> str:
    """Mirror the frozen detector's stage gates, without changing its result.

    Decision counters are NOT trade counts. Identifies the deepest gate met
    for each as-of 5m prefix. It uses the same level/touch/reaction helpers.
    """
    if len(execution) < 20 or len(htf) < LOCAL_STRUCTURE_LOOKBACK * 2 + 2:
        return "HTF_HISTORY_INSUFFICIENT"
    if (any(c.timestamp + BAR_MS > asof_ms for c in execution)
            or any(c.timestamp + HTF_MS > asof_ms for c in htf)):
        return "FUTURE_CANDLE_REJECTED"
    atr = _causal_atr_series(execution)
    level, ranges = _find_htf_key_levels(htf, execution, atr[0], "SHORT")
    if level is None or ranges is None or level.level_type != "resistance":
        return "NO_HTF_LEVEL"
    touched = False
    historical_reaction = False
    for index, candle in enumerate(execution[:-1]):
        if candle.timestamp < level.available_timestamp or atr[index] <= 0:
            continue
        near = min(
            abs((float(candle.low) - level.price) / atr[index]),
            abs((level.price - float(candle.high)) / atr[index]),
        ) <= KEY_LEVEL_TOUCH_TOLERANCE_ATR
        if not (near and float(candle.high) >= level.price):
            continue
        touched = True
        reaction = _find_reaction_end(
            closed=execution, touch_index=index, level=level,
            direction="SHORT", atr_by_index=atr,
        )
        if reaction is not None and reaction[0] == len(execution)-1:
            return "REACTION_FRESH"
        if reaction is not None:
            historical_reaction = True
    if historical_reaction:
        return "REACTION_HISTORICAL"
    return "TOUCH_NO_FRESH_REACTION" if touched else "NO_TOUCH"

def replay(symbol: str, candles: list[Candle], *, warmup_5m: int = 400) -> dict:
    if len(candles) < warmup_5m + 1:
        raise ValueError("INSUFFICIENT_HISTORY")
    for i, c in enumerate(candles):
        if c.timestamp % BAR_MS or (
            i and c.timestamp != candles[i-1].timestamp + BAR_MS
        ):
            raise ValueError("NONCONTIGUOUS_5M_HISTORY")
        if not (0 < c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high):
            raise ValueError("INVALID_OHLC")
    seen = set()
    stats = Counter()
    events = []
    masking_records = []
    for idx in range(warmup_5m, len(candles)):
        asof = candles[idx].timestamp + BAR_MS
        prefix = candles[max(0, idx-199):idx+1]
        htf = assemble_complete_1h(candles[:idx+1], asof)[-199:]
        stats["evaluated_prefixes"] += 1
        stats["stage_" + diagnostic_funnel_stage(prefix, htf, asof)] += 1
        masking = inspect_reaction_masking(prefix, htf)
        masking["asof_ms"] = asof
        masking_records.append(masking)
        signal = detect_causal_baseline_short(
            symbol=symbol, execution_candles=prefix, htf_candles=htf,
            evaluation_asof=datetime.fromtimestamp(asof / 1000, timezone.utc),
        )
        if signal is None:
            continue
        if signal.known_at_ms != asof or signal.observed_at_ms != asof:
            raise ValueError("CAUSAL_TIMESTAMP_VIOLATION")
        if signal.event_id in seen:
            stats["duplicate_emissions"] += 1
            continue
        seen.add(signal.event_id)
        stats["unique_events"] += 1
        events.append(vars(signal))
    # Funnel totals represent evaluated decisions (non-exclusive stages).
    stages = {
        "HTF_HISTORY_READY": stats["evaluated_prefixes"] - stats["stage_HTF_HISTORY_INSUFFICIENT"],
        "HTF_LEVEL_FOUND": sum(stats["stage_"+x] for x in (
            "NO_TOUCH", "TOUCH_NO_FRESH_REACTION", "REACTION_HISTORICAL", "REACTION_FRESH"
        )),
        "TOUCH_FOUND": sum(stats["stage_"+x] for x in (
            "TOUCH_NO_FRESH_REACTION", "REACTION_HISTORICAL", "REACTION_FRESH"
        )),
        "REACTION_CONFIRMED_FRESH": stats["stage_REACTION_FRESH"],
        "REACTION_CONFIRMED_HISTORICAL": stats["stage_REACTION_HISTORICAL"],
        "SIGNAL_EMITTED_UNIQUE": stats["unique_events"],
    }
    return {"mode": "HISTORICAL_DIAGNOSTIC_NOT_OOS",
            "symbol": symbol, "first_open_ms": candles[0].timestamp,
            "last_open_ms": candles[-1].timestamp,
            "candles": len(candles), "stats": dict(stats), "funnel": stages, "masking_audit": masking_summary(masking_records), "events": events}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--start", required=True, help="UTC YYYY-MM-DDTHH:MM, aligned 5m")
    p.add_argument("--end", required=True, help="UTC exclusive YYYY-MM-DDTHH:MM")
    p.add_argument("--output", default="/tmp/htf_causal_baseline_short_replay.json")
    a = p.parse_args()
    if a.symbol != "BTCUSDT":
        p.error("Phase-0 safety scope: BTCUSDT only")
    def timestamp(s):
        return int(datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp()*1000)
    start, end = timestamp(a.start), timestamp(a.end)
    if start % BAR_MS or end % BAR_MS or not 48*HTF_MS <= end-start <= 96*HTF_MS:
        p.error("Require 48-96h bounded, 5m-aligned range")
    if end > int(datetime.now(timezone.utc).timestamp()*1000)-BAR_MS:
        p.error("End must be in the past")
    # The source expects inclusive last candle open; require all bars fully closed.
    source = BybitHistoricalCandleSource(BybitClient(Settings()))
    result = source.fetch_5m_candles(a.symbol, start, end-BAR_MS, evaluation_asof_ms=end)
    if result.status != SOURCE_VALIDATED:
        raise SystemExit(f"FETCH_BLOCKED status={result.status} reason={result.reason_code} diagnostics={result.diagnostics}")
    report = replay(a.symbol, list(result.candles))
    report["source_status"] = result.status
    report["source_diagnostics"] = result.diagnostics
    path = Path(a.output)
    if not str(path).startswith("/tmp/"):
        p.error("Research output must reside under /tmp/")
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"result": "DIAGNOSTIC_COMPLETE", "path": str(path),
                      "candles": report["candles"], "stats": report["stats"], "funnel": report["funnel"], "masking_audit": report["masking_audit"]}, indent=2))


if __name__ == "__main__":
    main()
