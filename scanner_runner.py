"""Scanner Runner - continuous background scanner that writes to PostgreSQL.

Runs on system startup via Windows Startup folder.
Scans every 5 minutes, saves results to PostgreSQL, logs to file.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from app.config import Settings, load_settings
from app.db.repository import ScannerRepository
from app.exchange.bybit_client import BybitClient, BybitRateLimitError
from app.scanners.context_builder import build_market_context
# --- SCANNER_AC7 Phase A: structured cycle-completion diagnostics ----------
# Observability only: classify which symbols a cycle really processed, which
# it failed on and which it SKIPPED because Bybit/limiter denied the request
# (10006, BYBIT_API_SKIP, BREAKER_OPEN, deadline). No recovery, no extra API
# request, no change to symbols_scanned semantics or to the DB vocabulary.
from app.scanners.cycle_completion_v1 import (
    CYCLE_COMPLETION_EVENT,
    CYCLE_COMPLETION_SCANNER,
    RUN_STATUS_FAILED,
    CycleCompletionReport,
    STAGE_MARKET_DATA,
    STAGE_SCAN,
    classify_symbol_failure,
)
from app.scanners.direction_gate import ScannerDirectionGatePolicy
from app.scanners.expectancy_filter import ExpectancyFilter, load_expectancy
from app.scanners.funnel_diagnostics import flush_and_persist_all
from app.research.prospective_observer import ProspectiveOOSObserver
from app.scanners.models import SetupCandidate
from app.scanners.orchestrator import ScannerOrchestrator

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"
LOG_DIR = PROJECT_ROOT / "logs"
logger = logging.getLogger("scanner_runner")

# --- Issue #231: isolated Scanner-only configuration source --------------
#
# ``SCANNER_CONFIG_PATH`` is the ONLY switch introduced here.  It exists so a
# future Scanner-only canary can run the global Bybit rate limiter from its
# own JSON file while every other consumer (Paper, Research, HTF, Analytics)
# keeps reading the shared production ``config.yaml`` — which stays
# limiter-off.  When the variable is unset NOTHING changes: the historical
# ``<repo>/config.yaml`` is used, byte for byte, through the same
# ``load_settings()`` call as before.
#
# Contracts enforced here (fail-closed, all BEFORE any ``BybitClient`` is
# constructed and therefore before any Bybit request):
#   * only an ABSOLUTE path is accepted (no WorkingDirectory dependence);
#   * empty / relative / missing / directory / unreadable / non-JSON
#     overrides raise ``ScannerConfigError`` — there is NO implicit
#     fallback to the production config once an override was requested;
#   * ``.env`` handling, API keys, DB credentials and every other setting
#     are untouched: only the config-file source changes.
SCANNER_CONFIG_ENV = "SCANNER_CONFIG_PATH"
CONFIG_SOURCE_DEFAULT = "DEFAULT"
CONFIG_SOURCE_OVERRIDE = "OVERRIDE"


class ScannerConfigError(ValueError):
    """Fail-closed Scanner configuration error.

    Subclasses ``ValueError`` so every existing ``load_settings()`` failure
    contract (validation errors, malformed JSON, bad limiter parameters)
    keeps its historical exception type while gaining an explicit message
    that names the offending configuration source and path.
    """


def _is_absolute_config_path(value: str) -> bool:
    """True only for a path that is absolute **on the running OS**.

    ``Path`` is the native flavour (``WindowsPath`` on Windows, ``PosixPath``
    on Linux) and it is the very same class ``resolve_scanner_config_path()``
    builds the override from, so the predicate and the resulting path can
    never disagree — which is exactly what the previous pure-flavour check
    got wrong: ``PurePosixPath(...) or PureWindowsPath(...)`` accepted a
    Windows drive/UNC path on Linux while the native ``Path`` constructed
    from it was *relative*, i.e. silently anchored to the WorkingDirectory.

    Consequences, by design:

    * Linux accepts ``/srv/trad_bot/x.json`` and **rejects** ``C:\\srv\\x``,
      ``C:/srv/x`` and ``\\\\host\\share\\x`` — those are not POSIX-absolute;
    * Windows accepts ``C:\\srv\\x``, ``C:/srv/x`` and UNC paths and
      **rejects** drive-less ``/srv/x`` — it would resolve against the
      current drive, i.e. it is WorkingDirectory-dependent too;
    * each host keeps accepting its own legitimate absolute forms.

    ``pathlib`` never touches the filesystem or the WorkingDirectory here.
    """
    return Path(value).is_absolute()


def _foreign_absolute_hint(value: str) -> str:
    """Explain a rejection when the value is absolute only for the other OS.

    ``issubclass(Path, PurePosixPath)`` identifies the native flavour
    without hard-coding ``os.name``, so the hint stays correct for the real
    host and for the flavour-swapped regression tests.
    """
    native_is_posix = issubclass(Path, PurePosixPath)
    foreign = PureWindowsPath if native_is_posix else PurePosixPath
    if not foreign(value).is_absolute():
        return ""
    flavour = "Windows" if native_is_posix else "POSIX"
    return (
        f"; it is absolute for {flavour} but not for this OS, where native "
        f"Path({value!r}) is relative and would depend on the "
        "WorkingDirectory"
    )


def resolve_scanner_config_path(
    env: Mapping[str, str] | None = None,
) -> tuple[Path, str]:
    """Resolve ``(config_path, source)`` for this Scanner process.

    ``source`` is ``DEFAULT`` when ``SCANNER_CONFIG_PATH`` is unset (the
    historical ``<repo>/config.yaml``, cwd-independent) and ``OVERRIDE``
    when it points at an explicit absolute JSON file.

    Every malformed override raises :class:`ScannerConfigError` instead of
    silently degrading to the production config — an operator who asked for
    an isolated canary config must never get the shared one.
    """
    environ = os.environ if env is None else env
    raw = environ.get(SCANNER_CONFIG_ENV)
    if raw is None:
        return CONFIG_PATH, CONFIG_SOURCE_DEFAULT

    value = raw.strip()
    if not value:
        raise ScannerConfigError(
            f"{SCANNER_CONFIG_ENV} is set but empty/whitespace-only; unset it "
            "to use the default config, or point it at an absolute path to a "
            "JSON config file (no implicit fallback to config.yaml)"
        )
    if not _is_absolute_config_path(value):
        raise ScannerConfigError(
            f"{SCANNER_CONFIG_ENV} must be an absolute path for this OS, "
            f"got {value!r}; relative paths would depend on the process "
            f"WorkingDirectory{_foreign_absolute_hint(value)}"
        )

    path = Path(value)
    try:
        if not path.exists():
            raise ScannerConfigError(
                f"{SCANNER_CONFIG_ENV}={value!r} does not exist; refusing to "
                "fall back to the default config.yaml"
            )
        if path.is_dir():
            raise ScannerConfigError(
                f"{SCANNER_CONFIG_ENV}={value!r} is a directory, not a JSON "
                "config file"
            )
        if not os.access(path, os.R_OK):
            raise ScannerConfigError(
                f"{SCANNER_CONFIG_ENV}={value!r} is not readable by this user"
            )
    except ScannerConfigError:
        raise
    except OSError as exc:
        # Fail-closed: an unstatable path is a configuration error, never a
        # reason to continue with a different config file.
        raise ScannerConfigError(
            f"{SCANNER_CONFIG_ENV}={value!r} cannot be inspected "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    return path, CONFIG_SOURCE_OVERRIDE


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
    """Load Scanner settings from the resolved configuration source.

    Resolution and every validation step run BEFORE ``load_settings()``
    reads the file and therefore before ``main()`` can construct a
    ``BybitClient`` — an invalid ``SCANNER_CONFIG_PATH`` aborts startup
    with :class:`ScannerConfigError` instead of silently falling back to
    the shared production config.  ``.env`` loading and every other setting
    source are unchanged; only the config-file path may differ.
    """
    config_path, source = resolve_scanner_config_path()
    try:
        settings = load_settings(path=config_path, env_file=ENV_PATH)
    except ScannerConfigError:
        raise
    except Exception as exc:
        # Fail-closed: report WHICH source failed and why, without echoing
        # file contents (the message never includes secrets by contract).
        logger.critical(
            "scanner config load FAILED: source=%s config=%s error=%s: %s",
            source, config_path, type(exc).__name__, exc,
        )
        raise ScannerConfigError(
            f"scanner configuration load failed (source={source}, "
            f"config={config_path}): {type(exc).__name__}: {exc}"
        ) from exc
    logger.info(
        "scanner config: source=%s config=%s env_file=%s cwd=%s "
        "global_limiter=%s allow_env=%s state_path=%s "
        "universe_mode=%s top_n=%s symbols=%s",
        source,
        config_path,
        ENV_PATH,
        Path.cwd(),
        "ON" if settings.global_bybit_rate_limiter_enabled else "OFF",
        "ALLOWED" if settings.global_bybit_rate_limiter_allow_env else "DENIED",
        settings.global_limiter_state_path,
        settings.scanner_universe.mode,
        settings.scanner_universe.top_n,
        list(settings.symbols),
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


# --- Issue #253: BYBIT_SCANNER_STARTUP_BREAKER_OPEN_RECOVERY_V1 ----------
#
# The dynamic-universe refresh is the FIRST Bybit call of the process and
# runs BEFORE the database repository and the runner lock exist (the
# startup invariant of main()).  With the shared limiter enabled a
# BREAKER_OPEN deny raised there used to escape main() as an unhandled
# BybitRateLimitError, so systemd restarted Scanner in a loop (production
# 2026-11-03, release 54201c0 / PR #255:
# ``breaker_cooldown_exceeds_deadline`` on /v5/market/instruments-info).
#
# initialize_scanner_universe() waits and retries ONLY the denials a
# bounded wait can legitimately outlive.  Everything else — authentication,
# configuration, a shared-limiter state failure, an empty universe — is
# re-raised unchanged: never retried, never bypassed, never replaced with
# fabricated/stale symbols (FAIL_CLOSED preserved).

#: Poll granularity of the startup recovery wait: SIGTERM/SIGINT is
#: honoured at this latency, so the wait is never one unbounded sleep that
#: could outlive a shutdown request by minutes.
STARTUP_RETRY_POLL_S = 1.0

#: Hard floor of the wait between two startup attempts.  load_settings()
#: rejects a non-positive interval already, but Settings can be built
#: directly and the recovery loop must never degenerate into a busy loop.
STARTUP_RETRY_MIN_INTERVAL_S = 0.5

#: Secret-free message markers of a TEMPORARY limiter denial: the limiter
#: itself is healthy and simply not granting right now (breaker cooldown not
#: yet due, bucket not refilled inside the acquire deadline, epoch moved by
#: another process's rate-limit trip, or the client's own rate-limit budget
#: expiring on HTTP 429).  These are the exact wordings of BybitClient's
#: fail-closed deny branches (plus the process-local breaker below); the
#: regression suite drives the REAL client deny path, so a wording change
#: there breaks it loudly.
TEMPORARY_STARTUP_LIMITER_DENIAL_MARKERS: tuple[str, ...] = (
    "deferred by shared rate limiter (BREAKER_OPEN",
    "deferred by shared rate limiter (SKIPPED",
    "limiter grant revoked before send",
    "Rate limited on ",
    # Codex review P2: the PROCESS-LOCAL RateLimitBreaker cooldown opened by
    # an HTTP 429 / Bybit retCode 10006 (retry_budget.RateLimitBreaker.gate
    # -> raise_bybit_rate_limit_deferred).  The message carries a positive
    # remaining time — the cooldown expires by construction — and the bounded
    # wait of gate() has already failed to cover it, so startup may wait and
    # retry instead of terminating.
    "request deferred: rate-limit cooldown open (",
)


def is_temporary_startup_limiter_denial(exc: BaseException) -> bool:
    """True only when a bounded wait can legitimately outlive ``exc``.

    Temporary by construction: breaker cooldowns (5/30/120 s) expire, an
    ``SKIPPED`` acquire means only that the shared bucket did not refill
    inside the caller's deadline, a revoked grant means another process
    tripped the breaker (fresh epoch), the rate-limit budget refills, and
    the PROCESS-LOCAL ``RateLimitBreaker`` cooldown (HTTP 429 / Bybit
    retCode 10006) is a bounded ``bybit_rate_limit_cooldown_s`` window that
    closes by itself.

    Everything else is permanent for startup purposes and must fail fast:
    ``BybitAuthError`` (bad key / permission), a limiter that could not
    even open its shared state (``shared rate limiter unavailable`` —
    corrupt state, config mismatch, write/lock failure), a
    ``STATE_UNAVAILABLE`` deny, and any non-limiter error (empty universe,
    malformed payload, config validation).  Unknown future wordings
    default to NOT temporary — fail-closed: never retry what is not known
    to be temporary.
    """
    if not isinstance(exc, BybitRateLimitError):
        return False
    message = str(exc)
    return any(marker in message
               for marker in TEMPORARY_STARTUP_LIMITER_DENIAL_MARKERS)


def _startup_sleep(seconds: float) -> None:
    """The real wait used by the startup recovery loop.

    A single indirection point: tests can replace the wait without touching
    the process-wide ``time.sleep`` (and without ever waiting in CI).
    """
    time.sleep(seconds)


def wait_before_startup_retry(
    delay_s: float,
    *,
    sleep: Callable[[float], Any] | None = None,
    poll_s: float = STARTUP_RETRY_POLL_S,
) -> bool:
    """Wait ``delay_s`` seconds in short polls; ``False`` if SHUTDOWN hit.

    Returns True only when the full delay elapsed with no shutdown signal
    (the caller may retry); False means SIGTERM/SIGINT arrived while
    waiting and startup must stop cleanly.  Every sleep is positive and
    bounded by ``poll_s``, so the wait can neither busy-loop nor block a
    shutdown signal for longer than one poll.
    """
    if sleep is None:
        sleep = _startup_sleep
    remaining = float(delay_s)
    while remaining > 0:
        if SHUTDOWN:
            return False
        step = min(poll_s, remaining)
        sleep(step)
        remaining -= step
    return not SHUTDOWN


def initialize_scanner_universe(
    client: BybitClient,
    settings: Settings,
    *,
    sleep: Callable[[float], Any] | None = None,
) -> list[dict[str, Any]] | None:
    """Return the startup universe, waiting out temporary limiter denials.

    Contract (Issue #253, BYBIT_SCANNER_STARTUP_BREAKER_OPEN_RECOVERY_V1):

    * **temporary shared-limiter denial** → bounded, configurable backoff:
      ``startup_retry_interval_s`` doubling up to
      ``startup_retry_max_interval_s``, at most
      ``startup_retry_max_attempts`` attempts (0 = keep waiting until a
      shutdown signal), every wait polled in ``STARTUP_RETRY_POLL_S``
      chunks — no busy loop, no bypass of the limiter (a denied attempt
      sends no HTTP request at all);
    * **SIGTERM/SIGINT during a wait** → returns ``None`` so main() can
      exit BEFORE any database / runner-lock / trading / OOS side effect
      exists;
    * **permanent error** (authentication, configuration, shared-limiter
      state failure, empty universe) → re-raised unchanged after the very
      first attempt: never retried, never swallowed;
    * **success** → the universe exactly as discovered — never fabricated,
      stale or partial.
    """
    universe_settings = settings.scanner_universe
    interval_s = max(
        STARTUP_RETRY_MIN_INTERVAL_S,
        float(universe_settings.startup_retry_interval_s),
    )
    max_interval_s = max(
        interval_s, float(universe_settings.startup_retry_max_interval_s))
    max_attempts = max(0, int(universe_settings.startup_retry_max_attempts))
    if SHUTDOWN:
        return None

    attempt = 0
    delay_s = interval_s
    while True:
        attempt += 1
        try:
            return get_scanner_universe(client, settings)
        except BybitRateLimitError as exc:
            if not is_temporary_startup_limiter_denial(exc):
                raise                  # permanent: fail closed, never retried
            if 0 < max_attempts <= attempt:
                logger.error(
                    "scanner startup universe recovery gave up after %d "
                    "attempt(s): %s", attempt, exc)
                raise
            logger.warning(
                "scanner startup universe deferred by a Bybit rate limiter "
                "(shared or process-local, attempt %d%s): %s — retrying in "
                "%.1fs",
                attempt,
                (f"/{max_attempts}" if max_attempts else ""),
                exc, delay_s)
            if not wait_before_startup_retry(delay_s, sleep=sleep):
                logger.info(
                    "scanner startup stopped by shutdown signal during "
                    "universe recovery (attempt %d)", attempt)
                return None
            delay_s = min(delay_s * 2.0, max_interval_s)


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
_research_conn = None


def _get_research_observer(repository: ScannerRepository):
    """Lazy-init the generic research observer (singleton per process).

    Uses a DEDICATED pg8000 connection, independent from the production
    repository connection.  This ensures that research INSERT/commit/rollback
    can never interfere with production transactions.
    """
    global _research_observer, _research_conn
    if _research_observer is None:
        try:
            import pg8000
            from app.research.observer import ResearchObserver
            from app.research.repository import ResearchRepository
            from app.research.adapters.momentum_exhaustion_r import EXPERIMENT_CONFIG as MER_CONFIG
            from app.research.adapters.trend_pullback_v2 import EXPERIMENT_CONFIG as TPV2_CONFIG
            from app.research.adapters.breakout_retest import EXPERIMENT_CONFIG as BR_CONFIG
            from app.research.adapters.volatility_compression import EXPERIMENT_CONFIG as VC_CONFIG
            from app.research.adapters.momentum_exhaustion import EXPERIMENT_CONFIG as ME_CONFIG
            from app.research.adapters.support_resistance import EXPERIMENT_CONFIG as SRR_CONFIG
            from app.research.adapters.liquidity_reversal import EXPERIMENT_CONFIG as LR_CONFIG
            from app.research.adapters.liquidity_sweep_choch import EXPERIMENT_CONFIG as LSCO_CONFIG
            from app.research.adapters.trend_pullback_v3 import EXPERIMENT_CONFIG as TPV3_CONFIG
            from app.research.adapters.momentum_exhaustion_reverse_long_v2 import EXPERIMENT_CONFIG as ME_RL_V2_CONFIG
            from app.research.adapters.fvg_reaction_long import EXPERIMENT_CONFIG as FVG_CONFIG

            # Dedicated connection — never shares with production
            _research_conn = pg8000.connect(
                host=repository._host,
                port=repository._port,
                database=repository._database,
                user=repository._user,
                password=repository._password,
            )
            research_repo = ResearchRepository(_research_conn)
            experiments = {
                MER_CONFIG["scanner_name"]: MER_CONFIG,
                TPV2_CONFIG["scanner_name"]: TPV2_CONFIG,
                BR_CONFIG["scanner_name"]: BR_CONFIG,
                VC_CONFIG["scanner_name"]: VC_CONFIG,
                ME_CONFIG["scanner_name"]: ME_CONFIG,
                SRR_CONFIG["scanner_name"]: SRR_CONFIG,
                LR_CONFIG["scanner_name"]: LR_CONFIG,
                LSCO_CONFIG["scanner_name"]: LSCO_CONFIG,
                TPV3_CONFIG["scanner_name"]: TPV3_CONFIG,
                ME_RL_V2_CONFIG["scanner_name"]: ME_RL_V2_CONFIG,
                FVG_CONFIG["scanner_name"]: FVG_CONFIG,
            }
            _research_observer = ResearchObserver(research_repo, experiments)
            logger.info(
                "research observer initialized: dedicated connection, experiments=%s",
                list(experiments.keys()),
            )
        except Exception:
            _research_observer = None
            _research_conn = None
            logger.debug("research observer init failed — research capture disabled", exc_info=True)
    return _research_observer


def _observe_prospective_with_fill(client, prospective_obs, **kwargs):
    """Preserve existing OOS routing; independently collect Phase-1 depth.

    Issue #202 Phase 1 (ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1) is
    observe-only and fail-closed: it never routes orders and never alters the
    prospective observation lifecycle. The capture reuses the prospective
    observer's dedicated connection only AFTER ``observe()`` has committed its
    own transaction, so capture commit/rollback cannot affect prospective OOS
    state. Capture failures (including a missing connection) are always
    swallowed here and must never block the scanner cycle.
    """
    ids = prospective_obs.observe(**kwargs)
    if (
        os.environ.get("ME_SHORT_A_FILL_CAPTURE_ENABLED") == "1"
        and kwargs.get("scanner_name") == "MOMENTUM_EXHAUSTION"
        and kwargs.get("direction") == "SHORT"
    ):
        try:
            from app.research.me_short_a_fill_feasibility_v1 import capture
            capture(
                getattr(prospective_obs, "_conn", None), client,
                scanner_name=kwargs["scanner_name"],
                direction=kwargs["direction"],
                symbol=kwargs["symbol"],
                signal_time=kwargs["signal_time"],
                reference_price=kwargs["reference_price"],
                invalidation_price=kwargs["invalidation_price"],
                target_1=kwargs["target_1"],
                observation_ids=ids,
            )
        except Exception:
            logger.exception("ME fill feasibility capture error; scanner unaffected")
    return ids


# --- ME SHORT A prospective source intake (Issue #208 corrective) -------
#
# Research-only, forward-only capture of the ORIGINAL MOMENTUM_EXHAUSTION
# SHORT events, before/independent of the trading filters (direction gate,
# regime filter, expectancy filter). The forward outcome runtime reads these
# rows as an additional candidate source, so a trading filter can never
# silently truncate the ME SHORT A source -> Phase 1 -> lineage -> outcome
# chain again (confirmed incident: TSLAUSDT 2026-10-27 10:43:57.672455+00,
# SHORT in TREND_UP, captured by Phase 1 but absent from dds.scanner_setup).
#
# Fail-open by contract: every error is swallowed and must NEVER affect
# scanner execution; failures are logged with the full event identity and
# sunk to dds.scanner_error as ME_SHORT_A_SOURCE_INTAKE_ERROR so a research
# capture error can never become a silent signal loss.

_me_short_a_source_intake_conn = None
_me_short_a_source_intake_conn_retry_at = 0.0


def _get_me_short_a_source_intake_conn(repository: ScannerRepository):
    """Dedicated connection for the research-only source intake writer.

    Independent from the scanner's main transaction and from the prospective
    observer: research capture commits its own transactions only, and must
    not depend on any trading-side routing decision.
    """
    global _me_short_a_source_intake_conn, _me_short_a_source_intake_conn_retry_at
    if _me_short_a_source_intake_conn is not None:
        return _me_short_a_source_intake_conn
    now = time.monotonic()
    if now - _me_short_a_source_intake_conn_retry_at < 60.0:
        return None
    try:
        import pg8000

        connect_kwargs = {
            "database": repository._database,
            "user": repository._user,
            "password": repository._password,
        }
        if repository._unix_sock:
            connect_kwargs["unix_sock"] = repository._unix_sock
        else:
            connect_kwargs["host"] = repository._host
            connect_kwargs["port"] = repository._port
        _me_short_a_source_intake_conn = pg8000.connect(**connect_kwargs)
        return _me_short_a_source_intake_conn
    except Exception:
        _me_short_a_source_intake_conn = None
        _me_short_a_source_intake_conn_retry_at = now
        logger.debug(
            "ME SHORT A source intake connection unavailable", exc_info=True)
        return None


def _capture_me_short_a_source(
    repository: ScannerRepository,
    candidate: SetupCandidate,
    *,
    capture_route: str,
    blocked_stage: str | None = None,
    blocked_reason: str | None = None,
) -> None:
    """Append ONE original ME SHORT event to the research source intake.

    Only ``MOMENTUM_EXHAUSTION`` / ``SHORT`` candidates are captured. The
    capture is strictly research-only: nothing is ever routed back into the
    trading flow and no scanner decision is changed. Fail-open: the whole
    call is wrapped and must never raise into the scanner cycle.
    """
    if (
        candidate.scanner_name != "MOMENTUM_EXHAUSTION"
        or candidate.direction != "SHORT"
    ):
        return

    def _report_error() -> None:
        """Separate source-capture error diagnostics; never silent."""
        try:
            repository.save_error(
                symbol=candidate.symbol,
                scanner_name=candidate.scanner_name,
                error_type="ME_SHORT_A_SOURCE_INTAKE_ERROR",
                error_message=(
                    f"route={capture_route} signal_time={candidate.detected_at} "
                    f"setup_id={candidate.setup_id}"),
            )
        except Exception:
            logger.debug(
                "ME SHORT A source intake error sink failed", exc_info=True)

    try:
        from app.research.me_short_a_source_intake_v1 import capture_with_status

        conn = _get_me_short_a_source_intake_conn(repository)
        if conn is None:
            raise RuntimeError("source intake connection unavailable")
        features = candidate.features or {}
        status = capture_with_status(
            conn,
            scanner_name=candidate.scanner_name,
            direction=candidate.direction,
            symbol=candidate.symbol,
            signal_time=candidate.detected_at,
            reference_price=candidate.reference_price,
            invalidation_price=candidate.invalidation_price,
            target_1=candidate.target_1,
            target_2=candidate.target_2,
            trigger_5m_close=features.get("trigger_5m_close"),
            entry_zone_low=candidate.entry_zone_low,
            entry_zone_high=candidate.entry_zone_high,
            signal_candle_open_time=candidate.signal_candle_open_time or None,
            market_regime=candidate.market_regime,
            setup_id=str(candidate.setup_id),
            capture_route=capture_route,
            blocked_stage=blocked_stage,
            blocked_reason=blocked_reason,
        )
        # Persistence errors are sunk to dds.scanner_error and are never
        # silent. Non-errors (duplicate replay / not active / pre-freeze) are
        # expected states and stay in the intake module's debug log.
        if status == "ERROR":
            logger.error(
                "ME SHORT A source intake persistence error; scanner unaffected "
                "symbol=%s route=%s signal_time=%s",
                candidate.symbol, capture_route, candidate.detected_at)
            _report_error()
        elif status in ("DISABLED", "NOT_ACTIVE", "PRE_FREEZE"):
            logger.info(
                "ME SHORT A source intake status=%s symbol=%s route=%s",
                status, candidate.symbol, capture_route)
    except Exception:
        logger.exception(
            "ME SHORT A source intake capture failed; scanner unaffected "
            "symbol=%s route=%s signal_time=%s",
            candidate.symbol, capture_route, candidate.detected_at)
        _report_error()


# --- Prospective OOS Observer -------------------------------------------

_prospective_observer = None
_prospective_conn = None


def _get_prospective_observer(repository: ScannerRepository):
    """Lazy-init the prospective OOS observer (singleton per process).

    Uses a DEDICATED pg8000 connection, independent from production
    and from the generic research observer connection.
    """
    global _prospective_observer, _prospective_conn
    if _prospective_observer is None:
        try:
            import pg8000
            from app.research.prospective_observer import ProspectiveOOSObserver
            from app.research.prospective_completed_event_repository import (
                ProspectiveCompletedEventRepository,
            )
            import json
            from pathlib import Path

            # Load frozen registry
            registry_path = Path(__file__).parent / "app" / "research" / "prospective_registry.json"
            if not registry_path.exists():
                logger.debug("prospective registry not found at %s", registry_path)
                return None
            with open(registry_path) as f:
                registry_data = json.load(f)
            experiments = {e["experiment_id"]: e for e in registry_data["experiments"]}

            # Only start observing if prospective_started_at is set
            # (activation is done separately via migration/update)
            if not experiments:
                return None

            # Dedicated connection — never shares with production or generic research
            _prospective_conn = pg8000.connect(
                host=repository._host,
                port=repository._port,
                database=repository._database,
                user=repository._user,
                password=repository._password,
            )
            # Also init a research repository for immediate promotion to research_signal
            research_repo_for_promote = None
            try:
                from app.research.repository import ResearchRepository as _PromoteRepo
                import pg8000 as _pg8000
                _promote_conn = _pg8000.connect(
                    host=repository._host,
                    port=repository._port,
                    database=repository._database,
                    user=repository._user,
                    password=repository._password,
                )
                research_repo_for_promote = _PromoteRepo(_promote_conn)
            except Exception:
                logger.debug("prospective promote repo init failed — observations will not be promoted", exc_info=True)

            completed_event_repo = ProspectiveCompletedEventRepository(_prospective_conn)

            _prospective_observer = ProspectiveOOSObserver(
                _prospective_conn, experiments,
                research_repo=research_repo_for_promote,
                completed_event_repo=completed_event_repo,
            )
            logger.info(
                "prospective observer initialized: experiments=%s promote=%s completed_freeze=ON",
                list(experiments.keys()),
                "ON" if research_repo_for_promote else "OFF",
            )
        except Exception:
            _prospective_observer = None
            _prospective_conn = None
            logger.debug("prospective observer init failed — prospective capture disabled", exc_info=True)
    return _prospective_observer


# --- ME Early MAE V2 independent research capture -----------------------

_me_early_mae_v2_pipeline = None
_me_early_mae_v2_conn = None


def _get_me_early_mae_v2_pipeline(repository: ScannerRepository):
    """Opt-in research capture; never uses the scanner DB transaction."""
    import os

    if os.environ.get("ME_EARLY_MAE_V2_CAPTURE_ENABLED") != "1":
        return None

    # A capture flag alone must never activate an unfrozen experiment.
    if os.environ.get("ME_EARLY_MAE_V2_FREEZE_APPROVED") != "1":
        logger.warning(
            "ME Early MAE V2 capture refused: protocol freeze not approved"
        )
        return None

    global _me_early_mae_v2_pipeline, _me_early_mae_v2_conn

    if _me_early_mae_v2_pipeline is not None:
        return _me_early_mae_v2_pipeline

    conn = None
    try:
        import pg8000
        from app.research.me_r_long_early_mae_v2.pipeline import (
            MeEarlyMaeV2Pipeline,
        )

        conn = pg8000.connect(
            host=repository._host,
            port=repository._port,
            database=repository._database,
            user=repository._user,
            password=repository._password,
        )
        pipeline = MeEarlyMaeV2Pipeline(conn)
        pipeline.initialize()

        _me_early_mae_v2_conn = conn
        _me_early_mae_v2_pipeline = pipeline

        logger.info("ME Early MAE V2 research capture initialized")
        return pipeline

    except Exception:
        logger.exception("ME Early MAE V2 capture initialization failed")
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.debug("ME Early MAE V2 connection close failed", exc_info=True)
        return None


# --- ME_R_LONG_CLOSE_LOCATION_OOS Experiment ------------------------------

_me_r_long_cl_oos_observer = None


def _get_me_r_long_cl_oos_observer(repository: ScannerRepository):
    """Lazy-init the ME_R_LONG_CLOSE_LOCATION_OOS observer (singleton per process)."""
    global _me_r_long_cl_oos_observer
    if _me_r_long_cl_oos_observer is None:
        from app.shadow.me_r_long_close_location_oos_repository import MERLongCLoOosRepository
        _me_r_long_cl_oos_observer = MERLongCLoOosRepository(repository._conn)
    return _me_r_long_cl_oos_observer


def _observe_me_r_long_cl_oos(repository: ScannerRepository, candidate: SetupCandidate) -> str:
    """Record an ME_R_LONG_CLOSE_LOCATION_OOS candidate for OOS analysis.

    Called from the scanner runner for every candidate from the OOS scanner.
    Both PASS and REJECT candidates are saved for OOS analysis.
    This is a fire-and-forget observation — never blocks the signal.
    """
    from app.shadow.me_r_long_close_location_oos_repository import MERLongCLoOosRepository
    from app.shadow.me_r_long_close_location_oos_runner import (
        MERLongCLoOosAck,
        observe_oos_candidate,
    )

    repo = MERLongCLoOosRepository(repository._conn)
    ack = observe_oos_candidate(repo, candidate)

    if ack in (
        MERLongCLoOosAck.ERROR,
        MERLongCLoOosAck.INVALID_SOURCE,
    ):
        logger.error(
            "ME clean OOS capture not acknowledged: symbol=%s "
            "scanner_version=%s ack=%s",
            candidate.symbol,
            candidate.scanner_version,
            ack,
        )
    elif ack == MERLongCLoOosAck.SKIPPED_VERSION:
        logger.warning(
            "ME clean OOS capture skipped version: symbol=%s "
            "scanner_version=%s",
            candidate.symbol,
            candidate.scanner_version,
        )
    return ack


def _observe_htf_keylevel_point_b(repository: ScannerRepository, prospective_obs: ProspectiveOOSObserver | None, ctx, symbols: list[str]) -> None:
    """Evaluate the isolated HTF key-level detector without changing scanner output.

    The detector is research-only.  It does not create READY_TO_TRADE setups,
    does not pass through paper gates, and emits observations only when a
    completed Point-B or matched BASELINE cohort has objective metadata.
    """
    if prospective_obs is None:
        return
    try:
        from app.research.htf_keylevel_point_b import (
            BASELINE_EXPERIMENT_ID, EXPERIMENT_ID, detect_point_b_setups,
        )
        for symbol in symbols:
            for direction in ("LONG", "SHORT"):
                try:
                    results = detect_point_b_setups(
                        symbol=symbol,
                        direction=direction,
                        execution_candles=list(ctx.candles_5m),
                        htf_candles=list(ctx.candles_1h),
                        current_time=ctx.evaluated_at,
                        market_regime=ctx.market_regime,
                    )
                except Exception:
                    logger.debug("HTF key-level detector failed for %s %s", symbol, direction, exc_info=True)
                    continue

                candidates = []
                for result in results:
                    if result.signal is not None:
                        candidates.append((EXPERIMENT_ID, result.signal, result))
                    if result.baseline is not None:
                        candidates.append((BASELINE_EXPERIMENT_ID, result.baseline, None))

                for experiment_id, candidate, detection_result in candidates:
                    try:
                        prospective_obs.observe(
                            scanner_name=experiment_id,
                            direction=candidate.direction,
                            symbol=candidate.symbol,
                            signal_time=candidate.detected_at,
                            reference_price=candidate.reference_price,
                            invalidation_price=candidate.invalidation_price,
                            target_1=candidate.target_1,
                            target_2=candidate.target_2,
                            score=candidate.score,
                            features=dict(candidate.features),
                            parameters={},
                            market_regime=candidate.market_regime,
                            detection_result=detection_result,
                        )
                    except Exception:
                        logger.debug(
                            "HTF key-level prospective observation failed for %s %s",
                            experiment_id,
                            symbol,
                            exc_info=True,
                        )
    except Exception:
        logger.debug("HTF key-level prospective capture failed", exc_info=True)


def _run_oos_evaluator(client: BybitClient, repository: ScannerRepository) -> None:
    """Run OOS evaluator for ME_R_LONG_CLOSE_LOCATION_OOS experiment.

    Evaluates pending signals and updates outcomes for mature horizons.
    Called periodically from the scanner main loop.
    """
    from app.shadow.me_r_long_close_location_oos_evaluator import MERLongCLoOosEvaluator
    from app.shadow.me_r_long_close_location_oos_repository import MERLongCLoOosRepository

    repo = MERLongCLoOosRepository(repository._conn)
    evaluator = MERLongCLoOosEvaluator(repo, client)

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
    prospective_obs: ProspectiveOOSObserver | None = None,
    me_early_mae_v2_pipeline: Any = None,
    me_capture_spool: Any | None = None,
    *,
    report: CycleCompletionReport | None = None,
) -> tuple[int, int, int]:
    """Returns (total_found, scanned, failed).

    Contract (SCANNER_AC7 Phase A, unchanged): the 3-tuple, the meaning of
    ``scanned`` (symbols whose MarketContext was built) and every
    ``save_run_stat`` / ``save_setup`` / ``save_error`` call stay exactly as
    before. ``report`` is an OPTIONAL out-parameter: when given, it is filled
    with the structured completion facts (expected / processed / failed /
    skipped symbols and their reasons) for this cycle.
    """
    settings = settings or _load_runner_settings()
    # SCANNER_AC7 Phase A: one completion report per cycle. A caller may pass
    # its own instance (main() logs and persists it afterwards).
    report = report if report is not None else CycleCompletionReport()
    report.expected_symbols = len(symbols)
    total_found = 0
    scanned = 0
    failed = 0
    expectancy_rejected = 0
    run_stats = {
        name: {"symbols_scanned": 0, "candidates_found": 0,
               "setups_saved": 0, "errors_count": 0, "duration_ms": 0.0}
        for name in orchestrator.scanners
    }

    # ME clean OOS research delivery is independent of new scanner signals.
    # A previously captured observation survives a scanner restart.
    if me_capture_spool is not None:
        from app.shadow.me_r_close_location_capture_replay import (
            replay_me_clean_capture,
        )
        from app.shadow.me_r_long_close_location_oos_repository import (
            MERLongCLoOosRepository,
        )

        # Independent connection: ME research must never commit/rollback
        # the scanner's main PostgreSQL transaction.
        replay_conn = None
        try:
            import pg8000

            connect_kwargs = {
                "database": repository._database,
                "user": repository._user,
                "password": repository._password,
            }

            if repository._unix_sock:
                connect_kwargs["unix_sock"] = repository._unix_sock
            else:
                connect_kwargs["host"] = repository._host
                connect_kwargs["port"] = repository._port

            replay_conn = pg8000.connect(**connect_kwargs)
            replay_repo = MERLongCLoOosRepository(replay_conn)

            replay_stats = replay_me_clean_capture(
                me_capture_spool,
                replay_repo,
                limit=100,
            )
            logger.info(
                "ME clean capture replay: %s",
                replay_stats,
            )
        except Exception:
            logger.exception(
                "ME clean capture replay deferred: dedicated "
                "PostgreSQL connection unavailable"
            )
        finally:
            if replay_conn is not None:
                try:
                    replay_conn.close()
                except Exception:
                    logger.exception(
                        "ME clean capture replay connection close failed"
                    )

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
                report.record_processed(symbol)
            except Exception as exc:
                logger.exception("market data failed for %s", symbol)
                failed += 1
                # SCANNER_AC7 Phase A: a rate-limit SKIP (Bybit 10006,
                # BYBIT_API_SKIP, BREAKER_OPEN, deadline) means the symbol
                # received NO data at all and is recorded separately from an
                # ordinary market-data failure. Behaviour of ``failed`` and of
                # the 3-tuple return is unchanged; ``MARKET_DATA_ERROR`` below
                # keeps its historical value for every non-limiter failure.
                outcome = classify_symbol_failure(
                    symbol, exc, stage=STAGE_MARKET_DATA)
                report.record_outcome(outcome)
                if outcome.rate_limit:
                    repository.save_error(
                        symbol=symbol, scanner_name="ALL",
                        run_id=run_id,
                        error_type="RATE_LIMIT_SKIP",
                        error_message=str(exc),
                    )
                else:
                    repository.save_error(
                        symbol=symbol, scanner_name="ALL",
                        run_id=run_id,
                        error_type="MARKET_DATA_ERROR",
                        error_message=str(exc),
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
                    # Shadow/control candidates must be persisted as DETECTED,
                    # never READY_TO_TRADE.  Scanners set state=SETUP_READY by
                    # default which save_setup maps to READY_TO_TRADE; override
                    # that for shadow candidates so paper_runner never sees them.
                    if (
                        c.features.get("_shadow_control")
                        or c.scanner_name == "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"
                    ):
                        from app.scanners.models import SetupState as _ShadowState
                        from dataclasses import replace as _shadow_replace
                        c = _shadow_replace(c, state=_ShadowState.DETECTED)
                    if (
                        c.scanner_name == "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"
                        and me_capture_spool is not None
                    ):
                        try:
                            me_capture_spool.enqueue(c)
                        except Exception:
                            logger.critical(
                                "ME CLEAN CAPTURE LOSS RISK: enqueue failed "
                                "symbol=%s version=%s",
                                c.symbol, c.scanner_version,
                                exc_info=True,
                            )
                            failed += 1

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
                        if me_capture_spool is None:
                            try:
                                _observe_me_r_long_cl_oos(repository, c)
                            except Exception:
                                logger.exception("ME direct OOS capture failed")

                    # Issue #208 corrective: research-only ME SHORT A source
                    # intake, captured independently of the trading filters.
                    # Fail-open: never blocks the scanner cycle.
                    _capture_me_short_a_source(
                        repository, c,
                        capture_route=(
                            "SHADOW_CONTROL"
                            if c.features.get("_shadow_control")
                            else "TRADEABLE_FLOW"),
                    )

                    # Prospective OOS: capture for frozen experiments
                    # Fail-open: never blocks scanner cycle.
                    if prospective_obs is not None:
                        try:
                            _observe_prospective_with_fill(client, prospective_obs,
                                scanner_name=c.scanner_name,
                                direction=c.direction,
                                symbol=c.symbol,
                                signal_time=c.detected_at,
                                reference_price=c.reference_price,
                                invalidation_price=c.invalidation_price,
                                target_1=c.target_1,
                                target_2=c.target_2,
                                score=c.score,
                                features=dict(c.features) if c.features else {},
                                parameters={},
                                market_regime=c.market_regime,
                            )
                        except Exception:
                            logger.debug("prospective observation failed for %s", c.scanner_name, exc_info=True)

                # OBSERVE_ONLY candidates: save to DB with non-tradeable status
                # and capture for prospective OOS.  These candidates passed
                # risk_geometry + score_gate but were blocked by OBSERVE_ONLY
                # direction gate status.  They are research observations, not
                # tradeable signals.
                observe_candidates = symbol_stats.get("_observe_candidates", [])
                for oc in observe_candidates:
                    # Save with DETECTED status (not READY_TO_TRADE)
                    # so paper_runner never picks them up.
                    from app.scanners.models import SetupState as _ObsState
                    from dataclasses import replace as _obs_replace
                    obs_c = _obs_replace(oc, state=_ObsState.DETECTED)
                    # Issue #208 corrective: research-only ME SHORT A source
                    # intake. A direction-gate block must never prevent
                    # research capture. Fail-open.
                    _capture_me_short_a_source(
                        repository, obs_c,
                        capture_route=(
                            "DIRECTION_GATE_BLOCKED"
                            if oc.features.get("_blocked_for_prospective")
                            else "OBSERVE_ONLY"),
                        blocked_stage="direction_gate",
                        blocked_reason=(
                            oc.features.get("_blocked_reason")
                            or oc.features.get("_observe_only_reason")),
                    )
                    if (
                        obs_c.scanner_name == "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"
                        and me_capture_spool is not None
                    ):
                        try:
                            me_capture_spool.enqueue(obs_c)
                        except Exception:
                            logger.critical(
                                "ME CLEAN CAPTURE LOSS RISK: enqueue failed "
                                "symbol=%s version=%s",
                                obs_c.symbol, obs_c.scanner_version,
                                exc_info=True,
                            )
                            failed += 1

                    try:
                        repository.save_setup(obs_c, run_id=run_id)
                    except Exception:
                        logger.debug("observe-only setup save failed for %s", oc.scanner_name, exc_info=True)
                    # Dedicated ME PASS/REJECT research capture.
                    if oc.scanner_name == "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1":
                        if me_capture_spool is None:
                            try:
                                _observe_me_r_long_cl_oos(repository, obs_c)
                            except Exception:
                                logger.exception(
                                    "ME close-location dedicated OOS capture failed"
                                )
                    # Capture for prospective OOS
                    if prospective_obs is not None:
                        try:
                            _observe_prospective_with_fill(client, prospective_obs,
                                scanner_name=oc.scanner_name,
                                direction=oc.direction,
                                symbol=oc.symbol,
                                signal_time=oc.detected_at,
                                reference_price=oc.reference_price,
                                invalidation_price=oc.invalidation_price,
                                target_1=oc.target_1,
                                target_2=oc.target_2,
                                score=oc.score,
                                features=dict(oc.features) if oc.features else {},
                                parameters={},
                                market_regime=oc.market_regime,
                            )
                        except Exception:
                            logger.debug("observe-only prospective observation failed for %s", oc.scanner_name, exc_info=True)

                # Independent V3 closed-candle detector, research only.
                # Never changes production candidates or paper routing.
                # Registry INACTIVE must not invoke the detector.
                from app.research.me_reverse_long_v3.protocol import (
                    EXPERIMENT_ID as _ME_V3_ID,
                )
                _me_v3_spec = (
                    prospective_obs._registry.get(_ME_V3_ID)
                    if prospective_obs is not None else None
                )
                if (
                    _me_v3_spec is not None
                    and _me_v3_spec.get("status") == "RUNNING"
                    and _me_v3_spec.get("freeze_ts")
                ):
                    try:
                        from app.research.me_reverse_long_v3.detector import (
                            detect_v3_closed,
                        )
                        detection = detect_v3_closed(ctx)
                        if detection.status == "SIGNAL":
                            c = detection.candidate
                            clean_ctx = detection.clean_context
                            # Fail closed if processing crossed the planned entry.
                            from datetime import datetime, timezone
                            from app.research.me_reverse_long_v3.entry_policy import (
                                next_executable_entry_at,
                            )
                            capture_now = datetime.now(timezone.utc)
                            capture_timely = (
                                capture_now < next_executable_entry_at(c.detected_at)
                            )
                            if not capture_timely:
                                logger.info(
                                    "ME V3 late capture rejected: symbol=%s",
                                    symbol,
                                )
                            from app.research.me_reverse_long_v3.temporal import (
                                verify_trigger_candle,
                                verify_source_timeframes,
                            )
                            temporal = verify_trigger_candle(
                                clean_ctx.candles_5m, c.detected_at,
                            )
                            source_ok, source_reason = verify_source_timeframes(
                                clean_ctx, c.detected_at,
                            )
                            if capture_timely:
                                _observe_prospective_with_fill(client, prospective_obs,
                                    scanner_name=c.scanner_name,
                                    direction=c.direction,
                                    symbol=c.symbol,
                                    signal_time=c.detected_at,
                                    reference_price=c.reference_price,
                                    invalidation_price=c.invalidation_price,
                                    target_1=c.target_1,
                                    target_2=c.target_2,
                                    score=c.score,
                                    features=dict(c.features or {}),
                                    parameters={
                                        "_v3_shadow_capture": True,
                                        "_v3_scanner_version": c.scanner_version,
                                        "_v3_trigger_open_at": (
                                            temporal.trigger_open_at.isoformat()
                                            if temporal.trigger_open_at else None
                                        ),
                                        "_v3_entry_zone_low": c.entry_zone_low,
                                        "_v3_temporal_status": temporal.status,
                                        "_v3_temporal_reason": temporal.reason,
                                        "_v3_source_timeframes_validated": source_ok,
                                        "_v3_source_timeframes_reason": source_reason,
                                        "_v3_trigger_close_at": (
                                            temporal.trigger_close_at.isoformat()
                                            if temporal.trigger_close_at else None
                                        ),
                                        "_v3_execution_entry_at": (
                                            temporal.execution_entry_at.isoformat()
                                            if temporal.execution_entry_at else None
                                        ),
                                    },
                                    market_regime=c.market_regime,
                                )
                    except Exception:
                        logger.exception(
                            "ME V3 closed-candle research capture failed: symbol=%s",
                            symbol,
                        )

                # Independent ME Early MAE V2 prospective capture.
                # Candidates were selected before direction/expectancy gates.
                # No setup state or paper routing is modified.
                if me_early_mae_v2_pipeline is not None:
                    for me_candidate in symbol_stats.get(
                        "_me_early_mae_v2_candidates", []
                    ):
                        try:
                            me_early_mae_v2_pipeline.capture_candidate(
                                me_candidate
                            )
                        except Exception:
                            logger.exception(
                                "ME Early MAE V2 capture failed: symbol=%s setup_id=%s",
                                me_candidate.symbol,
                                me_candidate.setup_id,
                            )

                # SRR LONG Research: capture every SRR LONG candidate for
                # research outcome tracking (independent of paper trading).
                research_candidates = symbol_stats.get("_research_candidates", [])
                for rc in research_candidates:
                    try:
                        _observe_srr_long_research(repository, rc)
                    except Exception:
                        logger.debug("SRR research observation failed", exc_info=True)

                # Expectancy-rejected candidates for OOS research
                # Capture candidates that passed all gates but were rejected by expectancy filter
                expectancy_rejected_candidates = symbol_stats.get("_expectancy_rejected_candidates", [])
                for erc in expectancy_rejected_candidates:
                    # Issue #208 corrective: research-only ME SHORT A source
                    # intake, independent of the prospective observer and of
                    # every trading filter. Fail-open.
                    _capture_me_short_a_source(
                        repository, erc,
                        capture_route="EXPECTANCY_FILTER_REJECTED",
                        blocked_stage="expectancy_filter",
                        blocked_reason=erc.features.get("_rejection_reason"),
                    )
                    if prospective_obs is not None:
                        try:
                            # Add expectancy metadata to features
                            features = dict(erc.features) if erc.features else {}
                            features["_expectancy_rejection_oos"] = True
                            features["_rejection_stage"] = "expectancy_filter"
                            
                            # Determine rejection reason based on scanner
                            if erc.scanner_name == "BREAKOUT_RETEST":
                                features["_rejection_reason"] = "profit_factor_below_threshold"
                                features["_threshold"] = 1.20
                            elif erc.scanner_name == "FVG_REACTION_LONG_LOCAL_STRUCT_V1":
                                features["_rejection_reason"] = "negative_historical_performance"
                                features["_threshold"] = 0.0
                            elif erc.scanner_name == "TREND_PULLBACK_V3":
                                features["_rejection_reason"] = "regime_mismatch"
                                features["_actual_regime"] = erc.market_regime
                                features["_required_regime"] = "TREND_UP"
                            
                            _observe_prospective_with_fill(client, prospective_obs,
                                scanner_name=erc.scanner_name,
                                direction=erc.direction,
                                symbol=erc.symbol,
                                signal_time=erc.detected_at,
                                reference_price=erc.reference_price,
                                invalidation_price=erc.invalidation_price,
                                target_1=erc.target_1,
                                target_2=erc.target_2,
                                score=erc.score,
                                features=features,
                                parameters={},
                                market_regime=erc.market_regime,
                            )
                        except Exception:
                            logger.debug("expectancy rejected OOS observation failed for %s", erc.scanner_name, exc_info=True)

                # Regime-rejected candidates for OOS research
                # Capture candidates that passed all filters except regime filter
                regime_rejected_candidates = symbol_stats.get("_regime_rejected_candidates", [])
                for rrc in regime_rejected_candidates:
                    # Issue #208 corrective: THE confirmed incident class.
                    # The trading regime filter excludes this candidate from
                    # dds.scanner_setup, but the original ME SHORT event is
                    # still captured for research (REGIME_FILTER_REJECTED).
                    # The regime filter itself stays untouched: nothing is
                    # routed back into the trading flow. Fail-open.
                    _capture_me_short_a_source(
                        repository, rrc,
                        capture_route="REGIME_FILTER_REJECTED",
                        blocked_stage="regime_filter",
                        blocked_reason=(
                            f"{rrc.direction} in {rrc.market_regime}"),
                    )
                    if prospective_obs is not None:
                        try:
                            # Add regime metadata to features
                            features = dict(rrc.features) if rrc.features else {}
                            features["_regime_counterfactual_oos"] = True
                            features["_rejection_stage"] = "regime_filter"
                            features["_actual_regime"] = rrc.market_regime
                            features["_required_regime"] = "TREND_UP"
                            
                            _observe_prospective_with_fill(client, prospective_obs,
                                scanner_name=rrc.scanner_name,
                                direction=rrc.direction,
                                symbol=rrc.symbol,
                                signal_time=rrc.detected_at,
                                reference_price=rrc.reference_price,
                                invalidation_price=rrc.invalidation_price,
                                target_1=rrc.target_1,
                                target_2=rrc.target_2,
                                score=rrc.score,
                                features=features,
                                parameters={},
                                market_regime=rrc.market_regime,
                            )
                        except Exception:
                            logger.debug("regime rejected OOS observation failed for %s", rrc.scanner_name, exc_info=True)

                # ── Generic research: resolve statuses + promote to signals ──
                # Fail-open: never blocks scanner cycle.
                if _research_observer is not None:
                    try:
                        rejections = symbol_stats.get("_research_rejections", [])
                        setup_ready = symbol_stats.get("_research_setup_ready", [])
                        if rejections or setup_ready:
                            batch_stats = _research_observer._repo.resolve_and_promote_batch(
                                rejections, setup_ready,
                            )
                            if batch_stats["resolved"] or batch_stats["promoted"]:
                                logger.info(
                                    "research resolve: resolved=%d promoted=%d errors=%d",
                                    batch_stats["resolved"],
                                    batch_stats["promoted"],
                                    batch_stats["errors"],
                                )
                    except Exception:
                        logger.debug("research resolve/promote failed", exc_info=True)

                if prospective_obs is not None:
                    try:
                        _observe_htf_keylevel_point_b(repository, prospective_obs, ctx, [symbol])
                    except Exception:
                        logger.debug("HTF key-level observation failed for %s", symbol, exc_info=True)

                # ── Prospective OOS: log observation stats ──
                # Promotion happens immediately inside prospective observation
                # (_observe_prospective_with_fill preserves observe() semantics).
                # Fail-open: never blocks scanner cycle.
                if prospective_obs is not None:
                    try:
                        stats = prospective_obs.stats
                        if stats:
                            logger.info(
                                "prospective OOS: %s",
                                ", ".join(f"{k}={v}" for k, v in stats.items()),
                            )
                    except Exception:
                        logger.debug("prospective stats logging failed", exc_info=True)

                total_found += len(candidates)
                if candidates:
                    best = max(candidates, key=lambda c: c.score)
                    logger.info(
                        "%s: %d setups (best: %s %s score=%.0f)",
                        symbol, len(candidates), best.direction,
                        best.scanner_name, best.score,
                    )
            except Exception as exc:
                logger.exception("scan failed for %s", symbol)
                failed += 1
                # SCANNER_AC7 Phase A: the market context WAS built (the
                # symbol stays in ``processed``/``symbols_scanned``), only
                # the analysis failed — the historical SCAN_ERROR row is
                # unchanged, the report additionally explains the cause.
                report.record_outcome(
                    classify_symbol_failure(symbol, exc, stage=STAGE_SCAN))
                repository.save_error(
                    symbol=symbol, scanner_name="SCANNER",
                    run_id=run_id,
                    error_type="SCAN_ERROR",
                    error_message=str(exc),
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

    # SCANNER_AC7 Phase A: persist one diagnostic row for every INCOMPLETE
    # cycle (complete cycles keep their historical single SETUP_DETECTED
    # stream — no new write, no new API call). dds.scanner_event.event_type
    # has no CHECK constraint, so this needs NO migration. The flag stops
    # main()'s abort path from writing a second row for the same cycle.
    if not report.is_complete and not report.event_persisted:
        try:
            repository.save_event(
                CYCLE_COMPLETION_EVENT, CYCLE_COMPLETION_SCANNER, "",
                run_id=run_id,
                payload=report.to_payload(),
            )
            report.event_persisted = True
        except Exception:
            # Diagnostics are observational: never let a reporting failure
            # break the cycle it reports on.
            logger.warning("CYCLE_COMPLETION event persist failed", exc_info=True)

    return total_found, scanned, failed


def report_aborted_cycle(
    repository: ScannerRepository,
    report: CycleCompletionReport,
    cycle: int,
    run_id: int | None,
) -> None:
    """Diagnostics for a cycle that died inside ``main()`` (review P2).

    Contract: this helper NEVER raises, NEVER touches
    ``dds.scanner_run.status`` (the caller has already written ``FAILED``)
    and can never mask the original cycle exception — every statement is
    guarded individually. It only logs what the cycle had processed so far
    and, when the cycle had started, persists one ``CYCLE_COMPLETION`` row
    marked ``aborted`` with ``run_status=FAILED``.
    """
    if not (report.expected_symbols or report.processed or report.outcomes):
        return
    try:
        logger.warning(
            "cycle #%d aborted: %s", cycle,
            report.log_summary(run_status=RUN_STATUS_FAILED),
        )
    except Exception:
        logger.debug("aborted cycle summary failed", exc_info=True)
    if report.event_persisted:
        # run_scan_cycle already wrote this cycle's row — no duplicates.
        return
    try:
        repository.save_event(
            CYCLE_COMPLETION_EVENT, CYCLE_COMPLETION_SCANNER, "",
            run_id=run_id,
            payload=report.to_payload(
                aborted=True, run_status=RUN_STATUS_FAILED),
        )
        report.event_persisted = True
    except Exception:
        logger.debug("aborted cycle event persist failed", exc_info=True)


def main() -> None:
    setup_logging()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    settings = _load_runner_settings()
    client = BybitClient(settings)
    # Issue #253: the startup universe may be deferred by the SHARED limiter
    # (open breaker).  Wait it out here — still BEFORE the database and the
    # runner lock exist — instead of letting BybitRateLimitError escape and
    # crash-loop under systemd.  ``None`` means SIGTERM/SIGINT arrived while
    # waiting: exit cleanly with NO database/OOS/trading side effect.
    universe = initialize_scanner_universe(client, settings)
    if universe is None:
        logger.info(
            "scanner startup stopped by shutdown signal before database "
            "initialization")
        return
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

    # Prospective OOS observer: fail-open, captures for frozen experiments
    # Fail-closed initialization: prospective_obs = None ensures NameError cannot occur
    # even if the observer factory returns None due to missing registry or connection failure.
    prospective_obs: ProspectiveOOSObserver | None = None
    prospective_obs = _get_prospective_observer(repository)
    if prospective_obs is not None:
        logger.info("prospective OOS observer enabled: experiments=%s", list(prospective_obs._registry.keys()))

    me_early_mae_v2_pipeline = _get_me_early_mae_v2_pipeline(repository)

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

    # Research-only durable capture. Disabled unless explicitly enabled.
    me_capture_spool = None
    if os.getenv("ME_CL_DURABLE_CAPTURE_ENABLED", "0") == "1":
        from app.shadow.me_r_close_location_capture_spool import (
            MECleanCaptureSpool,
        )

        spool_path = Path(
            os.getenv(
                "ME_CL_DURABLE_CAPTURE_PATH",
                str(PROJECT_ROOT / "data" / "me_cl_clean_capture.sqlite3"),
            )
        )

        me_capture_spool = MECleanCaptureSpool(spool_path)
        logger.info("ME clean durable capture enabled: %s", spool_path)

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
        # SCANNER_AC7 Phase A: structured completion facts for THIS cycle
        # (expected/processed/failed/skipped symbols + reasons + transport
        # fact). Created OUTSIDE the try so the abort path below can always
        # report what had been processed before the cycle died.
        report = CycleCompletionReport()
        try:
            total, scanned, failed = run_scan_cycle(
                client, orchestrator, repository, symbols, run_id, settings,
                expectancy_filter=expectancy_filter,
                prospective_obs=prospective_obs,
                me_early_mae_v2_pipeline=me_early_mae_v2_pipeline,
                me_capture_spool=me_capture_spool,
                report=report,
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
                # SCANNER_AC7 Phase A: the dds.scanner_run.status vocabulary is
                # UNCHANGED (COMPLETED/PARTIAL/FAILED CHECK + every SQL mart);
                # report.run_status reproduces the historical mapping
                # byte-for-byte ("COMPLETED" if failed == 0 else "PARTIAL").
                # The richer completion_status (COMPLETE/PARTIAL/FAILED) is
                # logged below and persisted in the CYCLE_COMPLETION payload.
                status=report.run_status,
            )

            t_run_finished = time.monotonic()
            logger.info(
                "cycle #%d done: %d/%d symbols | status=%s | "
                "completion_status=%s | failed=%d skipped=%d | "
                "rate_limit_response=%d limiter_pre_send_skip=%d | "
                "%d setups | %d active signals | scan=%.2fs total=%.2fs",
                cycle, scanned, len(symbols), report.run_status,
                report.completion_status,
                len(report.failed_symbols), len(report.skipped_symbols),
                len(report.rate_limit_response_symbols),
                len(report.limiter_pre_send_skip_symbols),
                total, active_signals,
                t_run_finished - t_run_started, elapsed,
            )
            if not report.is_complete:
                # Explicit, greppable evidence of an incomplete cycle: which
                # symbols were dropped, why, and how long the limiter made us
                # wait (SCANNER_AC7 AC2/AC3/AC4 — never a silent "done").
                logger.warning(
                    "cycle #%d incomplete: %s", cycle, report.log_summary(),
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
            # SCANNER_AC7 Phase A (review P2): diagnostics of the ABORTED
            # cycle. The FAILED status above is written FIRST and the helper
            # cannot change it; the original exception was already logged by
            # logger.exception; and the helper itself never raises, so a
            # diagnostic failure can neither mask the original error nor
            # change how the Scanner finishes.
            report_aborted_cycle(repository, report, cycle, run_id)

        # Sleep in small intervals to respond to shutdown
        delay = seconds_until_next_cycle(
            start, settings.scan_interval, time.monotonic(),
        )
        logger.info("cycle #%d sleeping %.0fs before next cycle", cycle, delay)
        for _ in range(int(delay)):
            if SHUTDOWN:
                break
            time.sleep(1)

    if me_capture_spool is not None:
        try:
            logger.info(
                "ME clean capture final stats: %s",
                me_capture_spool.stats(),
            )
            me_capture_spool.close()
        except Exception:
            logger.exception("ME clean capture spool close failed")

    repository.close()
    logger.info("scanner stopped")


if __name__ == "__main__":
    main()
