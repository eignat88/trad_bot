"""CLI interface for paper trading gate.

Usage:
    python -m app.paper.cli status        # Show account + open trades
    python -m app.paper.cli trades        # List closed paper trades
    python -m app.paper.cli stats         # Aggregated stats by scanner
    python -m app.paper.cli run-once      # Run one entry/exit cycle
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from datetime import datetime, timezone

from app.config import Settings, load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.paper.engine import PaperTradingEngine
from app.paper.readiness import assess_readiness
from app.scanners.context_builder import build_market_context
from app.scanners.orchestrator import ScannerOrchestrator
from app.scanners.expectancy_filter import filter_candidates, load_expectancy

logger = logging.getLogger("paper_cli")


def _load_settings() -> Settings:
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent.parent
    return load_settings(path=root / "config.yaml", env_file=root / ".env")


def _emergency_stop_requested(settings: Settings) -> bool:
    stop_file = Path(settings.paper_emergency_stop_file)
    if not stop_file.is_absolute():
        stop_file = Path(__file__).resolve().parent.parent.parent / stop_file
    return stop_file.exists()


def _get_repo() -> ScannerRepository:
    """Paper state is PostgreSQL-only; fail fast rather than lose lifecycle data."""
    settings = _load_settings()
    try:
        return ScannerRepository(
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
            backend="postgres",
        )
    except Exception as exc:
        logger.critical(
            "PAPER_RUNNER_STOPPED: database unavailable and fallback cannot guarantee state consistency"
        )
        raise RuntimeError(
            "PAPER_RUNNER_STOPPED: database unavailable and fallback cannot guarantee state consistency"
        ) from exc


def _get_prices(client: BybitClient, symbols: list[str]) -> dict[str, float]:
    """Fetch latest close prices for a list of symbols."""
    prices = {}
    for ticker in client.get_tickers("linear"):
        sym = ticker.get("symbol", "")
        if sym in symbols:
            try:
                prices[sym] = float(ticker["lastPrice"])
            except (KeyError, TypeError, ValueError):
                pass
    return prices


def _get_funding_rates(client: BybitClient, symbols: list[str]) -> dict[str, float]:
    rates: dict[str, float] = {}
    for symbol in symbols:
        try:
            rates[symbol] = client.get_funding_rate(symbol)
        except Exception:
            logger.exception("failed to fetch funding rate for %s", symbol)
    return rates


def cmd_status(args: argparse.Namespace) -> None:
    """Show paper trading status."""
    repo = _get_repo()
    settings = _load_settings()
    engine = PaperTradingEngine(settings, repo)

    snap = engine.snapshot()
    print(f"\n{'='*60}")
    print(f"  PAPER TRADING ACCOUNT")
    print(f"{'='*60}")
    print(f"  Balance:      ${snap['balance']:,.2f}")
    print(f"  Starting:     ${snap['starting_balance']:,.2f}")
    print(f"  Total P&L:    ${snap['total_pnl']:+,.2f} ({snap['total_pnl_pct']:+.2f}%)")
    print(f"  Max Drawdown: {snap['max_drawdown_pct']:.2f}%")
    print(f"  Open Trades:  {snap['open_positions']}")

    if snap['open_trades']:
        print(f"\n  {'Symbol':<14} {'Dir':<6} {'Scanner':<28} {'Entry':>10} {'Stop':>10} {'TP1':>10}")
        print(f"  {'-'*14} {'-'*6} {'-'*28} {'-'*10} {'-'*10} {'-'*10}")
        for t in snap['open_trades']:
            tp1 = f"${t['tp1']:,.2f}" if t['tp1'] else "—"
            print(f"  {t['symbol']:<14} {t['direction']:<6} {t['scanner']:<28} ${t['entry']:>9,.2f} ${t['stop']:>9,.2f} {tp1:>10}")

    # DB stats
    stats = repo.get_paper_trade_stats()
    if stats:
        print(f"\n  {'='*60}")
        print(f"  PERFORMANCE BY SCANNER")
        print(f"  {'='*60}")
        print(f"  {'Scanner':<28} {'Dir':<6} {'Trades':>6} {'Wins':>5} {'WR':>6} {'AvgR':>7} {'TotalPnL':>10}")
        print(f"  {'-'*28} {'-'*6} {'-'*6} {'-'*5} {'-'*6} {'-'*7} {'-'*10}")
        for s in stats:
            wr = f"{s.get('win_rate', 0) or 0:.0%}" if s.get('win_rate') else "—"
            avg_r = f"{s.get('avg_r', 0) or 0:.2f}"
            pnl = f"${s.get('total_pnl_usdt', 0) or 0:+,.2f}"
            print(f"  {s['scanner_name']:<28} {s['direction']:<6} {s['total_trades']:>6} {s.get('wins', 0) or 0:>5} {wr:>6} {avg_r:>7} {pnl:>10}")

    print()
    repo.close()


def cmd_readiness(args: argparse.Namespace) -> None:
    """Show whether accumulated paper results satisfy the live-gate thresholds."""
    repo = _get_repo()
    try:
        readiness = assess_readiness(repo.get_paper_forward_summary(), _load_settings())
        print("\nPAPER FORWARD-TEST READINESS")
        print(f"  Forward days:  {readiness.forward_days:.1f}")
        print(f"  Closed trades: {readiness.closed_trades}")
        print(f"  Net P&L:       ${readiness.net_pnl_usdt:+,.2f}")
        print(f"  Max drawdown:  {readiness.max_drawdown:.2%}")
        print(f"  Eligible:      {'YES' if readiness.eligible else 'NO'}")
        for reason in readiness.failed_checks:
            print(f"  - {reason}")
    finally:
        repo.close()


def cmd_trades(args: argparse.Namespace) -> None:
    """List closed paper trades."""
    repo = _get_repo()
    if not repo._use_pg:
        print("PostgreSQL required for paper trades")
        return

    limit = args.limit or 20
    cursor = repo._conn.cursor()
    cursor.execute(
        """
        SELECT symbol, direction, scanner_name, entry_price, exit_price,
               exit_reason, pnl_usdt, pnl_r, pnl_percent, duration_sec,
               entered_at, closed_at
        FROM dds.paper_trade
        WHERE status = 'CLOSED'
        ORDER BY closed_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    rows = cursor.fetchall()

    if not rows:
        print("No closed paper trades yet.")
        repo.close()
        return

    print(f"\n{'Symbol':<14} {'Dir':<6} {'Scanner':<22} {'Entry':>10} {'Exit':>10} {'Reason':<16} {'PnL':>9} {'R':>6} {'Dur':>7}")
    print(f"{'-'*14} {'-'*6} {'-'*22} {'-'*10} {'-'*10} {'-'*16} {'-'*9} {'-'*6} {'-'*7}")
    for r in rows:
        sym, d, scan, entry, exit_p, reason, pnl, r_mult, pnl_pct, dur, entered, closed = r
        dur_str = f"{dur:.0f}s" if dur else "—"
        dur_min = f"{dur/60:.0f}m" if dur and dur > 60 else dur_str
        print(f"{sym:<14} {d:<6} {scan:<22} ${entry:>9,.2f} ${exit_p:>9,.2f} {reason or '':<16} ${pnl:>+8,.2f} {r_mult:>+5.2f} {dur_min:>7}")
    print()
    repo.close()


