from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean, pstdev
from typing import Iterable

from app.models import Trade


def calculate_metrics(trades: Iterable[Trade]) -> dict[str, float | int]:
    rows = [t for t in trades if t.status == "CLOSED"]
    if not rows:
        return {key: 0 for key in ("trades", "wins", "losses", "win_rate", "average_win",
            "average_loss", "average_r", "expectancy", "profit_factor", "net_profit",
            "max_drawdown", "sharpe_ratio", "max_consecutive_wins", "max_consecutive_losses",
            "average_trade_duration")}
    pnls, rs = [t.pnl_usdt for t in rows], [t.pnl_r for t in rows]
    wins, losses = [x for x in pnls if x > 0], [x for x in pnls if x < 0]
    win_rate = len(wins) / len(rows)
    avg_win = mean(wins) if wins else 0.0
    avg_loss = abs(mean(losses)) if losses else 0.0
    equity = peak = drawdown = 0.0
    win_run = loss_run = max_wins = max_losses = 0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        win_run, loss_run = ((win_run + 1, 0) if pnl > 0 else (0, loss_run + 1))
        max_wins, max_losses = max(max_wins, win_run), max(max_losses, loss_run)
    deviation = pstdev(rs) if len(rs) > 1 else 0.0
    return {"trades": len(rows), "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate, "average_win": avg_win, "average_loss": avg_loss,
        "average_r": mean(rs), "expectancy": win_rate * avg_win - (1 - win_rate) * avg_loss,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else (math.inf if wins else 0),
        "net_profit": sum(pnls), "max_drawdown": drawdown,
        "sharpe_ratio": mean(rs) / deviation * math.sqrt(len(rs)) if deviation else 0,
        "max_consecutive_wins": max_wins, "max_consecutive_losses": max_losses,
        "average_trade_duration": mean(t.duration for t in rows)}


def grouped_report(trades: Iterable[Trade]) -> dict[str, dict[str, dict[str, float | int]]]:
    rows = list(trades)
    result = {"overall": {"all": calculate_metrics(rows)}}
    extractors = {"symbol": lambda t: t.symbol, "setup": lambda t: t.setup,
                  "timeframe": lambda t: t.timeframe, "month": lambda t: t.month,
                  "direction": lambda t: t.direction}
    for dimension, extractor in extractors.items():
        groups: dict[str, list[Trade]] = defaultdict(list)
        for trade in rows:
            groups[extractor(trade)].append(trade)
        result[dimension] = {key: calculate_metrics(value) for key, value in groups.items()}
    return result
