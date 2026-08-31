"""Forward paper-test readiness checks for the live-trading gate."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings


@dataclass(frozen=True)
class PaperReadiness:
    forward_days: float
    closed_trades: int
    net_pnl_usdt: float
    max_drawdown: float
    avg_r: float
    profit_factor: float
    eligible: bool
    failed_checks: tuple[str, ...]


def assess_readiness(summary: dict[str, Any], settings: Settings) -> PaperReadiness:
    """Evaluate persisted paper results against explicit live-gate thresholds."""
    forward_days = float(summary.get("forward_days", 0.0) or 0.0)
    closed_trades = int(summary.get("closed_trades", 0) or 0)
    net_pnl_usdt = float(summary.get("net_pnl_usdt", 0.0) or 0.0)
    max_drawdown = float(summary.get("max_drawdown", 0.0) or 0.0)
    avg_r = float(summary.get("avg_r", 0.0) or 0.0)
    profit_factor = float(summary.get("profit_factor", 0.0) or 0.0)
    scanner_stats = summary.get("scanner_stats", []) or []

    failed: list[str] = []
    if forward_days < settings.paper_min_forward_days:
        failed.append(f"forward days {forward_days:.1f} < {settings.paper_min_forward_days}")
    if closed_trades < settings.paper_min_closed_trades:
        failed.append(f"closed trades {closed_trades} < {settings.paper_min_closed_trades}")
    if net_pnl_usdt <= 0:
        failed.append("net paper P&L is not positive")
    if max_drawdown > settings.paper_max_drawdown:
        failed.append(
            f"max drawdown {max_drawdown:.2%} > {settings.paper_max_drawdown:.2%}"
        )
    if avg_r <= settings.paper_min_avg_r:
        failed.append(f"avg R {avg_r:.2f} <= {settings.paper_min_avg_r:.2f}")
    if profit_factor <= settings.paper_min_profit_factor:
        failed.append(
            f"profit factor {profit_factor:.2f} <= {settings.paper_min_profit_factor:.2f}"
        )
    for row in scanner_stats:
        samples = int(row.get("closed", 0) or 0)
        if samples < settings.expectancy_min_samples:
            continue
        row_avg_r = float(row.get("avg_r", 0.0) or 0.0)
        row_pf = float(row.get("profit_factor", 0.0) or 0.0)
        if row_avg_r <= 0 or row_pf < 1.0:
            failed.append(
                "toxic scanner/direction "
                f"{row.get('scanner_name')}/{row.get('direction')} "
                f"(closed={samples}, avg_r={row_avg_r:.2f}, pf={row_pf:.2f})"
            )

    return PaperReadiness(
        forward_days=forward_days,
        closed_trades=closed_trades,
        net_pnl_usdt=net_pnl_usdt,
        max_drawdown=max_drawdown,
        avg_r=avg_r,
        profit_factor=profit_factor,
        eligible=not failed,
        failed_checks=tuple(failed),
    )
