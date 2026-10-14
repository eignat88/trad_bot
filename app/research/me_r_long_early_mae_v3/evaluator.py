"""Deterministic offline-capable prospective evaluator for V3."""
from __future__ import annotations

from datetime import datetime, timezone

from .candles import validate_window
from .entry_policy import evaluate_next_open
from .outcomes import evaluate_baseline
from .protocol import (
    BASELINE_MAX_HOLD_MINUTES,
    parse_utc,
    execution_geometry,
)
from .variants import evaluate_all


class MeEarlyMaeV3Evaluator:
    def __init__(self, repository) -> None:
        self.repository = repository

    def run_cycle(self, now=None) -> dict[str, int]:
        now = parse_utc(now or datetime.now(timezone.utc))
        stats = {"sources": 0, "fills": 0, "variants": 0, "errors": 0}
        for source in self.repository.load_pending_sources():
            stats["sources"] += 1
            try:
                self._process(source, now, stats)
            except Exception:
                stats["errors"] += 1
        return stats

    def _process(self, source, now, stats) -> None:
        entry_at = parse_utc(source["entry_filled_at"])
        if now < entry_at:
            return
        deadline = parse_utc(source["baseline_deadline_at"])
        if now < deadline:
            return
        candles = self.repository.fetch_candles(
            int(source["instrument_id"]), entry_at, deadline
        )
        entry_price = float(source["entry_price"])
        stop_price = float(source["stop_price"])
        target_price = float(source["target_price"])
        baseline = evaluate_baseline(
            candles, entry_at, deadline, entry_price, stop_price, target_price
        )
        if baseline.status != "EVALUATED":
            return
        outcomes = evaluate_all(
            candles, entry_at, entry_price, stop_price, target_price, baseline
        )
        self.repository.save_paired_outcome(
            int(source["source_event_id"]),
            {
                "source_status": "FINALIZED",
                "coverage_status": "COMPLETE",
                "baseline_exit_at": baseline.exit_at,
                "baseline_exit_price": baseline.exit_price,
                "baseline_exit_reason": baseline.exit_reason,
                "baseline_gross_r": baseline.gross_r,
                "baseline_net_r_normal": baseline.net_r_normal,
                "baseline_net_r_elevated": baseline.net_r_elevated,
            },
            [outcome.__dict__ for outcome in outcomes],
        )
        stats["fills"] += 1
        stats["variants"] += len(outcomes)
