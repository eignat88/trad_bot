"""Filter scanner signals by historical expectancy.

Loads expected R per (scanner_name, direction) from the database and rejects
candidates whose historical avg_r_after_costs is below a configurable threshold.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.scanners.models import SetupCandidate

if TYPE_CHECKING:
    from app.db.repository import ScannerRepository

logger = logging.getLogger(__name__)

# Minimum outcomes needed before we trust the average.
DEFAULT_MIN_SAMPLES = 30


@dataclass(frozen=True)
class ExpectancyRecord:
    scanner_name: str
    direction: str
    samples: int
    avg_r_after_costs: float
    win_rate: float
    profit_factor: float = 0.0
    net_pnl: float = 0.0


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
    ) -> bool:
        """Allow execution only after the configured evidence gates pass."""
        key = (scanner_name, direction)
        rec = self.records.get(key)
        if rec is None or rec.samples < min_samples:
            return False
        return (
            rec.avg_r_after_costs > min_avg_r
            and rec.profit_factor >= min_profit_factor
            and rec.net_pnl > min_net_pnl
        )

    def reason_for(self, scanner_name: str, direction: str) -> str:
        key = (scanner_name, direction)
        rec = self.records.get(key)
        if rec is None:
            return "INSUFFICIENT_DATA(0)"
        if rec.samples < DEFAULT_MIN_SAMPLES:
            return f"INSUFFICIENT_DATA({rec.samples})"
        return (
            f"AVG_R={rec.avg_r_after_costs:.4f},PF={rec.profit_factor:.4f},"
            f"NET_PNL={rec.net_pnl:.2f}"
        )

    def to_dict(self) -> dict:
        return {f"{k[0]}|{k[1]}": {"samples": v.samples, "avg_r": v.avg_r_after_costs, "wr": v.win_rate}
                for k, v in self.records.items()}


def load_expectancy(repository: ScannerRepository) -> ExpectancyFilter:
    """Load scanner expectancy from PostgreSQL."""
    if not repository._use_pg:
        return ExpectancyFilter()
    cursor = repository._conn.cursor()
    cursor.execute("""
        SELECT scanner_name, direction, closed, avg_r, win_rate,
               profit_factor, total_pnl_usdt
        FROM dds.paper_trade_stats
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
            net_pnl=float(row[6] or 0),
        )
    logger.info("loaded expectancy filter: %d scanner/direction records", len(f.records))
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
) -> tuple[list[SetupCandidate], int]:
    """Filter candidates by manual blocks and historical expectancy.

    ``blocked_combinations`` rejects a scanner/direction pair regardless of its
    historical sample count. Returns (accepted, rejected_count).
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
        ):
            accepted.append(c)
        else:
            rejected += 1
            logger.info(
                "expectancy filter rejected: %s %s %s (reason: %s)",
                c.symbol, c.scanner_name, c.direction,
                expectancy.reason_for(c.scanner_name, c.direction),
            )
    return accepted, rejected
