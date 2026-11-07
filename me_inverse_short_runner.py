"""ME_LONG_V2_INVERSE_SHORT_V1 runner — independent prospective SHORT paper.

Two modes:

``--dry-run``
    Pre-activation readiness validation: exercises the COMPLETE real contour
    (activation evaluation, source capture, entries, TP / SL / TIMEOUT exits,
    fees + slippage + funding, Net PnL) against an in-memory store.  No
    database, no activation fixation, no production state touched.

default (real contour process)
    Fail-closed startup: requires the runtime flag
    ``ME_INVERSE_SHORT_V1_ENABLED=1`` AND a RUNNING activation row whose
    frozen ``protocol_fingerprint`` matches the running code AND a non-null
    ``freeze_ts`` (fix separately with
    ``scripts/me_inverse_short_v1_activate.py``).  Without both the runner
    exits with code 2 and does nothing.

    The contour runs on a DEDICATED PostgreSQL connection (its own tables)
    and never reads or writes the production Paper engine state.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

from app.config import Settings, load_settings
from app.paper.me_long_v2_inverse_short_v1.runner import InverseShortContour, run_dry_run
from app.paper.me_long_v2_inverse_short_v1.store import connect_pg_store

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
logger = logging.getLogger("me_inverse_short_runner")

SHUTDOWN = False


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "me_inverse_short_trading.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def _handle_signal(signum, frame) -> None:  # noqa: ARG001 - signal signature
    global SHUTDOWN
    SHUTDOWN = True
    logger.info("shutdown requested (signal %s)", signum)


def _get_prices(client: Any, symbols: set[str]) -> dict[str, float]:
    """Fetch last prices for the contour's symbols only (fail-open per symbol)."""
    prices: dict[str, float] = {}
    if not symbols:
        return prices
    try:
        for ticker in client.get_tickers("linear"):
            sym = ticker.get("symbol", "")
            if sym in symbols:
                try:
                    prices[sym] = float(ticker["lastPrice"])
                except (KeyError, TypeError, ValueError):
                    pass
    except Exception:
        logger.exception("me inverse short: failed to fetch prices")
    return prices


def _get_funding_rates(client: Any, symbols: set[str]) -> dict[str, float]:
    """Observed funding rates only (review P2-1 tri-state contract).

    A symbol is present ONLY when a rate was actually observed; a fetch
    failure omits it entirely, so the engine sees ``None`` = UNKNOWN and
    leaves those intervals unsettled — it NEVER substitutes a fabricated
    or default zero rate.
    """
    rates: dict[str, float] = {}
    for symbol in sorted(symbols):
        try:
            rates[symbol] = client.get_funding_rate(symbol)
        except Exception:
            logger.exception("me inverse short: funding rate fetch failed for %s", symbol)
    return rates


def _contour_symbols(contour: InverseShortContour) -> set[str]:
    symbols = {p.symbol for p in contour.engine.open_positions.values()}
    symbols.update(e.symbol for e in contour.engine.pending_events())
    return symbols