def cmd_stats(args: argparse.Namespace) -> None:
    """Show aggregated stats."""
    repo = _get_repo()
    stats = repo.get_paper_trade_stats()

    if not stats:
        print("No paper trade stats yet.")
        repo.close()
        return

    print(f"\n{'Scanner':<28} {'Dir':<6} {'Total':>6} {'Closed':>7} {'Wins':>5} {'Losses':>6} {'WR':>7} {'PF':>7} {'AvgR':>7} {'TotalPnL':>10} {'AvgDur':>8}")
    print(f"{'-'*28} {'-'*6} {'-'*6} {'-'*7} {'-'*5} {'-'*6} {'-'*7} {'-'*7} {'-'*7} {'-'*10} {'-'*8}")
    for s in stats:
        wr = f"{s.get('win_rate', 0) or 0:.1%}" if s.get('win_rate') else "—"
        pf = f"{s.get('profit_factor', 0) or 0:.2f}" if s.get('profit_factor') else "—"
        avg_r = f"{s.get('avg_r', 0) or 0:.2f}"
        pnl = f"${s.get('total_pnl_usdt', 0) or 0:+,.2f}"
        dur = s.get('avg_duration_sec')
        dur_str = f"{dur/60:.0f}m" if dur and dur > 60 else (f"{dur:.0f}s" if dur else "—")
        print(f"{s['scanner_name']:<28} {s['direction']:<6} {s['total_trades']:>6} {s.get('closed', 0) or 0:>7} {s.get('wins', 0) or 0:>5} {s.get('losses', 0) or 0:>6} {wr:>7} {pf:>7} {avg_r:>7} {pnl:>10} {dur_str:>8}")
    print()
    repo.close()


