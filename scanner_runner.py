"""Scanner Runner - continuous background scanner that writes to PostgreSQL.

Runs on system startup via Windows Startup folder.
Scans every 5 minutes, saves results to PostgreSQL, logs to file.
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings, load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient
from app.scanners.context_builder import build_market_context
from app.scanners.direction_gate import ScannerDirectionGatePolicy
from app.scanners.expectancy_filter import ExpectancyFilter, load_expectancy
from app.scanners.funnel_diagnostics import flush_and_persist_all
from app.scanners.models import SetupCandidate
from app.scanners.orchestrator import ScannerOrchestrator

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"
LOG_DIR = PROJECT_ROOT / "logs"
logger = logging.getLogger("scanner_runner")


def setup_logging() -> None:
    """Configure runner logging only when the executable is started."""
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "scanner.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )

SHUTDOWN = False


def _load_runner_settings() -> Settings:
    """Load settings from files next to this runner, regardless of cwd."""
    settings = load_settings(path=CONFIG_PATH, env_file=ENV_PATH)
    logger.info(
        "scanner config: universe_mode=%s top_n=%s symbols=%s cwd=%s config=%s env_file=%s",
        settings.scanner_universe.mode,
        settings.scanner_universe.top_n,
        list(settings.symbols),
        Path.cwd(),
        CONFIG_PATH,
        ENV_PATH,
    )
    return settings


def _handle_signal(signum, frame):
    global SHUTDOWN
    logger.info("shutdown signal received")
    SHUTDOWN = True


def get_scanner_symbols(client: BybitClient, settings: Settings) -> list[str]:
    """Return the scanner universe selected by the current configuration."""
    return [str(item["symbol"]) for item in get_scanner_universe(client, settings)]


def get_scanner_universe(
    client: BybitClient, settings: Settings,
) -> list[dict[str, Any]]:
    """Return the ranked universe and the metadata used to select it."""
    universe = settings.scanner_universe
    if universe.mode == "dynamic":
        instruments = client.get_liquid_instruments(
            top_n=universe.top_n,
            min_turnover_24h=universe.min_turnover_24h,
            min_volume_24h=universe.min_volume_24h,
            quote_coin=universe.quote_coin,
        )
        if not instruments:
            raise RuntimeError(
                "Dynamic scanner universe is empty; check liquidity thresholds"
            )
        return instruments

    symbols = list(settings.symbols)
    if not symbols:
        raise RuntimeError("Static scanner universe is empty")
    return [{"symbol": symbol, "rank": rank}
            for rank, symbol in enumerate(symbols, start=1)]


def seconds_until_next_cycle(started_at: float, interval: int, now: float) -> float:
    """Return the remaining delay, keeping cycles anchored to their start."""
    return max(0, interval - (now - started_at))


# --- Shadow/FVG Filter OOS experiment ----------------------------------

_fvg_shadow_observer = None


def _get_fvg_shadow_observer(repository: ScannerRepository):
    """Lazy-init the FVG filter shadow observer (singleton per process)."""
    global _fvg_shadow_observer
    if _fvg_shadow_observer is None:
        from app.shadow.fvg_filter_shadow import FVGFilterShadowObserver
        _fvg_shadow_observer = FVGFilterShadowObserver(repository)
    return _fvg_shadow_observer


def _observe_fvg_shadow(repository: ScannerRepository, candidate: SetupCandidate) -> None:
    """Record an FVG LONG signal for the shadow filter experiment.

    Called from the scanner runner after a FVG LONG setup is saved.
    This is a fire-and-forget observation — never blocks the signal.
    """
    observer = _get_fvg_shadow_observer(repository)
    features = dict(candidate.features) if candidate.features else {}
    # Ensure score and market_regime are in features for snapshot
    features.setdefault("score", candidate.score)
    features.setdefault("market_regime", candidate.market_regime)

    # Get instrument_id from the repository
    instrument_id = None
    if repository._use_pg:
        instrument_id = repository.ensure_instrument(candidate.symbol)

    observer.observe_signal(
        setup_id=str(candidate.setup_id),
        symbol=candidate.symbol,
        instrument_id=instrument_id,
        direction=candidate.direction,
        detected_at=candidate.detected_at,
        features=features,
    )


# --- SRR LONG Research Experiment ------------------------------------------

_srr_research_observer = None


def _get_srr_research_observer(repository: ScannerRepository):
    """Lazy-init the SRR research observer (singleton per process)."""
    global _srr_research_observer
    if _srr_research_observer is None:
        from app.shadow.srr_research_observer import SRRResearchObserver
        _srr_research_observer = SRRResearchObserver(repository._conn)
    return _srr_research_observer


def _observe_srr_long_research(repository: ScannerRepository, candidate: SetupCandidate) -> None:
    """Record an SRR LONG candidate for research outcome tracking.

    Called from the scanner runner for every valid SRR LONG candidate,
    regardless of whether it is later blocked or traded by the direction gate.
    Research capture is independent of paper trading — never enables trading.
    """
    observer = _get_srr_research_observer(repository)
    features = dict(candidate.features) if candidate.features else {}

    observer.observe_blocked_candidate(
        setup_id=str(candidate.setup_id),
        scanner_name=candidate.scanner_name,
        scanner_version=candidate.scanner_version,
        symbol=candidate.symbol,
        direction=candidate.direction,
        detected_at=candidate.detected_at,
        signal_candle_open_time=candidate.signal_candle_open_time,
        reference_price=candidate.reference_price,
        entry_zone_low=candidate.entry_zone_low,
        entry_zone_high=candidate.entry_zone_high,
        invalidation_price=candidate.invalidation_price,
        target_1=candidate.target_1,
        target_2=candidate.target_2,
        score=candidate.score,
        market_regime=candidate.market_regime,
        features=features,
        reasons=candidate.reasons,
    )


# --- Generic Research Framework V1 ------------------------------------------

_research_observer = None


def _get_research_observer(repository: ScannerRepository):
    """Lazy-init the generic research observer (singleton per process)."""
    global _research_observer
    if _research_observer is None:
        try:
            from app.research.observer import ResearchObserver
            from app.research.repository import ResearchRepository
            from app.research.adapters.momentum_exhaustion_r import EXPERIMENT_CONFIG as MER_CONFIG

            research_repo = ResearchRepository(repository._conn)
            experiments = {
                MER_CONFIG["scanner_name"]: MER_CONFIG,
            }
            _research_observer = ResearchObserver(research_repo, experiments)
        except Exception:
            logger.debug("research observer init failed — research capture disabled", exc_info=True)
    return _research_observer


# --- ME_R_LONG_CLOSE_LOCATION_OOS Experiment ------------------------------

_me_r_long_cl_oos_observer = None


def _get_me_r_long_cl_oos_observer(repository: ScannerRepository):
    """Lazy-init the ME_R_LONG_CLOSE_LOCATION_OOS observer (singleton per process)."""
    global _me_r_long_cl_oos_observer
    if _me_r_long_cl_oos_observer is None:
        from app.shadow.me_r_long_close_location_oos_repository import MERLongCLoOosRepository
        _me_r_long_cl_oos_observer = MERLongCLoOosRepository(repository._conn)
    return _me_r_long_cl_oos_observer


def _observe_me_r_long_cl_oos(repository: ScannerRepository, candidate: SetupCandidate) -> None:
    """Record an ME_R_LONG_CLOSE_LOCATION_OOS candidate for OOS analysis.

    Called from the scanner runner for every candidate from the OOS scanner.
    Both PASS and REJECT candidates are saved for OOS analysis.
    This is a fire-and-forget observation — never blocks the signal.
    """
    from app.shadow.me_r_long_close_location_oos_repository import MERLongCLoOosRepository
    from app.shadow.me_r_long_close_location_oos_runner import observe_oos_candidate

    repo = MERLongCLoOosRepository(repository._conn)
    observe_oos_candidate(repo, candidate)


def _run_oos_evaluator(client: BybitClient, repository: ScannerRepository) -> None:
    """Run OOS evaluator for ME_R_LONG_CLOSE_LOCATION_OOS experiment.

    Evaluates pending signals and updates outcomes for mature horizons.
    Called periodically from the scanner main loop.
    """
    from app.shadow.me_r_long_close_location_oos_evaluator import MERLongCLoOosEvaluator
    from app.shadow.me_r_long_close_location_oos_repository import MERLongCLoOosRepository

    repo = MERLongCLoOosRepository(repository._conn)
    evaluator = MERLongCLoOosEvaluator(client, repo)

    # Run evaluation cycle
    stats = evaluator.evaluate_pending(limit=100)

    # Log summary
    logger.info(
        "ME_R_LONG_CLOSE_LOCATION_OOS evaluator: checked=%d updated=%d errors=%d",
        stats["checked"], stats["updated"], stats["errors"],
    )


def run_scan_cycle(
    client: BybitClient,
    orchestrator: ScannerOrchestrator,
    repository: ScannerRepository,
    symbols: list[str],
    run_id: int | None,
    settings: Settings | None = None,
    expectancy_filter: ExpectancyFilter | None = None,
) -> tuple[int, int, int]:
    """Returns (total_found, scanned, failed)."""
    settings = settings or _load_runner_settings()
    total_found = 0
    scanned = 0
    failed = 0
    expectancy_rejected = 0
    run_stats = {
        name: {"symbols_scanned": 0, "candidates_found": 0,
               "setups_saved": 0, "errors_count": 0, "duration_ms": 0.0}
        for name in orchestrator.scanners
    }

    if not symbols:
        return 0, 0, 0

    min_avg_r = settings.expectancy_min_avg_r if settings.expectancy_filter_enabled else 0.0
    # One SELECT per scan cycle; DB errors retain Settings as a fail-safe policy.
    gate_policy = ScannerDirectionGatePolicy.load_for_cycle(
        repository,
        scanner_names=orchestrator.scanners.keys(),
        blocked_combinations=settings.blocked_scanner_directions,
        regime_whitelist=settings.scanner_regime_whitelist,
    )
    workers = min(settings.scanner_workers, len(symbols))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="market-data") as executor:
        futures = {
            executor.submit(build_market_context, client, symbol, settings): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                ctx = future.result()
                scanned += 1
            except Exception:
                logger.exception("market data failed for %s", symbol)
                failed += 1
                repository.save_error(
                    symbol=symbol, scanner_name="ALL",
                    run_id=run_id,
                    error_type="MARKET_DATA_ERROR",
                    error_message=str(sys.exc_info()[1]),
                )
                continue

            try:
                if settings.expectancy_filter_enabled:
                    candidates, symbol_stats = orchestrator.scan_all_with_stats(
                        ctx,
                        expectancy_filter=expectancy_filter,
                        min_avg_r=min_avg_r,
                        min_samples=settings.expectancy_min_samples,
                        gate_policy=gate_policy,
                        regime_filter=settings.regime_filter_enabled,
                        scanner_regime_whitelist=settings.scanner_regime_whitelist,
                        trading_mode=settings.trading_mode,
                    )
                else:
                    candidates, symbol_stats = orchestrator.scan_all_with_stats(
                        ctx,
                        gate_policy=gate_policy,
                        regime_filter=settings.regime_filter_enabled,
                        scanner_regime_whitelist=settings.scanner_regime_whitelist,
                    )
                for name, values in symbol_stats.items():
                    # Skip metadata keys (e.g. _research_candidates)
                    if name.startswith("_"):
                        continue
                    stat = run_stats[name]
                    stat["symbols_scanned"] += 1
                    for field in ("candidates_found", "setups_saved", "errors_count", "duration_ms"):
                        stat[field] += values[field]

                for c in candidates:
                    repository.save_setup(c, run_id=run_id)
                    repository.save_event(
                        "SETUP_DETECTED", c.scanner_name, symbol,
                        run_id=run_id,
                        timeframe=c.setup_timeframe,
                        direction=c.direction,
                        score=c.score,
                        detected_at=c.detected_at,
                        payload={"entry_zone": [c.entry_zone_low, c.entry_zone_high]},
                    )

                    # Shadow/FVG Filter OOS experiment: classify every FVG LONG signal
                    if (c.scanner_name == "FVG_REACTION_LONG_LOCAL_STRUCT_V1"
                            and c.direction == "LONG"):
                        try:
                            _observe_fvg_shadow(repository, c)
                        except Exception:
                            logger.debug("FVG shadow observation failed", exc_info=True)

                    # ME_R_LONG_CLOSE_LOCATION_OOS: capture every OOS scanner candidate
                    # Both PASS and REJECT are saved for OOS analysis
                    if c.scanner_name == "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1":
                        try:
                            _observe_me_r_long_cl_oos(repository, c)
                        except Exception:
                            logger.debug("ME_R_LONG_CL_OOS observation failed", exc_info=True)

                # SRR LONG Research: capture every SRR LONG candidate for
                # research outcome tracking (independent of paper trading).
                research_candidates = symbol_stats.get("_research_candidates", [])
                for rc in research_candidates:
                    try:
                        _observe_srr_long_research(repository, rc)
                    except Exception:
                        logger.debug("SRR research observation failed", exc_info=True)

                total_found += len(candidates)
                if candidates:
                    best = max(candidates, key=lambda c: c.score)
                    logger.info(
                        "%s: %d setups (best: %s %s score=%.0f)",
                        symbol, len(candidates), best.direction,
                        best.scanner_name, best.score,
                    )
            except Exception:
                logger.exception("scan failed for %s", symbol)
                failed += 1
                repository.save_error(
                    symbol=symbol, scanner_name="SCANNER",
                    run_id=run_id,
                    error_type="SCAN_ERROR",
                    error_message=str(sys.exc_info()[1]),
                )

    for scanner_name, values in run_stats.items():
        repository.save_run_stat(run_id, scanner_name, **values)

    # FVG lifecycle observability: one INFO line per scanner cycle
    fvg_scanner = orchestrator.scanners.get("FVG_REACTION_LONG_LOCAL_STRUCT_V1")
    if fvg_scanner is not None:
        log_summary = getattr(fvg_scanner, "log_lifecycle_summary", None)
        if callable(log_summary):
            try:
                log_summary()
            except Exception:
                logger.debug("FVG lifecycle summary logging failed", exc_info=True)

    return total_found, scanned, failed


def main() -> None:
    setup_logging()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    settings = _load_runner_settings()
    client = BybitClient(settings)
    universe = get_scanner_universe(client, settings)
    symbols = [str(item["symbol"]) for item in universe]

    repository = ScannerRepository(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        backend="postgres",
    )

    # Verify DB connectivity (schema must be applied separately via schema.sql)
    if not repository.ping():
        logger.error("database health check failed")
        repository.close()
        raise SystemExit("database health check failed")

    if not repository.acquire_runner_lock():
        # Try to clear stale idle backends holding the advisory lock
        killed = repository.cleanup_stale_advisory_lock()
        if killed:
            logger.warning("cleaned up %d stale advisory lock holder(s)", killed)
            if not repository.acquire_runner_lock():
                logger.error("scanner runner lock still held after cleanup")
                repository.close()
                raise SystemExit("scanner runner already active")
        else:
            logger.error(
                "scanner runner lock held by active process — terminating"
            )
            repository.close()
            raise SystemExit("scanner runner already active")
    repository.abort_stale_runs()

    # Generic research observer: fail-open, never blocks scanner
    research_obs = _get_research_observer(repository)
    if research_obs is not None:
        logger.info("generic research observer enabled: experiments=%s", list(research_obs._experiments.keys()))

    orchestrator = ScannerOrchestrator(repository=repository, research_observer=research_obs)

    # Sync scanner direction gates to config.scanner_direction_gate (canonical source)
    try:
        gate_counts = repository.sync_scanner_direction_gate(
            registered_scanners=list(orchestrator.scanners.keys()),
            blocked_combinations=frozenset(settings.blocked_scanner_directions),
            regime_whitelist=settings.scanner_regime_whitelist,
        )
        logger.info(
            "scanner direction gate synced: %d scanners, %d combinations, "
            "%d blocked, %d regime, %d enabled, %d preserved (MANUAL)",
            gate_counts["scanners"], gate_counts["combinations"],
            gate_counts["blocked"], gate_counts["regime"],
            gate_counts["enabled"], gate_counts["preserved"],
        )
    except Exception:
        logger.exception("scanner direction gate sync failed — continuing with existing gates")

    # Load expectancy filter if enabled
    expectancy_filter = None
    if settings.expectancy_filter_enabled:
        expectancy_filter = load_expectancy(repository)
        logger.info(
            "expectancy filter loaded: %d records, min_avg_r=%.4f, min_samples=%d",
            len(expectancy_filter.records), settings.expectancy_min_avg_r,
            settings.expectancy_min_samples,
        )
    else:
        logger.info("expectancy filter disabled")

    logger.info(
        "scanner started: symbols=%s scanners=%d interval=%ds expectancy_filter=%s",
        symbols, len(orchestrator.scanners), settings.scan_interval,
        "ON" if expectancy_filter else "OFF",
    )

    cycle = 0
    while not SHUTDOWN:
        # ------------------------------------------------------------------
        # Phase 1: Refresh universe (may involve Bybit API calls)
        # ------------------------------------------------------------------
        t_phase_start = time.monotonic()
        try:
            universe = get_scanner_universe(client, settings)
            symbols = [str(item["symbol"]) for item in universe]
            logger.info("scanner universe refreshed: %d symbols", len(symbols))
        except Exception:
            # Keep the last good universe after a transient Bybit failure and
            # retry the refresh at the beginning of the next cycle.
            logger.exception("failed to refresh scanner universe")

        cycle += 1
        start = time.monotonic()
        t_universe_done = start
        logger.info(
            "cycle #%d phase: universe_refresh=%.2fs",
            cycle, t_universe_done - t_phase_start,
        )

        # ------------------------------------------------------------------
        # Phase 2: Create scanner_run — started_at is set here
        # ------------------------------------------------------------------
        try:
            run_id = repository.start_run(
                symbols_total=len(symbols),
                universe_mode=settings.scanner_universe.mode,
            )
            repository.save_run_universe(run_id, universe)
        except Exception:
            logger.warning("DB error starting run, attempting reconnect")
            if repository.reconnect():
                try:
                    run_id = repository.start_run(
                        symbols_total=len(symbols),
                        universe_mode=settings.scanner_universe.mode,
                    )
                    repository.save_run_universe(run_id, universe)
                except Exception:
                    logger.exception("run start failed after reconnect")
                    run_id = None
            else:
                logger.error("reconnect failed, skipping cycle")
                run_id = None

        t_run_started = time.monotonic()
        logger.info(
            "cycle #%d phase: run_create=%.2fs (started_at set by PostgreSQL now())",
            cycle, t_run_started - start,
        )

        # ------------------------------------------------------------------
        # Phase 3: Scan cycle — the actual instrument processing
        # ------------------------------------------------------------------
        try:
            total, scanned, failed = run_scan_cycle(
                client, orchestrator, repository, symbols, run_id, settings,
                expectancy_filter=expectancy_filter,
            )

            # -- SIGNAL_FUNNEL_DIAGNOSTICS_V1: flush counters to DB --
            # Fail-open: DB errors are logged but do not interrupt the cycle.
            try:
                flush_and_persist_all(repository)
            except Exception:
                logger.debug("signal funnel flush failed", exc_info=True)

            repository.expire_setups()

            # Aggregate signals
            active_signals = 0
            try:
                active_signals = repository.aggregate_signals(run_id)
            except Exception:
                logger.exception("signal aggregation failed")

            elapsed = time.monotonic() - start

            # Finish the run
            repository.finish_run(
                run_id,
                symbols_scanned=scanned,
                symbols_failed=failed,
                setups_found=total,
                error_count=failed,
                status="COMPLETED" if failed == 0 else "PARTIAL",
            )

            t_run_finished = time.monotonic()
            logger.info(
                "cycle #%d done: %d/%d symbols | %d setups | %d active signals | "
                "scan=%.2fs total=%.2fs",
                cycle, scanned, len(symbols), total, active_signals,
                t_run_finished - t_run_started, elapsed,
            )

            # Refresh expectancy filter every 10 cycles
            if expectancy_filter is not None and cycle % 10 == 0:
                expectancy_filter = load_expectancy(repository)
                logger.info("expectancy filter refreshed: %d records", len(expectancy_filter.records))

            # OOS evaluator: run every oos_evaluator_cycle_interval cycles
            if (settings.oos_evaluator_cycle_interval > 0
                    and cycle % settings.oos_evaluator_cycle_interval == 0):
                try:
                    _run_oos_evaluator(client, repository)
                except Exception:
                    logger.exception("OOS evaluator cycle failed")
        except Exception:
            logger.exception("cycle #%d failed", cycle)
            if run_id:
                try:
                    repository.finish_run(
                        run_id, status="FAILED", error_count=1,
                    )
                except Exception:
                    logger.warning("could not record failed run status")
                    repository.reconnect()

        # Sleep in small intervals to respond to shutdown
        delay = seconds_until_next_cycle(
            start, settings.scan_interval, time.monotonic(),
        )
        logger.info("cycle #%d sleeping %.0fs before next cycle", cycle, delay)
        for _ in range(int(delay)):
            if SHUTDOWN:
                break
            time.sleep(1)

    repository.close()
    logger.info("scanner stopped")


if __name__ == "__main__":
    main()