def run_dry_run_cli(settings: Settings) -> int:
    """Run the pre-activation dry-run and print its report."""
    report = run_dry_run(settings)
    print(json.dumps(report, indent=2, default=str))
    if report.get("ok"):
        logger.info(
            "me inverse short dry-run OK: entries=%s net_pnl=%s",
            report.get("entries"), report.get("net_pnl"),
        )
        return 0
    logger.error("me inverse short dry-run FAILED: %s", report)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ME_LONG_V2_INVERSE_SHORT_V1 independent prospective SHORT paper contour",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="in-memory end-to-end validation; no DB, no activation required",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="run exactly one cycle and exit (real mode)",
    )
    parser.add_argument(
        "--interval", type=int, default=None,
        help="cycle interval in seconds (default: settings.position_monitor_interval)",
    )
    args = parser.parse_args(argv)

    settings = load_settings()

    if args.dry_run:
        if argv is None:
            setup_logging()
        return run_dry_run_cli(settings)

    setup_logging()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    store = connect_pg_store(
        host=settings.db_host,
        port=int(settings.db_port),
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        initial_balance=settings.initial_balance,
    )

    contour = InverseShortContour(settings, store)
    if not contour.activated:
        logger.critical(
            "me inverse short contour REFUSES to start: activation=%s "
            "(flag ME_INVERSE_SHORT_V1_ENABLED=1 + RUNNING activation row with "
            "the current protocol fingerprint + freeze_ts are both required; "
            "DRAINING (EXIT_ONLY) is accepted as a valid identity; "
            "status: IMPLEMENTATION_REQUIRED / NOT_DEPLOYED)",
            contour.activation_reason,
        )
        store.close()
        return 2

    if contour.exit_only:
        # Review P2-2: a restart that reads DRAINING with a valid identity
        # continues directly in EXIT_ONLY — it never returns to RUNNING and
        # never re-opens entries; existing positions keep being managed.
        logger.warning(
            "me inverse short contour EXIT_ONLY (DRAINING): managing "
            "EXISTING positions only — no new source captures, no new "
            "entries; STOP/TP/TIMEOUT, funding reconciliation, outbox "
            "replay and pending-close recovery continue. "
            "fingerprint=%s freeze_ts=%s (never rewritten on restore)",
            contour.activation.protocol_fingerprint[:16],
            contour.activation.freeze_ts.isoformat(),
        )

    logger.info(
        "me inverse short contour started: freeze_ts=%s fingerprint=%s "
        "open_positions=%d balance=%.2f",
        contour.activation.freeze_ts.isoformat(),
        contour.activation.protocol_fingerprint[:16],
        len(contour.engine.open_positions),
        contour.engine.balance,
    )

    from app.exchange.bybit_client import BybitClient
    client = BybitClient(settings)

    interval = int(args.interval or settings.position_monitor_interval)
    interval = max(1, interval)
    cycle = 0
    last_report = time.monotonic()

    while not SHUTDOWN:
        cycle += 1
        started = time.monotonic()
        try:
            symbols = _contour_symbols(contour)
            prices = _get_prices(client, symbols)
            open_symbols = {p.symbol for p in contour.engine.open_positions.values()}
            funding = _get_funding_rates(client, open_symbols)
            result = contour.run_cycle(prices, funding)
            if result["entries"] or result["exits"]:
                logger.info(
                    "me inverse short cycle: entries=%d exits=%s balance=%.2f "
                    "open=%d pending=%d",
                    result["entries"], result["exit_reasons"],
                    result["balance"], result["open_positions"],
                    result["pending_events"],
                )
            if time.monotonic() - last_report >= 300:
                logger.info(
                    "me inverse short snapshot: %s",
                    json.dumps(contour.engine.snapshot(prices), default=str),
                )
                last_report = time.monotonic()
        except Exception:
            logger.exception("me inverse short cycle failed (contour isolated)")

        if args.once:
            break
        delay = max(0, interval - (time.monotonic() - started))
        for _ in range(int(delay)):
            if SHUTDOWN:
                break
            time.sleep(1)

    try:
        logger.info(
            "me inverse short shutting down: %s",
            json.dumps(contour.engine.snapshot(), default=str),
        )
    except Exception:
        logger.debug("final snapshot failed", exc_info=True)
    if contour.engine.durability_risk:
        # Honest shutdown report (P1): never claim durable recovery for a
        # terminal snapshot the database never accepted.
        logger.critical(
            "me inverse short shutdown with UNRESOLVED close durability risk: "
            "%d terminal snapshot(s) exist only in process memory — recovery "
            "is NOT guaranteed if PostgreSQL was unavailable before any "
            "durable write succeeded: keys=%s",
            len(contour.engine.unstaged_close_keys),
            list(contour.engine.unstaged_close_keys),
        )
    store.close()
    logger.info("me inverse short runner stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
