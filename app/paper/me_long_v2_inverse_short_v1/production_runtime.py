"""Isolated production runtime for ME_LONG_V2_INVERSE_SHORT_V1 (Issue #268).

A standalone, systemd-managed service that keeps the FROZEN PR #267 contour
(``InverseShortContour``) running against real Bybit market data:

    activation refresh → recovery/reconciliation → open-position exits +
    close-outbox replay → funding settlement → prospective entries (FULL
    mode only) → persistence → structured telemetry/heartbeat

Every step inside ``contour.run_cycle()`` is the EXISTING PR #267 code —
this module only owns the process lifecycle around it:

* a configurable cycle loop with graceful SIGTERM/SIGINT shutdown
  (the current cycle always finishes; committed work is never dropped);
* ONE instance per experiment (OS-level lock file — a second process
  exits with :data:`EXIT_ALREADY_RUNNING` before touching anything);
* a market data feed over the shared ``BybitClient`` (retry budget, API
  telemetry and the GLOBAL rate limiter stay in charge — no bypass);
* fail-closed data policy: stale/missing prices are excluded from the
  price map, so new entries never fill on guessed prices and protective
  exits HOLD instead of closing on stale data (retried next cycle);
* structured per-cycle telemetry (Issue §3.7) + a heartbeat file for
  hung-runtime diagnostics;
* startup directory/writability verification (the
  ``/opt/trad_bot_pr267_release/logs`` PermissionError lesson).

What this runtime NEVER does: create real Bybit orders, write
``dds.paper_trade``, run backfill, activate the experiment, or touch the
production Scanner / Paper / Research Evaluator.

Activation stays TWO-FACTOR and fail-closed (``activation.py`` semantics
are untouched): without ``ME_INVERSE_SHORT_V1_ENABLED=1`` AND a RUNNING
row with the current fingerprint + valid ``freeze_ts`` the runtime starts
happily and does NOTHING — no entries, no captures, no writes.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.config import Settings, load_settings

from .market_data import (
    MarketDataConfig,
    MarketDataProvider,
    MarketDataSnapshot,
)
from .protocol import EXPERIMENT_ID, protocol_fingerprint
from .runner import InverseShortContour
from .store import InverseShortStore, connect_pg_store

logger = logging.getLogger(__name__)

#: Repository root (…/app/paper/me_long_v2_inverse_short_v1/production_runtime.py).
PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: Exit-code contract (also documented in the systemd unit).
EXIT_OK = 0                 # clean shutdown (--once, SIGTERM/SIGINT)
EXIT_FATAL = 1              # startup/runtime fatal error (Restart=on-failure)
EXIT_ALREADY_RUNNING = 3    # another instance holds the lock (never restart-loop)

DEFAULT_LOCK_FILE = PROJECT_ROOT / "var" / "me_inverse_short_runtime.lock"
DEFAULT_HEARTBEAT_FILE = PROJECT_ROOT / "var" / "me_inverse_short_runtime_heartbeat.json"
DEFAULT_LOG_FILE = PROJECT_ROOT / "logs" / "me_inverse_short_runtime.log"


# ---------------------------------------------------------------------------
# Single-instance lock (Issue §3.1: two runtime processes must be impossible)
# ---------------------------------------------------------------------------
class InstanceLockHeld(RuntimeError):
    """Another runtime process already owns the instance lock file."""


def _lock_handle(fh: Any) -> None:
    """Non-blocking OS lock on byte 0 of ``fh`` (released by the OS on exit).

    Windows: ``msvcrt.locking`` byte-range lock; POSIX: ``fcntl.flock``.
    Raises ``OSError`` when the range is already held by another process.
    """
    fh.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(fh: Any) -> None:
    fh.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class RuntimeInstanceLock:
    """Process-lifetime exclusive lock on a dedicated lock file.

    * the lock file is NEVER unlinked (unlinking a file another process
      holds locked breaks mutual exclusion);
    * the OS releases the lock automatically when the process dies, so a
      crashed runtime never leaves the experiment locked;
    * the holder's PID is recorded for diagnostics only — liveness is the
      OS lock, never the PID.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fh: Any | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> None:
        if self._fh is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if fh.tell() == 0 and os.fstat(fh.fileno()).st_size == 0:
                fh.write(b"0")  # ensure byte 0 exists before locking it
                fh.flush()
            _lock_handle(fh)
        except OSError as exc:
            fh.close()
            raise InstanceLockHeld(
                f"ME_LONG_V2_INVERSE_SHORT_V1 runtime already running "
                f"(lock held: {self.path})",
            ) from exc
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()).encode("ascii"))
            fh.flush()
        except OSError:  # pragma: no cover - diagnostics only
            pass
        self._fh = fh
        logger.info(
            "me inverse short runtime instance lock acquired: pid=%d path=%s",
            os.getpid(), self.path,
        )

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock_handle(fh)
        except OSError:  # pragma: no cover - best effort
            logger.debug("instance lock release failed", exc_info=True)
        finally:
            try:
                fh.close()
            except OSError:  # pragma: no cover
                pass

    def __enter__(self) -> "RuntimeInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Process lifecycle helpers