def _load_ready_setups(repo: ScannerRepository) -> list[dict]:
    """Load READY_TO_TRADE setups directly from DB — no re-scanning."""
    cursor = repo._conn.cursor()
    cursor.execute(
        """
        SELECT s.setup_id, i.symbol, s.scanner_name, s.direction, s.score,
               s.entry_zone_low, s.entry_zone_high, s.invalidation_price,
               s.target_1, s.target_2, s.market_regime, s.detected_at,
               s.reference_price
        FROM dds.scanner_setup s
        JOIN dds.instrument i ON i.instrument_id = s.instrument_id
        WHERE s.status = 'READY_TO_TRADE'
          AND s.detected_at > now() - interval '2 hours'
        ORDER BY s.score DESC
        """
    )
    rows = cursor.fetchall()
    return [
        {
            "setup_id": r[0], "symbol": r[1], "scanner_name": r[2],
            "direction": r[3], "score": float(r[4]),
            "entry_zone_low": float(r[5]) if r[5] else 0.0,
            "entry_zone_high": float(r[6]) if r[6] else 0.0,
            "invalidation_price": float(r[7]) if r[7] else 0.0,
            "target_1": float(r[8]) if r[8] else None,
            "target_2": float(r[9]) if r[9] else None,
            "market_regime": r[10],
            "detected_at": r[11],
            "reference_price": float(r[12]) if r[12] else 0.0,
        }
        for r in rows
    ]


