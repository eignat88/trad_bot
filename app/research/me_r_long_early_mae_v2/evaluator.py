"""Virtual execution and paired MAE evaluation for ME_R_LONG V2.

The evaluator is isolated from scanner_runner, paper_runner, paper/live
engines, balances, cooldowns and production risk gates.
"""
from __future__ import annotations

import logging

from datetime import datetime, timedelta, timezone
from typing import Any

from .candles import parse_utc
from .outcomes import baseline_outcome
from .protocol import BASELINE_MAX_HOLD_MINUTES, ENTRY_EXPIRY_MINUTES, VARIANTS
from .variants import evaluate_variant
from .entry_coverage import check_entry_coverage
from .virtual_execution import evaluate_virtual_fill


logger = logging.getLogger(__name__)


class MeEarlyMaeV2Evaluator:
    """Run one durable, restart-safe prospective evaluation cycle."""

    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def run_cycle(self, now: datetime | None = None) -> dict[str, int]:
        now = parse_utc(now) if now else datetime.now(timezone.utc)
        stats = {"sources": 0, "fills": 0, "variants": 0, "errors": 0}
        for source in self.repository.load_pending_sources():
            stats["sources"] += 1
            try:
                self._process_source(source, now, stats)
            except Exception:
                stats["errors"] += 1
                logger.exception("ME Early MAE V2 source evaluation failed: source_event_id=%s", source.get("source_event_id"))
        return stats

    def _process_source(
        self,
        source: dict[str, Any],
        now: datetime,
        stats: dict[str, int],
    ) -> None:
        source_id = int(source["source_event_id"])
        if source["source_status"] in {"DETECTED", "ENTRY_PENDING"}:
            self._attempt_fill(source, now, stats)
            return
        if source["source_status"] != "ENTRY_FILLED":
            return

        required = (
            source.get("entry_filled_at"),
            source.get("entry_price"),
            source.get("virtual_stop_price"),
            source.get("virtual_target_price"),
        )
        if any(value is None for value in required):
            return

        entry_at = parse_utc(source["entry_filled_at"])
        baseline_end = entry_at + timedelta(minutes=BASELINE_MAX_HOLD_MINUTES)
        if now < baseline_end:
            return

        candles = self.repository.fetch_candles(
            int(source["instrument_id"]), entry_at, baseline_end,
        )
        baseline = baseline_outcome(
            candles=candles,
            entry_at=entry_at,
            deadline=baseline_end,
            entry_price=float(source["entry_price"]),
            stop_price=float(source["virtual_stop_price"]),
            target_price=float(source["virtual_target_price"]),
        )
        if baseline is None:
            self.repository.save_variant_batch(source_id, [
                {
                    "variant_id": variant.variant_id,
                    "variant_status": "INCOMPLETE_COVERAGE",
                    "trigger_result": "INCOMPLETE_COVERAGE",
                    "mae_r": None,
                    "mfe_r": None,
                    "window_low": None,
                    "window_high": None,
                    "variant_exit_at": None,
                    "variant_exit_price": None,
                    "variant_exit_reason": None,
                    "variant_gross_r": None,
                    "variant_net_r": None,
                    "delta_net_r": None,
                    "ambiguous_ordering": False,
                    "coverage_reason": "BASELINE_CANDLE_COVERAGE_INCOMPLETE",
                }
                for variant in VARIANTS
            ])
            return

        baseline_data = {
            "exit_at": baseline.exit_at,
            "exit_price": baseline.exit_price,
            "exit_reason": baseline.exit_reason,
            "gross_r": baseline.gross_r,
            "net_r": baseline.net_r,
            "coverage": "COMPLETE",
        }

        evaluations = []
        for variant in VARIANTS:
            result = evaluate_variant(
                variant=variant,
                candles=candles,
                entry_at=entry_at,
                entry_price=float(source["entry_price"]),
                stop_price=float(source["virtual_stop_price"]),
                target_price=float(source["virtual_target_price"]),
                baseline=baseline,
            )
            evaluations.append({
                "variant_id": variant.variant_id,
                "variant_status": result.variant_status,
                "mae_r": result.mae_r,
                "mfe_r": result.mfe_r,
                "window_low": result.window_low,
                "window_high": result.window_high,
                "trigger_result": result.trigger_result,
                "variant_exit_at": result.variant_exit_at,
                "variant_exit_price": result.variant_exit_price,
                "variant_exit_reason": result.variant_exit_reason,
                "variant_gross_r": result.variant_gross_r,
                "variant_net_r": result.variant_net_r,
                "delta_net_r": result.delta_net_r,
                "ambiguous_ordering": result.ambiguous_ordering,
                "coverage_reason": result.coverage_reason,
            })
        self.repository.save_paired_outcome(source_id, baseline_data, evaluations)
        stats["fills"] += 1
        stats["variants"] += len(evaluations)

    def _attempt_fill(
        self,
        source: dict[str, Any],
        now: datetime,
        stats: dict[str, int],
    ) -> None:
        source_id = int(source["source_event_id"])
        detected_at = parse_utc(source["detected_at"])
        expiry_at = detected_at + timedelta(minutes=ENTRY_EXPIRY_MINUTES)
        if now < expiry_at:
            return

        candles = self.repository.fetch_candles(
            int(source["instrument_id"]), detected_at, now,
        )
        fill = evaluate_virtual_fill(source, candles, now)
        if fill is None:
            complete, reason = check_entry_coverage(
                candles, detected_at, expiry_at, now,
            )
            if not complete:
                stats["incomplete_entry_coverage"] = (
                    stats.get("incomplete_entry_coverage", 0) + 1
                )
                return

            self.repository.mark_source_not_filled(
                source_id, "ENTRY_NOT_FILLED",
            )
            return

        self.repository.update_source_entry(source_id, {
            "entry_pending_at": fill.entry_available_at,
            "entry_filled_at": fill.entry_at,
            "entry_price": fill.entry_price,
            "virtual_stop_price": fill.stop_price,
            "virtual_target_price": fill.target_price,
            "risk_distance": fill.risk_distance,
        })
        self.repository.ensure_variants(source_id)
        stats["fills"] += 1