# ---------------------------------------------------------------------------
def ensure_writable(path: str | Path) -> Path:
    """Create the parent directory of ``path`` and PROBE it for writability.

    Fail-fast at startup (exit 1 with an actionable message) instead of
    dying mid-run with a ``PermissionError`` — every writable runtime/log
    directory must be prepared BEFORE the first service start.
    """
    target = Path(path)
    directory = target.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".write_probe_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"RUNTIME_DIR_NOT_WRITABLE:{directory}: {exc}. Prepare the "
            f"writable runtime/log directories (ownership + mode) BEFORE "
            f"the first service start.",
        ) from exc
    return target


def install_signal_handlers(shutdown: threading.Event) -> dict[int, Any]:
    """SIGTERM/SIGINT -> graceful stop at the NEXT cycle boundary.

    The handler only sets the event: the CURRENT cycle (including every
    durable write it performs) always completes, then the loop exits 0.
    Confirmed operations are never interrupted mid-flight.

    Returns the previous handlers so callers (tests, embedders) can
    restore them.
    """
    previous: dict[int, Any] = {}

    def _handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        logger.info(
            "shutdown signal %s received; finishing the current cycle, "
            "then exiting cleanly", signum,
        )
        shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, _handler)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            logger.debug("could not install handler for signal %s", sig)
    return previous


def _connect_store_with_retry(
    settings: Settings,
    *,
    attempts: int,
    retry_s: float,
    shutdown: threading.Event,
) -> InverseShortStore:
    """Open the contour's dedicated PostgreSQL store (bounded retries)."""
    last_error: BaseException | None = None
    for attempt in range(1, max(1, attempts) + 1):
        if shutdown.is_set():
            raise RuntimeError("SHUTDOWN_BEFORE_STORE_CONNECT")
        try:
            return connect_pg_store(
                host=settings.db_host,
                port=int(settings.db_port),
                database=settings.db_name,
                user=settings.db_user,
                password=settings.db_password,
                initial_balance=settings.initial_balance,
            )
        except Exception as exc:  # noqa: BLE001 - bounded startup retry
            last_error = exc
            logger.error(
                "me inverse short runtime: PostgreSQL connect attempt "
                "%d/%d failed: %s: %s",
                attempt, attempts, type(exc).__name__, str(exc)[:200],
            )
            if attempt < attempts and shutdown.wait(max(0.0, retry_s)):
                break
    raise RuntimeError(f"STORE_CONNECT_FAILED: {last_error}")


