"""Filter scanner signals by historical expectancy.

Loads expected R per (scanner_name, direction) from ``dds.scanner_expectancy``
(built on ``dds.signal_outcome``) and rejects candidates whose historical
``avg_r_after_costs`` is below a configurable threshold.

The source of truth is ``dds.scanner_expectancy`` which counts only entries
where ``entry_touched = true``, excluding NO_ENTRY and EXPIRED-only signals.
This avoids pollution from ``dds.paper_trade_stats`` which aggregates over
all paper-trade rows including entries that never actually opened.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.scanners.models import SetupCandidate

if TYPE_CHECKING:
    from app.db.repository import ScannerRepository

logger = logging.getLogger(__name__)

# Minimum *entries* (signals that touched the entry zone) needed before we
# trust the computed expectancy.  At least this many real entries are required
# before the filter begins rejecting candidates on statistical grounds.
DEFAULT_MIN_SAMPLES = 30


@dataclass(frozen=True)
class ExpectancyRecord:
    scanner_name: str
    direction: str
    samples: int          # entries (entry_touched) from signal_outcome
    avg_r_after_costs: float
    win_rate: float
    profit_factor: float = 0.0
    net_pnl: float = 0.0  # retained for logging only, not used in gate


@dataclass
class ExpectancyFilter:
    """In-memory lookup of scanner/direction expectancy.

    This is NOT updated live; callers should refresh periodically.
    """

    records: dict[tuple[str, str], ExpectancyRecord] = field(default_factory=dict)

    def is_profitable(
        self,
        scanner_name: str,
        direction: str,
        *,
        min_avg_r: float = 0.0,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        min_profit_factor: float = 1.20,
        min_net_pnl: float = 0.0,
        trading_mode: str = "paper",
    ) -> bool:
        """Allow execution only after the configured evidence gates pass.

        In PAPER mode, combinations with insufficient samples are allowed
        through for bootstrap data collection.  LIVE mode always enforces
        the full evidence gate.

        Gate criteria (when enough samples exist):
          * avg_r_after_costs > min_avg_r
          * profit_factor >= min_profit_factor

        ``net_pnl`` is intentionally excluded from the gate — it is a
        position-size-dependent absolute metric and does not reflect
        normalized expectancy.
        """
        key = (scanner_name, direction)
        rec = self.records.get(key)
        if rec is None or rec.samples < min_samples:
            # PAPER bootstrap: allow to accumulate stats.
            if trading_mode == "paper":
                return True
            return False
        return (
            rec.avg_r_after_costs > min_avg_r
            and rec.profit_factor >= min_profit_factor
        )

    def reason_for(self, scanner_name: str, direction: str) -> str:
        key = (scanner_name, direction)
        rec = self.records.get(key)
        if rec is None:
            return "INSUFFICIENT_DATA(0)"
        if rec.samples < DEFAULT_MIN_SAMPLES:
            return f"INSUFFICIENT_DATA({rec.samples})"
        return (
            f"AVG_R_AFTER_COSTS={rec.avg_r_after_costs:.4f},"
            f"PF={rec.profit_factor:.4f},"
            f"NET_PNL={rec.net_pnl:.2f}"
        )

    def to_dict(self) -> dict:
        return {f"{k[0]}|{k[1]}": {"samples": v.samples, "avg_r": v.avg_r_after_costs, "wr": v.win_rate}
                for k, v in self.records.items()}


def load_expectancy(repository: ScannerRepository) -> ExpectancyFilter:
    """Load scanner expectancy from ``dds.scanner_expectancy``.

    This view is built on ``dds.signal_outcome`` and counts only signals
    where ``entry_touched = true``, computing R-multiples after fees and
    slippage.  It is the canonical source for runtime expectancy gating.
    """
    if not repository._use_pg:
        return ExpectancyFilter()
    cursor = repository._conn.cursor()
    cursor.execute("""
        SELECT scanner_name, direction,
               entries, avg_r_after_costs, win_rate_on_entries,
               profit_factor
        FROM dds.scanner_expectancy
    """)
    f = ExpectancyFilter()
    for row in cursor.fetchall():
        key = (row[0], row[1])
        f.records[key] = ExpectancyRecord(
            scanner_name=row[0],
            direction=row[1],
            samples=int(row[2] or 0),
            avg_r_after_costs=float(row[3] or 0),
            win_rate=float(row[4] or 0),
            profit_factor=float(row[5] or 0),
        )
    logger.info(
        "loaded expectancy filter: %d scanner/direction records "
        "(source: dds.scanner_expectancy)",
        len(f.records),
    )
    return f


def filter_candidates(
    candidates: list[SetupCandidate],
    expectancy: ExpectancyFilter,
    *,
    min_avg_r: float = 0.0,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_profit_factor: float = 1.20,
    min_net_pnl: float = 0.0,
    enforce_expectancy: bool = True,
    blocked_combinations: frozenset[tuple[str, str]] = frozenset(),
    trading_mode: str = "paper",
) -> tuple[list[SetupCandidate], int]:
    """Filter candidates by manual blocks and historical expectancy.

    ``blocked_combinations`` rejects a scanner/direction pair regardless of its
    historical sample count.  In PAPER mode, combinations with fewer than
    ``min_samples`` closed trades are allowed through for bootstrap data
    collection.  Returns (accepted, rejected_count).
    """
    accepted: list[SetupCandidate] = []
    rejected = 0
    for c in candidates:
        combination = (c.scanner_name.upper(), c.direction.upper())
        if combination in blocked_combinations:
            rejected += 1
            logger.info(
                "signal block rejected: %s %s %s (reason: DISABLED_SCANNER_DIRECTION)",
                c.symbol, c.scanner_name, c.direction,
            )
            continue
        if not enforce_expectancy:
            accepted.append(c)
            continue
        if expectancy.is_profitable(
            c.scanner_name,
            c.direction,
            min_avg_r=min_avg_r,
            min_samples=min_samples,
            min_profit_factor=min_profit_factor,
            min_net_pnl=min_net_pnl,
            trading_mode=trading_mode,
        ):
            accepted.append(c)
        else:
            rejected += 1
            rec = expectancy.records.get((c.scanner_name, c.direction))
            if rec is not None and rec.samples < min_samples:
                logger.info(
                    "expectancy filter bypass: %s %s %s "
                    "(entries=%d, min_samples=%d, reason=INSUFFICIENT_SAMPLES)",
                    c.symbol, c.scanner_name, c.direction,
                    rec.samples, min_samples,
                )
            else:
                logger.info(
                    "expectancy filter rejected: %s %s %s "
                    "(entries=%s, avg_r_after_costs=%s, pf=%s, reason=NEGATIVE_EXPECTANCY)",
                    c.symbol, c.scanner_name, c.direction,
                    rec.samples if rec else "N/A",
                    f"{rec.avg_r_after_costs:.4f}" if rec else "N/A",
                    f"{rec.profit_factor:.4f}" if rec else "N/A",
                )
    return accepted, rejected
