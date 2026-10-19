"""Issue #174: bounded, offline, READ-ONLY causal event coverage diagnostics.

Usage:
 python -m tools.research.htf_baseline_short_causal_coverage_v1 \
   --data-dir /tmp/htf_coverage_174 --symbols BTCUSDT ETHUSDT \
   --start 2026-10-14T00:00:00Z --end 2026-10-17T00:00:00Z \
   --out /tmp/htf_174_coverage.json

Requires SYMBOL_5m.csv files (timestamp,open,high,low,close,volume), with
adequate prior warm-up. It does NOT fetch/exchange/modify any data.
All counts are decision-prefix counts unless explicitly named unique_events.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from app.models import Candle
from app.research import htf_keylevel_baseline_short_causal_observer_v1 as observer
from tools.research.htf_baseline_short_episode_ledger_v1 import event_episode

BAR_MS = 300_000
HOUR_MS = 3_600_000


def utc_ms(s: str) -> int:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("timestamps must carry timezone")
    return int(d.timestamp() * 1000)


def load_bars(path: Path) -> list[Candle]:
    rows: list[Candle] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(Candle(int(row["timestamp"]), *(
                float(row[k]) for k in ("open", "high", "low", "close", "volume")
            )))
    for i, c in enumerate(rows):
        if (c.timestamp % BAR_MS or
                (i and c.timestamp - rows[i-1].timestamp != BAR_MS)):
            raise ValueError(f"{path}: gap, unordered or duplicate at index {i}")
        if min(c.open, c.high, c.low, c.close) <= 0 or c.low > min(c.open, c.close) or c.high < max(c.open, c.close):
            raise ValueError(f"{path}: invalid OHLC at index {i}")
    return rows


def closed_htf(prefix: Sequence[Candle], asof_ms: int) -> list[Candle]:
    """Only fully-covered 12x5m hours that are closed as of decision time."""
    buckets: dict[int, list[Candle]] = {}
    for c in prefix:
        h = c.timestamp // HOUR_MS * HOUR_MS
        if h + HOUR_MS <= asof_ms:
            buckets.setdefault(h, []).append(c)
    out: list[Candle] = []
    for t in sorted(buckets):
        group = buckets[t]
        if len(group) != 12 or [c.timestamp for c in group] != [t+i*BAR_MS for i in range(12)]:
            continue
        out.append(Candle(t, group[0].open, max(x.high for x in group),
                          min(x.low for x in group), group[-1].close,
                          sum(x.volume for x in group)))
    return out


def diagnose(symbol: str, candles: Sequence[Candle], start_ms: int,
             end_ms: int, windows: Sequence[int] = (200, 400)) -> dict:
    if not candles or start_ms >= end_ms:
        raise ValueError("empty candles or invalid interval")
    results: dict[str, dict] = {}
    for window in windows:
        funnel: Counter[str] = Counter()
        first_seen: dict[str, dict] = {}
        for index, bar in enumerate(candles):
            asof = int(bar.timestamp) + BAR_MS
            if not start_ms <= asof < end_ms:
                continue
            if index + 1 < 20:
                continue
            prefix = list(candles[max(0, index+1-window):index+1])
            htf = closed_htf(candles[:index+1], asof)[-200:]
            funnel["decision_prefixes"] += 1
            if len(htf) < 26:
                funnel["insufficient_htf_warmup"] += 1
                continue
            try:
                signal = observer.detect_causal_baseline_short(
                    symbol=symbol, execution_candles=prefix,
                    htf_candles=htf,
                    evaluation_asof=datetime.fromtimestamp(asof/1000, timezone.utc),
                )
            except Exception as exc:
                funnel[f"error_{type(exc).__name__}"] += 1
                continue
            if signal is None:
                funnel["no_fresh_signal"] += 1
                continue
            funnel["emitted_prefixes"] += 1
            if signal.known_at_ms != asof or signal.observed_at_ms < signal.known_at_ms:
                funnel["causal_time_violation"] += 1
                continue
            if signal.event_id not in first_seen:
                episode = event_episode(symbol, prefix, htf, signal.touch_open_ms)
                first_seen[signal.event_id] = {
                    "symbol": symbol, "event_id": signal.event_id,
                    "known_at_ms": signal.known_at_ms,
                    "first_seen_ms": asof, "touch_open_ms": signal.touch_open_ms,
                    "reaction_open_ms": signal.reaction_open_ms, **episode,
                }
            else:
                funnel["repeat_emission"] += 1
        events = list(first_seen.values())
        episode_groups: dict[str, list[str]] = {}
        for event in events:
            episode_groups.setdefault(event["episode_id"], []).append(event["event_id"])
        results[str(window)] = {
            "decision_funnel": dict(sorted(funnel.items())),
            "unique_events": len(first_seen),
            "unique_htf_levels": len({e["level_id"] for e in events}),
            "unique_touch_episodes": len(episode_groups),
            "episodes_with_multiple_signals": sum(len(v) > 1 for v in episode_groups.values()),
            "same_episode_excess_signals": sum(len(v)-1 for v in episode_groups.values()),
            "episode_groups": episode_groups,
            "first_seen": events,
        }
    return {"symbol": symbol, "windows": results, "verdict": "DIAGNOSTIC_ONLY_NOT_OOS"}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    start, end = utc_ms(args.start), utc_ms(args.end)
    report = {"issue": 174, "research_only": True,
              "data_source": "provided CSV; no network calls",
              "start_ms": start, "end_ms": end, "symbols": []}
    for symbol in args.symbols:
        report["symbols"].append(diagnose(
            symbol, load_bars(args.data_dir / f"{symbol}_5m.csv"), start, end))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"output": str(args.out),
                      "symbols": len(report["symbols"]),
                      "decision_counts": [
                          {s["symbol"]: {w: x["decision_funnel"].get("decision_prefixes", 0)
                           for w, x in s["windows"].items()}}
                          for s in report["symbols"]]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