# ---------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------
class ProductionRuntime:
    """One isolated ME_LONG_V2_INVERSE_SHORT_V1 production process.

    Owns ONLY the loop, the data feed and telemetry; all trading logic
    stays inside the frozen ``InverseShortContour`` (PR #267).
    """

    def __init__(
        self,
        settings: Settings,
        store: InverseShortStore,
        provider: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        interval_s: float | None = None,
        activation_enabled: bool | None = None,
        heartbeat_path: str | Path | None = None,
        telemetry: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.provider = provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        resolved_interval = (
            settings.position_monitor_interval if interval_s is None
            else interval_s
        )
        self.interval_s = float(resolved_interval)
        if not (self.interval_s > 0):
            raise ValueError(f"INTERVAL_INVALID:{resolved_interval!r}")
        self.heartbeat_path = (
            Path(heartbeat_path) if heartbeat_path is not None else None
        )
        self._telemetry = telemetry
        self.shutdown = threading.Event()
        self.last_telemetry: dict[str, Any] | None = None
        self.cycles = 0
        self.cycle_errors = 0

        # The EXISTING contour: activation resolution, engine recovery and
        # the frozen cycle logic all come from PR #267 untouched.
        self.contour = InverseShortContour(
            settings, store, clock=self._clock,
            activation_enabled=activation_enabled,
        )

    # ------------------------------------------------------------------
    # cycle
    # ------------------------------------------------------------------
    @staticmethod
    def _merge_bars(
        snapshot: MarketDataSnapshot, prices: dict[str, float],
    ) -> dict[str, dict[str, float]]:
        """Hand the engine the closed-bar range WIDENED by the live price.

        Both inputs are observable at cycle time (last CLOSED bar + the
        current quote), so no future OHLC can leak in, while a level
        touched during the current (still forming) bar is never missed
        just because the last closed bar did not contain it.
        """
        merged: dict[str, dict[str, float]] = {}
        for symbol, bar in snapshot.bars.items():
            price = prices.get(symbol)
            if price is None:
                continue  # the engine refuses to act without a price anyway
            merged[symbol] = {
                "high": max(float(bar["high"]), float(price)),
                "low": min(float(bar["low"]), float(price)),
            }
        return merged

    def _gather_symbols(
        self,
    ) -> tuple[set[str], set[str], set[str], int]:
        """``(price_symbols, entry_symbols, open_symbols, db_errors)``.

        * open-position symbols are in-memory and ALWAYS priced, so
          STOP/TP/TIMEOUT survives any pending-events store failure;
        * prospective entry events are read ONLY in FULL mode (never in
          EXIT_ONLY/DRAINING, never while the contour is not activated —
          there is nothing to enter, so nothing is requested);
        * a pending-events read failure yields no entry symbols (fail
          closed) and never raises.
        """
        open_symbols = {
            position.symbol
            for position in self.contour.engine.open_positions.values()
        }
        db_errors = 0
        entry_symbols: set[str] = set()
        if self.contour.activated and not self.contour.exit_only:
            try:
                entry_symbols = {
                    event.symbol
                    for event in self.contour.engine.pending_events()
                }
            except Exception:
                db_errors += 1
                logger.warning(
                    "me inverse short runtime: pending entry events "
                    "unavailable (store read failed); new entries fail "
                    "closed for this cycle — protective exits are unaffected",
                    exc_info=True,
                )
        return open_symbols | entry_symbols, entry_symbols, open_symbols, db_errors

    def _entry_refusals(
        self,
        result: dict[str, Any],
        snapshot: MarketDataSnapshot,
        entry_symbols: set[str],
        db_errors: int,
    ) -> list[str]:
        """Honest, per-cause reasons why NO new entry happened this cycle."""
        refusals: list[str] = []
        if not result.get("activated"):
            refusals.append(
                f"ACTIVATION:{result.get('activation_reason', 'UNKNOWN')}",
            )
        elif result.get("exit_only"):
            refusals.append("EXIT_ONLY")
        if result.get("durability_risk"):
            refusals.append("DURABILITY_RISK")
        if not result.get("accounting_consistent", True):
            refusals.append("ACCOUNTING_INCONSISTENT")
        if result.get("pending_events_unavailable"):
            refusals.append("PENDING_EVENTS_UNAVAILABLE")
        refusals.extend(snapshot.entry_refusals(entry_symbols))
        if (
            result.get("open_positions", 0) >= int(self.settings.max_open_positions)
        ):
            refusals.append("ENTRY_CAP")
        if db_errors:
            refusals.append("DB_ERRORS")
        return refusals

    def run_cycle(self, cycle: int) -> dict[str, Any]:
        """Run exactly one full cycle and return its telemetry record."""
        started = time.monotonic()
        now = self._clock()
        db_errors = 0

        # -- 1) activation refresh (fail closed) + raw row for telemetry --
        activation_status = "UNKNOWN"
        record = None
        try:
            record = self.store.load_activation()
            activation_status = (
                record.status if record is not None else "ROW_MISSING"
            )
        except Exception:
            db_errors += 1
            activation_status = "UNREADABLE"
            logger.warning(
                "me inverse short runtime: activation row unreadable "
                "(entries fail closed)", exc_info=True,
            )
        self.contour.refresh_activation()
        identity = self.contour.activation
        if identity is not None:
            activation_status = identity.status

        # -- 2/3) symbols: open positions always, entries only in FULL ----
        price_symbols, entry_symbols, open_symbols, symbol_db_errors = (
            self._gather_symbols()
        )
        db_errors += symbol_db_errors

        # -- market data (never raises; stale/missing values excluded) ----
        snapshot = self.provider.snapshot(
            price_symbols=price_symbols,
            bar_symbols=open_symbols,
            funding_symbols=open_symbols,
        )

        # -- 3..6) the frozen contour cycle (PR #267 ordering preserved) --
        prices = dict(snapshot.prices)
        bars = self._merge_bars(snapshot, prices)
        result = self.contour.run_cycle(
            prices, snapshot.funding_rates_percent, bars,
        )

        # -- structured telemetry (Issue §3.7) ---------------------------
        refusals = self._entry_refusals(
            result, snapshot, entry_symbols, db_errors,
        )
        freeze_ts = None
        if identity is not None and identity.freeze_ts is not None:
            freeze_ts = identity.freeze_ts.isoformat()
        elif record is not None and record.freeze_ts is not None:
            freeze_ts = str(record.freeze_ts)
        duration_ms = int(round((time.monotonic() - started) * 1000.0))
        telemetry: dict[str, Any] = {
            "event": "me_inverse_short_runtime_cycle",
            "ts": now.isoformat(),
            "experiment_id": EXPERIMENT_ID,
            "cycle": cycle,
            "activation_status": activation_status,
            "activation_mode": self.contour.activation_mode,
            "activation_reason": result.get("activation_reason"),
            "protocol_fingerprint": protocol_fingerprint(),
            "freeze_ts": freeze_ts,
            "cycle_duration_ms": duration_ms,
            "market_data_age_ms": snapshot.max_price_age_ms,
            "symbols_checked": len(price_symbols),
            "pending_events": result.get("pending_events"),
            "entries": result.get("entries", 0),
            "exits": result.get("exits", 0),
            "exit_reasons": result.get("exit_reasons", []),
            "open_positions": result.get("open_positions", 0),
            "pending_closes": result.get("pending_closes", 0),
            "durability_risk": result.get("durability_risk", False),
            "accounting_consistent": result.get("accounting_consistent", False),
            "funding_pending": self.contour.engine.unsettled_funding_periods(),
            "api_errors": snapshot.api_errors,
            "db_errors": db_errors,
            "entry_refusals": refusals,
            "market_data_statuses": snapshot.summary(),
            "balance": result.get("balance"),
            "equity": result.get("equity"),
            "cycle_errors": self.cycle_errors,
        }
        if refusals:
            logger.info(
                "me inverse short runtime: new entries refused: %s",
                "; ".join(refusals),
            )
        return telemetry

    def _failure_record(self, cycle: int, exc: BaseException) -> dict[str, Any]:
        """Best-effort telemetry for a cycle that raised (fields intact)."""
        identity = self.contour.activation
        return {
            "event": "me_inverse_short_runtime_cycle",
            "ts": self._clock().isoformat(),
            "experiment_id": EXPERIMENT_ID,
            "cycle": cycle,
            "activation_status": identity.status if identity else "UNKNOWN",
            "activation_mode": self.contour.activation_mode,
            "activation_reason": self.contour.activation_reason,
            "protocol_fingerprint": protocol_fingerprint(),
            "freeze_ts": (
                identity.freeze_ts.isoformat() if identity else None
            ),
            "cycle_duration_ms": None,
            "market_data_age_ms": None,
            "symbols_checked": 0,
            "pending_events": None,
            "entries": 0,
            "exits": 0,
            "exit_reasons": [],
            "open_positions": len(self.contour.engine.open_positions),
            "pending_closes": len(self.contour.engine.pending_closes),
            "durability_risk": self.contour.engine.durability_risk,
            "accounting_consistent": self.contour.engine.accounting_consistent,
            "funding_pending": self.contour.engine.unsettled_funding_periods(),
            "api_errors": 0,
            "db_errors": 1,
            "entry_refusals": [f"CYCLE_ERROR:{type(exc).__name__}"],
            "market_data_statuses": {},
            "balance": None,
            "equity": None,
            "cycle_errors": self.cycle_errors,
        }

    # ------------------------------------------------------------------
    # loop
    # ------------------------------------------------------------------
    def _emit(self, telemetry: dict[str, Any]) -> None:
        self.last_telemetry = telemetry
        line = json.dumps(telemetry, default=str, sort_keys=True)
        if self._telemetry is not None:
            try:
                self._telemetry(telemetry)
            except Exception:  # pragma: no cover - observers are optional
                logger.debug("telemetry sink failed", exc_info=True)
        logger.info("me inverse short runtime cycle: %s", line)

    def _write_heartbeat(self, telemetry: dict[str, Any]) -> None:
        """Atomically refresh the heartbeat file (hung-runtime diagnostics)."""
        if self.heartbeat_path is None:
            return
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "cycle": telemetry.get("cycle"),
            "cycle_duration_ms": telemetry.get("cycle_duration_ms"),
            "state": "running",
            "activation_mode": telemetry.get("activation_mode"),
            "open_positions": telemetry.get("open_positions"),
            "durability_risk": telemetry.get("durability_risk"),
        }
        try:
            tmp = self.heartbeat_path.with_suffix(
                f"{self.heartbeat_path.suffix}.tmp",
            )
            tmp.write_text(
                json.dumps(payload, default=str), encoding="utf-8",
            )
            os.replace(tmp, self.heartbeat_path)
        except OSError:
            logger.warning(
                "me inverse short runtime: heartbeat write failed",
                exc_info=True,
            )

    def run(
        self,
        *,
        once: bool = False,
        max_cycles: int | None = None,
        shutdown: threading.Event | None = None,
    ) -> int:
        """Run the loop until shutdown / ``--once`` / ``max_cycles``.

        Returns :data:`EXIT_OK` on every clean stop.  Cycle-level errors
        are contained (logged + counted) — they never crash the loop, and
        they can never affect the Scanner/Paper/Evaluator processes.
        """
        stop = self.shutdown if shutdown is None else shutdown
        cycle = 0
        logger.info(
            "me inverse short runtime loop started: interval=%.1fs "
            "activated=%s mode=%s reason=%s open_positions=%d balance=%.2f",
            self.interval_s, self.contour.activated,
            self.contour.activation_mode, self.contour.activation_reason,
            len(self.contour.engine.open_positions),
            self.contour.engine.balance,
        )
        while not stop.is_set():
            cycle += 1
            self.cycles = cycle
            try:
                telemetry = self.run_cycle(cycle)
            except Exception as exc:
                self.cycle_errors += 1
                logger.exception(
                    "me inverse short runtime cycle %d failed (contained: "
                    "the runtime continues; Scanner/Paper/Evaluator are "
                    "separate processes and unaffected)", cycle,
                )
                try:
                    telemetry = self._failure_record(cycle, exc)
                except Exception:  # pragma: no cover - defensive
                    logger.exception("failure telemetry unavailable")
                    telemetry = {
                        "event": "me_inverse_short_runtime_cycle",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "experiment_id": EXPERIMENT_ID,
                        "cycle": cycle,
                        "entries": 0,
                        "exits": 0,
                        "cycle_errors": self.cycle_errors,
                    }
            self._emit(telemetry)
            self._write_heartbeat(telemetry)
            if once or (max_cycles is not None and cycle >= max_cycles):
                break
            stop.wait(self.interval_s)
        self._final_report()
        return EXIT_OK

    def _final_report(self) -> None:
        try:
            snapshot = self.contour.engine.snapshot()
            logger.info(
                "me inverse short runtime stopped: %s",
                json.dumps(snapshot, default=str),
            )
        except Exception:  # pragma: no cover - best effort
            logger.debug("final snapshot failed", exc_info=True)
        if self.contour.engine.durability_risk:
            # Honest shutdown (PR #267 P1): never claim durable recovery
            # for a terminal snapshot PostgreSQL never accepted.
            logger.critical(
                "me inverse short runtime shutdown with UNRESOLVED close "
                "durability risk: %d terminal snapshot(s) exist only in "
                "process memory: keys=%s",
                len(self.contour.engine.unstaged_close_keys),
                list(self.contour.engine.unstaged_close_keys),
            )