def cmd_run_once(args: argparse.Namespace) -> None:
    """Run one paper trading cycle: check entries + check exits.

    This does NOT re-scan via Bybit. It reads READY_TO_TRADE setups from DB
    and only fetches current prices for entry-zone checks.
    """
    settings = _load_settings()
    repo = _get_repo()
    client = BybitClient(settings)

    # Initialize engine (loads existing open trades from DB)
    engine = PaperTradingEngine(settings, repo)

    # --- 1. CHECK EXITS (monitor existing open positions) ---
    open_symbols = list(engine.open_trades.keys())
    if open_symbols:
        prices = _get_prices(client, open_symbols)
        closed = engine.check_exits(prices, _get_funding_rates(client, open_symbols))
        if closed:
            logger.info("paper: %d trades closed this cycle", len(closed))

    # --- 2. CHECK ENTRIES (read READY_TO_TRADE from DB, fetch prices only) ---
    ready_setups = [] if _emergency_stop_requested(settings) else _load_ready_setups(repo)
    if _emergency_stop_requested(settings):
        logger.critical("paper emergency stop is active: new entries are disabled")
    if not ready_setups:
        logger.info("no READY_TO_TRADE setups found")
    else:
        # Deduplicate symbols
        needed_symbols = list({s["symbol"] for s in ready_setups} | set(engine.open_trades.keys()))
        prices = _get_prices(client, needed_symbols)
        logger.info("fetched prices for %d symbols, %d READY_TO_TRADE setups", len(prices), len(ready_setups))

        # Convert dicts to SetupCandidate-like objects for engine.check_entries
        from dataclasses import dataclass
        from app.scanners.models import SetupCandidate
        candidates = []
        for s in ready_setups:
            c = SetupCandidate(
                setup_id=s["setup_id"],
                scanner_name=s["scanner_name"],
                symbol=s["symbol"],
                direction=s["direction"],
                score=s["score"],
                entry_zone_low=s["entry_zone_low"],
                entry_zone_high=s["entry_zone_high"],
                invalidation_price=s["invalidation_price"],
                target_1=s["target_1"],
                target_2=s["target_2"],
                market_regime=s["market_regime"],
                reference_price=s["reference_price"],
            )
            candidates.append(c)

        # Manual scanner/direction blocks are safety controls and therefore
        # apply independently of the statistical expectancy feature flag.
        expectancy = load_expectancy(repo) if settings.expectancy_filter_enabled else None
        from app.scanners.expectancy_filter import ExpectancyFilter
        candidates, rejected = filter_candidates(
            candidates,
            expectancy or ExpectancyFilter(),
            min_avg_r=settings.expectancy_min_avg_r,
            min_samples=settings.expectancy_min_samples,
            min_profit_factor=settings.expectancy_min_profit_factor,
            min_net_pnl=settings.expectancy_min_net_pnl,
            enforce_expectancy=settings.expectancy_filter_enabled,
            blocked_combinations=frozenset(settings.blocked_scanner_directions),
            trading_mode=settings.trading_mode,
        )
        if rejected:
            logger.info("paper: candidate filter rejected %d setups", rejected)

        opened = engine.check_entries(candidates, prices)
        if opened:
            logger.info("paper: %d trades opened this cycle", len(opened))
        else:
            logger.info("paper: no entries this cycle (%d setups checked)", len(candidates))

    # --- 3. EXPIRE OLD SETUPS ---
    expired = repo.expire_stale_setups(max_age_minutes=120)
    if expired:
        logger.info("expired %d stale setups", expired)

    # --- 4. ACCOUNT SNAPSHOT ---
    stats_rows = repo.get_paper_trade_stats()
    total_trades = sum(s.get("total_trades", 0) or 0 for s in stats_rows)
    winning = sum(s.get("wins", 0) or 0 for s in stats_rows)
    losing = sum(s.get("losses", 0) or 0 for s in stats_rows)
    total_pnl = sum(s.get("total_pnl_usdt", 0) or 0 for s in stats_rows)

    account = engine.snapshot()
    repo.save_paper_account_snapshot(
        balance=account["balance"],
        equity=account["equity"],
        open_positions=len(engine.open_trades),
        total_trades=total_trades,
        winning_trades=winning,
        losing_trades=losing,
        total_pnl=total_pnl,
        max_drawdown=engine._max_drawdown,
    )

    print(json.dumps(account, indent=2))
    repo.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper Trading CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show account status and open trades")
    sub.add_parser("trades", help="List closed paper trades")
    sub.add_parser("stats", help="Aggregated stats by scanner")
    sub.add_parser("readiness", help="Check forward paper results against the live gate")
    sub.add_parser("run-once", help="Run one entry/exit cycle")

    # trades --limit N
    sub.choices["trades"].add_argument("--limit", type=int, default=20)

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.command == "status":
        cmd_status(args)
    elif args.command == "trades":
        cmd_trades(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "readiness":
        cmd_readiness(args)
    elif args.command == "run-once":
        cmd_run_once(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
