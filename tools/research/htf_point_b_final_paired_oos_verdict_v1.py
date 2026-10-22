"""Issue #169: deterministic read-only matching of HTF Point-B and baseline records.

This module deliberately does NOT connect to a DB, mutate lifecycle state,
reconstruct candles, or claim an execution/causal verdict. Inputs should be
validated, frozen, exported observations with outcome information attached.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

POINT_B = "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE"
BASELINE = "HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE"
FREEZE = datetime(2026, 10, 6, 14, tzinfo=timezone.utc)


@dataclass(frozen=True)
class EventRecord:
    experiment_id: str
    setup_event_id: str
    observation_id: int
    symbol: str
    direction: str
    signal_time: datetime
    finalized: bool
    gross_r: float | None


@dataclass(frozen=True)
class PairedRecord:
    event_id: str
    symbol: str
    direction: str
    baseline: EventRecord
    point_b: EventRecord

    @property
    def delta_r(self) -> float:
        assert self.baseline.gross_r is not None
        assert self.point_b.gross_r is not None
        return self.point_b.gross_r - self.baseline.gross_r


@dataclass(frozen=True)
class MatchReport:
    pairs: tuple[PairedRecord, ...]
    reasons: dict[str, int]
    point_b_total: int
    baseline_total: int


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("Naive signal_time is not admissible")
    return dt.astimezone(timezone.utc)


def _record_key(r: EventRecord) -> tuple[str, str]:
    return (r.setup_event_id, r.direction)


def match_frozen_events(rows: Iterable[EventRecord]) -> MatchReport:
    """Match by event AND direction; fail closed on duplicates and bad identity.

    The baseline must be observed at or before Point-B. This is an
    event-conditional comparison: missing Point-B setups are counted as
    unmatched, not discarded from the funnel statistics.
    """
    groups: dict[tuple[str, str], dict[str, list[EventRecord]]] = {}
    reasons: Counter[str] = Counter()
    totals = Counter()
    for row in rows:
        if row.experiment_id not in {POINT_B, BASELINE}:
            reasons["other_experiment"] += 1
            continue
        totals[row.experiment_id] += 1
        if not row.setup_event_id or row.direction not in {"LONG", "SHORT"} or not row.symbol:
            reasons["invalid_identity"] += 1
            continue
        if _utc(row.signal_time) <= FREEZE:
            reasons["prefreeze"] += 1
            continue
        by_exp = groups.setdefault(_record_key(row), {POINT_B: [], BASELINE: []})
        by_exp[row.experiment_id].append(row)

    pairs: list[PairedRecord] = []
    for (event_id, direction), cohort in sorted(groups.items()):
        pb_rows, bl_rows = cohort[POINT_B], cohort[BASELINE]
        if len(pb_rows) > 1 or len(bl_rows) > 1:
            reasons["duplicate_event_identity"] += 1
            continue
        if not pb_rows:
            reasons["baseline_only_event"] += 1
            continue
        if not bl_rows:
            reasons["point_b_only_event"] += 1
            continue
        pb, bl = pb_rows[0], bl_rows[0]
        if pb.symbol != bl.symbol or pb.direction != bl.direction:
            reasons["symbol_or_direction_mismatch"] += 1
        elif _utc(bl.signal_time) > _utc(pb.signal_time):
            reasons["baseline_after_point_b"] += 1
        elif not pb.finalized or not bl.finalized or pb.gross_r is None or bl.gross_r is None:
            reasons["not_both_finalized"] += 1
        else:
            pairs.append(PairedRecord(event_id, pb.symbol, direction, bl, pb))
    return MatchReport(tuple(pairs), dict(sorted(reasons.items())),
                       totals[POINT_B], totals[BASELINE])