# ---------------------------------------------------------------------------
# CLI entrypoint: python -m app.paper.me_long_v2_inverse_short_v1.production_runtime
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=(
            "python -m app.paper.me_long_v2_inverse_short_v1."
            "production_runtime"
        ),
        description=(
            "Isolated ME_LONG_V2_INVERSE_SHORT_V1 prospective paper runtime "
            "(activation OFF by default; never places real orders)."
        ),
    )
    parser.add_argument(
        "--interval", type=float, default=None,
        help="cycle interval in seconds "
             "(default: settings.position_monitor_interval)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="run exactly one cycle and exit 0",
    )
    parser.add_argument(
        "--max-cycles", type=int, default=None,
        help="stop cleanly after N cycles (diagnostics)",
    )
    parser.add_argument(
        "--lock-file", default=None,
        help=f"single-instance lock file (default: {DEFAULT_LOCK_FILE})",
    )
    parser.add_argument(
        "--heartbeat-file", default=None,
        help=f"heartbeat file (default: {DEFAULT_HEARTBEAT_FILE})",
    )
    parser.add_argument(
        "--log-file", default=None,
        help=f"runtime log file (default: {DEFAULT_LOG_FILE})",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--db-connect-attempts", type=int, default=10,
        help="bounded PostgreSQL connect attempts at startup",
    )
    parser.add_argument(
        "--db-connect-retry-s", type=float, default=5.0,
        help="delay between startup PostgreSQL connect attempts",
    )
    return parser


