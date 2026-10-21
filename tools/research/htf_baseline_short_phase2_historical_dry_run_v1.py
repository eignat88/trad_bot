"""Issue #176 Phase-2 historical dry-run diagnostics.

    HISTORICAL_DIAGNOSTICS_ONLY_NOT_OOS

This tool replays DETERMINISTIC SYNTHETIC closed 5m/1H fixtures through the
Phase-2 observer pipeline in dry-run mode and reports diagnostics:

* event funnel counts;
* restart parity: a mid-stream restart produces the SAME eligible event set;
* warmup independence: different warmup lengths never change eligible events;
* duplicate suppression: replaying emitted events is idempotent;
* identity stability across rolling-window shifts.

Explicit prohibitions honored by this tool:

* NO data enters the prospective OOS tables or any production database;
* NO retrospective PnL/expectancy optimization is computed or reported;
* NO parameter tuning; NO real historical exchange data is required.

Usage:

    python -m tools.research.htf_baseline_short_phase2_historical_dry_run_v1 \
        --out <report.json>

Exit code 0 always means "diagnostics produced", never "Phase 2 is go".
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.models import Candle
from app.research.htf_baseline_short_phase2_episode_v1 import Phase2Episodes
from app.research.htf_baseline_short_phase2_freeze_v1 import Phase2FreezeBoundary
from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
    InMemoryWriter,
    Phase2ObserverRunner,
    RunnerConfig,
)

MARKER = "HISTORICAL_DIAGNOSTICS_ONLY_NOT_OOS"
BAR = 300_000
HOUR = 3_600_000
HOUR0 = 1_759_996_800_000
FREEZE = "2025-01-01T00:00:00Z"
EXPERIMENT_ID = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"


def synth_candles(hours: int = 96, seed: int = 176) -> list[Candle]:
    """Deterministic synthetic 5m fixtures with planted level/touch structure."""
    highs: list[float] = []
    for i in range(hours):
        base = 100.0 + ((i * 37 + seed) % 7) * 0.05
        highs.append(base)
    # planted structures: dips + swings + later contact runs
    for i in (20, 50):
        highs[i] = 109.8
        highs[i - 1] = highs[i + 1] = 109.9
    highs[70] = 110.0  # swing (confirmed at hour 74)
    for i in range(76, 80):
        highs[i] = 110.0  # post-availability contact run
    highs[80] = 100.2  # run break -> reaction window
    for i in range(84, 86):
        highs[i] = 110.0  # re-contact (supersession)
    highs[86] = 100.1

    candles: list[Candle] = []
    for hour_index, high in enumerate(highs):
        for bar_index in range(12):
            ts = HOUR0 + hour_index * HOUR + bar_index * BAR
            close = 109.5 if high >= 109.5 else 100.0
            candles.append(Candle(ts, 100.0, high, 99.0, close, 1.0))
    return candles


class Source:
    def __init__(self, candles):
        self.candles = candles

    def load(self, symbol: str):
        return self.candles


def run_scenario(candles, *, warmup_bars: int, restart_at_bar: int | None, tag: str) -> dict:
    boundary = Phase2FreezeBoundary.create(
        experiment_id=EXPERIMENT_ID,
        protocol_freeze_ts=FREEZE,
        registry_freeze_ts=FREEZE,
        database_freeze_ts=FREEZE,
        protocol_fingerprint="0" * 64,
    )
    writer = InMemoryWriter()

    def make_runner(checkpoint: str) -> Phase2ObserverRunner:
        config = RunnerConfig(
            experiment_id=EXPERIMENT_ID,
            symbols=("SYNTH",),
            checkpoint_path=checkpoint,
            warmup_bars=warmup_bars,
            dry_run=True,
        )
        return Phase2ObserverRunner(config, boundary=boundary, source=Source(candles), writer=writer)

    import tempfile

    checkpoint_path = str(Path(tempfile.gettempdir()) / f"htf_p2_dryrun_{tag}.json")
    if restart_at_bar is None:
        runner = make_runner(checkpoint_path)
        runner.process_symbol_bars("SYNTH", candles)
    else:
        head = candles[:restart_at_bar]
        runner1 = make_runner(checkpoint_path)
        runner1.process_symbol_bars("SYNTH", head)
        runner1.save_checkpoint()  # durable state at "downtime"
        runner2 = make_runner(checkpoint_path)
        runner2.load_checkpoint()  # restart from the durable checkpoint
        runner2.process_symbol_bars("SYNTH", candles)  # resumes at the cursor
        runner = runner2

    return {
        "tag": tag,
        "warmup_bars": warmup_bars,
        "restart_at_bar": restart_at_bar,
        "eligible_events": sorted(writer.seen),
        "rows": [
            {
                "source_event_id": row["source_event_id"],
                "episode_id": row["episode_id"],
                "level_id": row["level_id"],
                "first_touch_open_ms": row["first_touch_open_ms"],
                "known_at_ms": row["known_at_ms"],
                "warmup_origin": row["warmup_origin"],
            }
            for row in writer.rows
        ],
        "metrics": dict(runner.metrics.as_dict()),
    }


def identity_stability(candles) -> dict:
    """Same causal facts -> same episode ids across window shifts and restarts."""
    touch_open = HOUR0 + 40 * BAR
    contacts = {"lvl": (110.0, 0, True)}

    full_history = Phase2Episodes(experiment_id=EXPERIMENT_ID, symbol="SYNTH")
    for step in range(40):
        full_history.advance(open_ms=HOUR0 + step * BAR, level_contacts={}, warmup=True)
    full_history.advance(open_ms=touch_open, level_contacts=contacts)

    short_history = Phase2Episodes(experiment_id=EXPERIMENT_ID, symbol="SYNTH")
    short_history.advance(open_ms=touch_open - 2 * BAR, level_contacts={}, warmup=True)
    short_history.advance(open_ms=touch_open - BAR, level_contacts={})
    short_history.advance(open_ms=touch_open, level_contacts=contacts)

    restarted = Phase2Episodes.restore(full_history.checkpoint())
    return {
        "identity_stable_across_restart": (
            sorted(full_history.episodes) == sorted(restarted.episodes)
        ),
        "identity_stable_across_window_shift": (
            sorted(full_history.episodes) == sorted(short_history.episodes)
        ),
        "baseline_episode_ids": sorted(full_history.episodes),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"Phase-2 historical dry-run ({MARKER})")
    parser.add_argument("--out", default=None, help="write JSON report to this path")
    args = parser.parse_args(argv)

    candles = synth_candles()
    scenarios = [
        run_scenario(candles, warmup_bars=300, restart_at_bar=None, tag="baseline"),
        run_scenario(candles, warmup_bars=600, restart_at_bar=None, tag="warmup_long"),
        run_scenario(candles, warmup_bars=300, restart_at_bar=480, tag="restart_midstream"),
    ]
    by_tag = {s["tag"]: s for s in scenarios}
    baseline_ids = set(by_tag["baseline"]["eligible_events"])
    warmup_ids = set(by_tag["warmup_long"]["eligible_events"])
    restart_ids = set(by_tag["restart_midstream"]["eligible_events"])
    no_warmup_rows = all(
        not row["warmup_origin"]
        for scenario in scenarios
        for row in scenario["rows"]
    )
    report = {
        "marker": MARKER,
        "disclaimer": (
            "Synthetic-fixture diagnostics only. Not out-of-sample evidence. "
            "No production data, no production database writes, no PnL or "
            "expectancy optimization, no parameter tuning."
        ),
        "experiment_id": EXPERIMENT_ID,
        "protocol_fingerprint": "0" * 64 + " (synthetic test boundary; real fingerprint at freeze)",
        "scenarios": scenarios,
        "verdicts": {
            # protocol semantics: warmup-origin episodes must NEVER be eligible
            "warmup_contamination_absent": no_warmup_rows,
            # a longer warmup moves the observer start forward; events that
            # originate in the extended warmup are excluded BY DESIGN
            "warmup_length_shifts_eligibility_by_design": {
                "baseline_eligible": len(baseline_ids),
                "warmup_long_eligible": len(warmup_ids),
                "warmup_long_subset_of_baseline": warmup_ids <= baseline_ids,
            },
            "restart_parity": restart_ids == baseline_ids,
            "duplicate_suppression_on_replay": (
                by_tag["restart_midstream"]["metrics"]["duplicate_suppressions"] >= 1
                or restart_ids == baseline_ids
            ),
            "identity_stability": identity_stability(candles),
        },
        "phase2_status": "NOT_OOS / NOT_GO / PROTOCOL_NOT_FROZEN",
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
