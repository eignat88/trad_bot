"""Deterministic offline-capable prospective evaluator for V3."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .entry_policy import decide_next_open, next_boundary
from .outcomes import evaluate_baseline
from . import protocol as _protocol
from .protocol import (
    ACTIVATION_AT,
    BASELINE_MAX_HOLD_MINUTES,
    ENTRY_EXPIRY_MINUTES,
    parse_utc,
)
from .variants import evaluate_all

FINAL_STATES = frozenset({"ENTRY_NOT_FILLED", "INVALID", "FINALIZED"})


class MeEarlyMaeV3Evaluator:
    def __init__(self, repository) -> None:
        self.repository = repository

    def run_cycle(self, now=None) -> dict[str, int]:
        now = parse_utc(now or datetime.now(timezone.utc))
        stats = {
            "sources": 0, "entry_pending": 0, "fills": 0,
            "not_filled": 0, "variants": 0, "errors": 0,
        }
        for source in self.repository.load_pending_sources():
            stats["sources"] += 1
            try:
                self._process(source, now, stats)
            except Exception:
                stats["errors"] += 1
        return stats

    def _process(self, source, now, stats) -> None:
        state = source["source_status"]
        if state in FINAL_STATES:
            return
        if state in ("DETECTED", "ENTRY_PENDING"):
            self._attempt_entry(source, now, stats)
            return
        if state != "ENTRY_FILLED":
            raise ValueError(f"UNSUPPORTED_SOURCE_STATE:{state}")
        self._attempt_baseline(source, now, stats)

    def _attempt_entry(self, source, now, stats) -> None:
        eligible = source.get("entry_eligible_at")
        detected = source.get("detected_at")
        if eligible is None or detected is None:
            raise ValueError("MISSING_ENTRY_PROVENANCE")
        entry_boundary = next_boundary(max(parse_utc(eligible), parse_utc(detected), _protocol.ACTIVATION_AT))
        candles = self.repository.fetch_candles(
            int(source["instrument_id"]), entry_boundary, now
        )
        decision = decide_next_open(
            candles=candles,
            entry_eligible_at=parse_utc(eligible),
            detected_at=parse_utc(detected),
            activation_at=parse_utc(_protocol.ACTIVATION_AT),
            asof=parse_utc(now),
        )
        if decision.state == "ENTRY_FILLED":
            entry = decision.entry
            self.repository.update_source_entry(int(source["source_event_id"]), {
                "entry_filled_at": entry.entry_at,
                "entry_price": entry.entry_price,
                "stop_price": entry.stop_price,
                "target_price": entry.target_price,
                "risk_distance": entry.risk_distance,
                "baseline_deadline_at": entry.entry_at
                + timedelta(minutes=BASELINE_MAX_HOLD_MINUTES),
            })
            stats["fills"] += 1
        elif decision.state == "ENTRY_NOT_FILLED":
            self.repository.mark_source_not_filled(
                int(source["source_event_id"]), decision.reason
            )
            stats["not_filled"] += 1
        elif decision.state == "INCOMPLETE_COVERAGE":
            self.repository.mark_source_incomplete(
                int(source["source_event_id"]), decision.reason
            )
            stats["entry_pending"] += 1
        elif decision.state == "INVALID":
            self.repository.mark_source_invalid(
                int(source["source_event_id"]), decision.reason
            )
            stats["errors"] += 1
        else:
            stats["entry_pending"] += 1

    def _attempt_baseline(self, source, now, stats) -> None:
        required = (
            source.get("entry_filled_at"),
            source.get("entry_price"),
            source.get("stop_price"),
            source.get("target_price"),
            source.get("baseline_deadline_at"),
        )
        if any(value is None for value in required):
            raise ValueError("INCOMPLETE_ENTRY_FIELDS")
        entry_at = parse_utc(source["entry_filled_at"])
        deadline = parse_utc(source["baseline_deadline_at"])
        if now < deadline:
            return
        candles = self.repository.fetch_candles(
            int(source["instrument_id"]), entry_at, deadline
        )
        baseline = evaluate_baseline(
            candles,
            entry_at,
            deadline,
            float(source["entry_price"]),
            float(source["stop_price"]),
            float(source["target_price"]),
        )
        if baseline.status != "EVALUATED":
            self.repository.mark_source_incomplete(
                int(source["source_event_id"]), baseline.coverage_reason
            )
            return
        outcomes = evaluate_all(
            candles,
            entry_at,
            float(source["entry_price"]),
            float(source["stop_price"]),
            float(source["target_price"]),
            baseline,
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
        stats["variants"] += len(outcomes)