def setup_logging(log_file: Path, level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    log_file = Path(args.log_file) if args.log_file else DEFAULT_LOG_FILE
    heartbeat_file = (
        Path(args.heartbeat_file) if args.heartbeat_file
        else DEFAULT_HEARTBEAT_FILE
    )
    lock_file = Path(args.lock_file) if args.lock_file else DEFAULT_LOCK_FILE

    # 1) Writable runtime/log directories BEFORE anything else (the
    #    /opt/trad_bot_pr267_release/logs PermissionError lesson).
    try:
        ensure_writable(log_file)
        ensure_writable(heartbeat_file)
        ensure_writable(lock_file)
    except RuntimeError as exc:
        print(f"CRITICAL: {exc}", file=sys.stderr)
        return EXIT_FATAL

    # 2) Single instance — BEFORE logging configuration and before any
    #    database or market-data work.
    lock = RuntimeInstanceLock(lock_file)
    try:
        lock.acquire()
    except InstanceLockHeld as exc:
        print(f"CRITICAL: {exc}", file=sys.stderr)
        return EXIT_ALREADY_RUNNING

    setup_logging(log_file, args.log_level)
    logger.info("me inverse short runtime: logging initialised")

    shutdown = threading.Event()
    install_signal_handlers(shutdown)
    store: InverseShortStore | None = None
    try:
        try:
            settings = load_settings()
            config = MarketDataConfig.from_env()
        except (ValueError, OSError) as exc:
            logger.critical(
                "me inverse short runtime refuses to start (invalid "
                "configuration): %s", exc,
            )
            return EXIT_FATAL

        try:
            store = _connect_store_with_retry(
                settings,
                attempts=args.db_connect_attempts,
                retry_s=args.db_connect_retry_s,
                shutdown=shutdown,
            )
        except RuntimeError as exc:
            if shutdown.is_set():
                logger.info("shutdown requested during startup; stopping")
                return EXIT_OK
            logger.critical(
                "me inverse short runtime refuses to start: %s "
                "(systemd Restart=on-failure will retry)", exc,
            )
            return EXIT_FATAL

        from app.exchange.bybit_client import BybitClient

        provider = MarketDataProvider(BybitClient(settings), config=config)
        runtime = ProductionRuntime(
            settings, store, provider,
            interval_s=args.interval,
            heartbeat_path=heartbeat_file,
        )
        logger.info(
            "me inverse short runtime starting: flag_enabled=%s "
            "activated=%s mode=%s reason=%s interval=%.1fs",
            os.environ.get("ME_INVERSE_SHORT_V1_ENABLED", "<unset>"),
            runtime.contour.activated,
            runtime.contour.activation_mode,
            runtime.contour.activation_reason,
            runtime.interval_s,
        )
        return runtime.run(
            once=args.once, max_cycles=args.max_cycles, shutdown=shutdown,
        )
    except Exception:
        logger.exception("me inverse short runtime fatal error")
        return EXIT_FATAL
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # pragma: no cover - best effort
                logger.debug("store close failed", exc_info=True)
        lock.release()
        logger.info("me inverse short runtime process exiting")


if __name__ == "__main__":
    sys.exit(main())
