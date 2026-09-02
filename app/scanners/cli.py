"""Scanner CLI entry point."""
from __future__ import annotations
import argparse
import json
import time
from datetime import datetime, timezone
from app.config import load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.scanners.context_builder import build_market_context
from app.scanners.orchestrator import ScannerOrchestrator

ALL_SCANNERS = [
    "LIQUIDITY_SWEEP_CHOCH_OB", "BREAKOUT_RETEST", "LIQUIDITY_REVERSAL",
    "TREND_PULLBACK_V2", "VOLATILITY_COMPRESSION", "SUPPORT_RESISTANCE_REACTION", "MOMENTUM_EXHAUSTION",
]


def print_banner() -> None:
    print("=" * 64)
    print(" MARKET SCANNER")
    print("=" * 64)


def print_setups(candidates: list) -> None:
    if not candidates:
        print("  No setups found.\n")
        return
    print(f"  {'Score':<6} {'Symbol':<12} {'TF':<5} {'Direction':<8} {'Scanner'}")
    print("  " + "-" * 60)
    for c in candidates:
        print(f"  {c.score:<6.0f} {c.symbol:<12} {c.setup_timeframe:<5} {c.direction:<8} {c.scanner_name}")
        if c.reasons:
            print(f"         Reasons: {', '.join(c.reasons)}")
        if c.target_1:
            print(f"         Entry: {c.entry_zone_low:.2f}-{c.entry_zone_high:.2f} | SL: {c.invalidation_price:.2f} | TP1: {c.target_1:.2f}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit Multi-Setup Market Scanner")
    parser.add_argument("--scanners", nargs="*", default=None)
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    settings = load_settings()
    client = BybitClient(settings)
    universe = settings.scanner_universe
    if args.symbols:
        symbols = args.symbols
    elif universe.mode == "dynamic":
        symbols = client.get_liquid_symbols(
            top_n=universe.top_n,
            min_turnover_24h=universe.min_turnover_24h,
            min_volume_24h=universe.min_volume_24h,
            quote_coin=universe.quote_coin,
        )
        if not symbols:
            parser.error("dynamic scanner universe is empty; check liquidity thresholds")
    else:
        symbols = list(settings.symbols)
    enabled = args.scanners or ALL_SCANNERS

    repository = ScannerRepository(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
    )
    repository.ensure_schema()
    orchestrator = ScannerOrchestrator(enabled_scanners=enabled, repository=repository)

    if not args.json:
        print_banner()
        print(f"Scanners: {len(orchestrator.scanners)}")
        print(f"Symbols: {', '.join(symbols)}")
        print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print()

    all_results: list[dict] = []
    start = time.time()

    for symbol in symbols:
        try:
            ctx = build_market_context(client, symbol, settings)
            candidates = orchestrator.scan_all(ctx)
            for c in candidates:
                repository.save_setup(c)
            if args.json:
                for c in candidates:
                    all_results.append({"symbol": c.symbol, "scanner": c.scanner_name, "direction": c.direction, "score": c.score, "entry_low": c.entry_zone_low, "entry_high": c.entry_zone_high, "invalidation": c.invalidation_price, "target_1": c.target_1, "target_2": c.target_2, "reasons": list(c.reasons), "detected_at": c.detected_at.isoformat()})
            else:
                if candidates:
                    print(f"  [{symbol}]")
                    print_setups(candidates)
                else:
                    print(f"  [{symbol}] No setups")
        except Exception as e:
            if args.json:
                all_results.append({"symbol": symbol, "error": str(e)})
            else:
                print(f"  [{symbol}] ERROR: {e}")

    elapsed = time.time() - start
    if args.json:
        print(json.dumps(all_results, indent=2))
    else:
        print("-" * 64)
        print(f"Duration: {elapsed:.1f}s | Scanned: {len(symbols)} symbols")
        print("=" * 64)


if __name__ == "__main__":
    main()
