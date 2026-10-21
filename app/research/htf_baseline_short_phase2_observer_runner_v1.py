"""Issue #176 Phase-2 observe-only observer runtime (Blocker B7).

A dedicated, disabled-by-default runtime for
``HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1``. It never touches paper/live
execution, never routes signals to trading, and never modifies existing
experiments.

Fail-closed startup contract:
  * if the experiment is not registered, or not RUNNING with a freeze and a
    matching protocol fingerprint, the runner exits WITHOUT creating any
    observation (exit code 3, clear reason);
  * if the three freeze sources disagree, same behavior;
  * if a checkpoint belongs to a different freeze boundary, it is rejected
    (no silent reinterpretation).

Processing contract (per closed 5m bar):
  validate 5m series -> closed 1H view with explicit coverage -> causal level
  universe -> per-level contact/confirmation inputs -> atomic episode advance
  -> freeze-gated idempotent emission -> checkpoint advance.

Delivery guarantee: AT-LEAST-ONCE with IDEMPOTENT writes (DB unique key +
emitted-event set in the checkpoint). Exactly-once is NOT claimed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import stat as stat_module
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.models import Candle
from app.research.htf_baseline_short_phase2_contracts_v1 import (
    BAR_MS,
    ContractViolation,
    aggregate_closed_htf,
)
from app.research.htf_baseline_short_phase2_episode_v1 import (
    Phase2Episodes,
    source_event_id_for,
)
from app.research.htf_baseline_short_phase2_freeze_v1 import (
    EligibilityDecision,
    Phase2FreezeBoundary,
    parse_ts_ms,
    reject_all,
)
from app.research.htf_baseline_short_phase2_level_universe_v1 import (
    build_resistance_universe,
    is_touch,
)
from app.research.htf_baseline_short_phase2_protocol_v1 import (
    EXPERIMENT_ID,
    protocol_fingerprint,
)
from app.research.htf_baseline_short_phase2_state_store_v1 import (
    StateStoreError,
    WriterLease,
    WriterLeaseError,
    read_checkpoint,
    write_checkpoint,
)

log = logging.getLogger("htf.phase2.observer")

EXIT_OK = 0
EXIT_STOP_REQUESTED = 0
EXIT_NOT_RUNNABLE = 3
EXIT_STATE_ERROR = 4
EXIT_WRITER_ERROR = 5

CANONICAL_WINDOW = 200
MIN_EXEC_BARS = 20
MIN_HTF_BARS = 26
DEFAULT_HTF_HISTORY_HOURS = 200


class NotRunnableError(RuntimeError):
    """Registration/freeze gate refused startup (fail-closed)."""


class CheckpointBoundaryError(StateStoreError):
    """Checkpoint belongs to a different freeze boundary; reject, never reuse."""


@dataclass
class RunnerConfig:
    experiment_id: str = EXPERIMENT_ID
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    checkpoint_path: str = "var/htf_phase2/state.json"
    warmup_bars: int = 400
    poll_seconds: float = 300.0
    dry_run: bool = True
    once: bool = False
    max_cycles: int | None = None
    stale_lease_after_s: float = 600.0
    allow_stale_takeover: bool = False
    protocol_fingerprint: str = field(default_factory=protocol_fingerprint)


@dataclass
class Metrics:
    cycles: int = 0
    bars_processed: int = 0
    emissions_eligible: int = 0
    emissions_rejected: int = 0
    duplicate_suppressions: int = 0
    coverage_gap_bars: int = 0
    checkpoint_writes: int = 0
    writer_errors: int = 0
    writer_rejected: int = 0
    quarantine_bars: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class InMemoryWriter:
    """Simulated persistence for dry-run/tests (records, never a DB)."""

    def __init__(self):
        self.rows: list[dict] = []
        self.seen: set[str] = set()

    def emit(self, events: list[dict], boundary) -> SimpleNamespace:
        inserted = deduplicated = rejected = 0
        for event in events:
            decision = boundary.check_event(
                signal_time=event["known_at_ms"],
                first_touch_time=event["first_touch_open_ms"],
                warmup_origin=bool(event.get("warmup_origin")),
                resumed_pre_freeze=bool(event.get("resumed_pre_freeze", False)),
            )
            if not decision:
                rejected += 1
                continue
            if event["source_event_id"] in self.seen:
                deduplicated += 1
                continue
            self.seen.add(event["source_event_id"])
            self.rows.append(dict(event))
            inserted += 1
        return SimpleNamespace(inserted=inserted, deduplicated=deduplicated, rejected=rejected)


class FileCandleSource:
    """CSV candle source (SYMBOL_5m.csv with timestamp,open,high,low,close,volume)."""

    def __init__(self, data_dir: str | os.PathLike):
        self.data_dir = Path(data_dir)

    def load(self, symbol: str) -> list[Candle]:
        path = self.data_dir / f"{symbol}_5m.csv"
        rows: list[Candle] = []
        with path.open(newline="", encoding="utf-8") as handle:
            import csv as _csv

            for row in _csv.DictReader(handle):
                rows.append(
                    Candle(
                        int(row["timestamp"]),
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        float(row["volume"]),
                    )
                )
        return rows


def build_boundary(
    *,
    experiment_id: str,
    protocol_freeze_ts,
    registry_freeze_ts,
    database_freeze_ts,
    protocol_fingerprint_hex: str,
) -> Phase2FreezeBoundary:
    try:
        return Phase2FreezeBoundary.create(
            experiment_id=experiment_id,
            protocol_freeze_ts=protocol_freeze_ts,
            registry_freeze_ts=registry_freeze_ts,
            database_freeze_ts=database_freeze_ts,
            protocol_fingerprint=protocol_fingerprint_hex,
        )
    except Exception as exc:  # fail-closed on any missing/mismatched source
        raise NotRunnableError(f"freeze boundary unavailable: {exc}") from exc


class Phase2ObserverRunner:
    def __init__(
        self,
        config: RunnerConfig,
        *,
        boundary: Phase2FreezeBoundary,
        source,
        writer=None,
        db_conn=None,
    ):
        self.config = config
        self.boundary = boundary
        self.source = source
        self.writer = writer
        self.db_conn = db_conn
        self.metrics = Metrics()
        self._stop = False
        self._state: dict[str, Phase2Episodes] = {}
        self._cursors: dict[str, int] = {}
        self._emitted: set[str] = set()
        self._known_levels: dict[str, dict] = {}
        self._warmup_done: dict[str, int] = {}
        self._coverage_reports: list[dict] = []
        self._provenance: dict = {
            "candle_source": type(source).__name__,
            "htf_source": "AGGREGATED_5M",
        }

    # ------------------------------------------------------------- lifecycle
    def request_stop(self, *_args) -> None:
        log.info("stop requested; finishing current bar")
        self._stop = True

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self.request_stop)
            except (ValueError, OSError):
                pass

    # ------------------------------------------------------------- state I/O
    def _checkpoint_payload(self) -> dict:
        return {
            "experiment_id": self.config.experiment_id,
            "boundary_freeze_ts_ms": self.boundary.freeze_ts_ms,
            "protocol_fingerprint": self.boundary.protocol_fingerprint,
            "created_at_ms": int(time.time() * 1000),
            "provenance": dict(self._provenance),
            "coverage_reports": list(self._coverage_reports),
            "emitted": sorted(self._emitted),
            "known_levels": self._known_levels,
            "cursors": dict(self._cursors),
            "warmup_done": dict(self._warmup_done),
            "symbols": {
                symbol: state.checkpoint() for symbol, state in self._state.items()
            },
        }

    def save_checkpoint(self) -> None:
        write_checkpoint(self.config.checkpoint_path, self._checkpoint_payload())
        self.metrics.checkpoint_writes += 1

    def load_checkpoint(self) -> bool:
        path = Path(self.config.checkpoint_path)
        if not path.exists():
            return False
        payload = read_checkpoint(path)  # raises on corrupt/legacy
        if payload.get("boundary_freeze_ts_ms") != self.boundary.freeze_ts_ms:
            raise CheckpointBoundaryError(
                "checkpoint belongs to a different freeze boundary; "
                "refusing to resume (no silent reinterpretation)"
            )
        if payload.get("protocol_fingerprint") != self.boundary.protocol_fingerprint:
            raise CheckpointBoundaryError("checkpoint protocol fingerprint mismatch")
        self._emitted = set(payload.get("emitted", []))
        self._known_levels = dict(payload.get("known_levels", {}))
        self._cursors = dict(payload.get("cursors", {}))
        self._warmup_done = dict(payload.get("warmup_done", {}))
        self._coverage_reports = list(payload.get("coverage_reports", []))
        if payload.get("provenance"):
            self._provenance = dict(payload["provenance"])
        self._state = {
            symbol: Phase2Episodes.restore(data)
            for symbol, data in payload.get("symbols", {}).items()
        }
        return True

    # ------------------------------------------------------------- processing
    def process_symbol_bars(self, symbol: str, candles: list[Candle]) -> list[dict]:
        """Advance one symbol over its closed 5m series; returns emitted events."""
        from app.research.htf_keylevel_point_b import _causal_atr_series, _find_reaction_end

        if symbol not in self._state:
            self._state[symbol] = Phase2Episodes(
                experiment_id=self.config.experiment_id, symbol=symbol
            )
        state = self._state[symbol]
        emitted_events: list[dict] = []
        start_index = self._cursors.get(symbol, 0)
        warmup_budget = max(0, self.config.warmup_bars - self._warmup_done.get(symbol, 0))

        for index in range(start_index, len(candles)):
            if self._stop:
                break
            bar = candles[index]
            open_ms = int(bar.timestamp)
            asof = open_ms + BAR_MS

            prefix = candles[max(0, index - CANONICAL_WINDOW + 1): index + 1]
            try:
                htf, coverage = aggregate_closed_htf(candles[: index + 1], asof)
            except ContractViolation:
                self.metrics.coverage_gap_bars += 1
                self._cursors[symbol] = index + 1
                continue
            # CP4 gap policy: an incomplete/missing 1H hour must never silently
            # change level selection. Quarantine the symbol at the first dirty
            # bar (cursor frozen) and wait for the source data to be completed;
            # identity stays stable across quarantine/repair.
            if not coverage.clean:
                self.metrics.quarantine_bars += 1
                self._coverage_reports.append(
                    {
                        "symbol": symbol,
                        "asof_ms": asof,
                        "action": "QUARANTINED",
                        "partial_hours": list(coverage.partial_hours),
                        "missing_hours": list(coverage.missing_hours),
                    }
                )
                break
            htf = htf[-DEFAULT_HTF_HISTORY_HOURS:]
            # warmup accounting happens ONLY for bars the machine actually
            # advances over; quarantined bars never consume warmup budget
            warmup = warmup_budget > 0
            if warmup:
                warmup_budget -= 1
                self._warmup_done[symbol] = self._warmup_done.get(symbol, 0) + 1
            known = self._known_levels.setdefault(symbol, {})
            contacts: dict[str, tuple[float, int, bool]] = {}
            confirmations: list[str] = []
            unavailable: list[str] = []
            try:
                if len(prefix) >= MIN_EXEC_BARS and len(htf) >= MIN_HTF_BARS:
                    universe, _diag = build_resistance_universe(
                        htf, prefix, asof, symbol=symbol
                    )
                    for record in universe:
                        known.setdefault(
                            record.level_id,
                            {
                                "level_id": record.level_id,
                                "price": record.price,
                                "available_ms": record.available_ms,
                            },
                        )
                    atr = _causal_atr_series(prefix)
                    for key in sorted(known):
                        level = known[key]
                        if int(level["available_ms"]) > open_ms:
                            continue
                        touch = is_touch(
                            bar, float(level["price"]), int(level["available_ms"]), atr[-1]
                        )
                        contacts[key] = (float(level["price"]), int(level["available_ms"]), touch)
                        eid = state.pending.get(key)
                        # P2-06/P2-09: confirmation allowed on a touching bar only
                        # while the contact run is active (continuation).
                        if eid and (not touch or key in state.active):
                            record = state.episodes[eid]
                            first = record["first_touch_open_ms"]
                            matching = [i for i, c in enumerate(prefix) if int(c.timestamp) == first]
                            if matching:
                                candidate = _find_reaction_end(
                                    closed=prefix,
                                    touch_index=matching[0],
                                    level=SimpleNamespace(price=record["level_price_at_touch"]),
                                    direction="SHORT",
                                    atr_by_index=atr,
                                )
                                if candidate is not None and candidate[0] == len(prefix) - 1:
                                    confirmations.append(key)
                            else:
                                unavailable.append(key)
            except ContractViolation as exc:
                # 5m contract violation (gap/duplicate/conflict...) or universe
                # coverage refusal: quarantine instead of silently re-selecting
                self.metrics.coverage_gap_bars += 1
                self._coverage_reports.append(
                    {
                        "symbol": symbol,
                        "asof_ms": asof,
                        "action": "QUARANTINED",
                        "reason": str(exc),
                    }
                )
                break
            emitted = state.advance(
                open_ms=open_ms,
                level_contacts=contacts,
                confirmations=confirmations,
                unavailable=unavailable,
                warmup=warmup,
            )
            for event in emitted:
                event["provenance"] = {
                    "candle_source": self._provenance.get("candle_source"),
                    "htf_source": self._provenance.get("htf_source", "AGGREGATED_5M"),
                    "coverage_clean": True,
                }
                decision = self.boundary.check_event(
                    signal_time=event["known_at_ms"],
                    first_touch_time=event["first_touch_open_ms"],
                    warmup_origin=bool(event.get("warmup_origin")),
                )
                if not decision:
                    self.metrics.emissions_rejected += 1
                    continue
                if event["source_event_id"] in self._emitted:
                    self.metrics.duplicate_suppressions += 1
                    continue
                emitted_events.append(event)
            self._cursors[symbol] = index + 1
            self.metrics.bars_processed += 1

        if emitted_events and self.writer is not None:
            result = self.writer.emit(emitted_events, self.boundary)
            self.metrics.emissions_eligible += getattr(result, "inserted", 0)
            self.metrics.duplicate_suppressions += getattr(result, "deduplicated", 0)
            # writer-side rejections are counted separately: events reaching the
            # writer were already boundary-filtered above, so any writer-level
            # rejection is drift, never a second count of the same rejection
            self.metrics.writer_rejected += getattr(result, "rejected", 0)
            for event in emitted_events:
                self._emitted.add(event["source_event_id"])
        elif emitted_events:
            for event in emitted_events:
                self._emitted.add(event["source_event_id"])
        return emitted_events

    # ------------------------------------------------------------------- run
    def run_once(self) -> int:
        lease = WriterLease(
            self.config.checkpoint_path,
            instance_id=f"{self.config.experiment_id}:{os.getpid()}",
            stale_after_s=self.config.stale_lease_after_s,
        )
        try:
            lease.acquire(allow_stale_takeover=self.config.allow_stale_takeover)
            self.load_checkpoint()
            for symbol in self.config.symbols:
                candles = self.source.load(symbol)
                self.process_symbol_bars(symbol, candles)
                lease.heartbeat()
            self.save_checkpoint()
            self.metrics.cycles += 1
            log.info("cycle complete metrics=%s", json.dumps(self.metrics.as_dict()))
        except WriterLeaseError as exc:
            self.metrics.writer_errors += 1
            log.error("writer lease failure: %s", exc)
            return EXIT_STATE_ERROR
        except (StateStoreError, CheckpointBoundaryError) as exc:
            self.metrics.writer_errors += 1
            log.error("state store failure: %s", exc)
            return EXIT_STATE_ERROR
        except Exception as exc:
            # persistence-specific and unexpected errors map to a deterministic
            # exit code; the checkpoint was not advanced, so restart replays
            from app.research.htf_baseline_short_phase2_persistence_v1 import (
                P2PersistenceError,
            )

            if isinstance(exc, P2PersistenceError):
                self.metrics.writer_errors += 1
                log.error("persistence failure: %s", exc)
                return EXIT_WRITER_ERROR
            raise
        finally:
            lease.release()
        return EXIT_OK

    def run_loop(self) -> int:
        cycles = 0
        while not self._stop:
            code = self.run_once()
            if code != EXIT_OK:
                return code
            cycles += 1
            if self.config.once:
                break
            if self.config.max_cycles is not None and cycles >= self.config.max_cycles:
                break
            time.sleep(self.config.poll_seconds)
        return EXIT_OK


def load_activation_config(path: str | os.PathLike) -> dict:
    """Load the operator-maintained runtime activation config (authority 3).

    Expected JSON: {"experiment_id": ..., "freeze_ts": <ISO8601>,
    "protocol_fingerprint": <64-hex>}. This file is deployed by the operator at
    activation time and is maintained independently of both the protocol
    constant and the database activation record.
    """
    try:
        data = json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NotRunnableError(f"activation config unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise NotRunnableError("activation config must be a JSON object")
    for key in ("experiment_id", "freeze_ts", "protocol_fingerprint"):
        if not data.get(key):
            raise NotRunnableError(f"activation config missing {key}")
    return data


def require_runnable(
    db_conn,
    config: RunnerConfig,
    *,
    activation: dict | None = None,
    protocol_freeze_ts: str | None = None,
) -> Phase2FreezeBoundary:
    """Fail-closed gate over THREE independently maintained freeze authorities.

    1. Approved frozen protocol — ``protocol_v1.approved_freeze_ts()`` (or an
       explicitly injected value in tests). None means the protocol is not
       frozen yet -> refuse to run.
    2. Durable experiment registration — the database activation record
       (freeze_ts + protocol_fingerprint written once by ``activate()``).
    3. Runtime activation configuration — the operator-deployed activation
       file (``activation`` mapping from :func:`load_activation_config`).

    No source is derived from another, so a stale or missing source can never
    be masked by copying. Every missing source or pairwise disagreement
    rejects startup; fingerprints must additionally match the fingerprint of
    the code actually running (``config.protocol_fingerprint``).
    """
    from app.research.htf_baseline_short_phase2_persistence_v1 import (
        freeze_sources,
        load_experiment_state,
    )
    from app.research.htf_baseline_short_phase2_protocol_v1 import approved_freeze_ts

    state = load_experiment_state(db_conn, config.experiment_id)
    sources = freeze_sources(db_conn, config.experiment_id)

    if state.protocol_fingerprint != config.protocol_fingerprint:
        raise NotRunnableError(
            "protocol fingerprint mismatch between runtime code and registration "
            "(fail closed; no observations)"
        )
    if protocol_freeze_ts is None:
        protocol_freeze_ts = approved_freeze_ts()
    if not protocol_freeze_ts:
        raise NotRunnableError(
            "protocol freeze authority is empty: the protocol is not frozen "
            "(fail closed; no observations)"
        )
    if activation is None:
        raise NotRunnableError(
            "runtime activation configuration is missing (fail closed; no observations)"
        )
    if activation.get("experiment_id") != config.experiment_id:
        raise NotRunnableError("activation config experiment_id mismatch")
    if activation.get("protocol_fingerprint") != config.protocol_fingerprint:
        raise NotRunnableError(
            "activation config fingerprint mismatch (outdated activation "
            "configuration; fail closed)"
        )
    if sources.get("database") is None:
        raise NotRunnableError("database freeze authority is empty")
    return build_boundary(
        experiment_id=config.experiment_id,
        protocol_freeze_ts=protocol_freeze_ts,
        registry_freeze_ts=activation.get("freeze_ts"),
        database_freeze_ts=sources["database"],
        protocol_fingerprint_hex=config.protocol_fingerprint,
    )


DB_CONNECT_TIMEOUT_S = 5
#: shared/admin roles the observer must never authenticate as
_FORBIDDEN_DB_USERS = frozenset({"postgres", "trad_bot", "analytics_runner", "analytics_agent"})


class DbCredentialsError(RuntimeError):
    """Missing or unsafe database credentials (fail closed; never guess)."""


def check_password_file_stat(st_mode: int, st_uid: int, euid: int | None = None) -> None:
    """Validate password-file metadata (F1 hardening); raises DbCredentialsError.

    Pure logic (usable on any platform): the file must be a regular file, must
    not be group/world accessible (``mode & 0o077`` must be 0; 0600/0400 pass)
    and must be owned by the service user or root. ``euid`` defaults to the
    current effective uid.
    """
    if not stat_module.S_ISREG(st_mode):
        raise DbCredentialsError("password file must be a regular file (not a symlink or device)")
    if stat_module.S_IMODE(st_mode) & 0o077:
        raise DbCredentialsError(
            "password file must not be group/world accessible (require 0600 or stricter)"
        )
    if os.name == "posix":
        owner = os.geteuid() if euid is None else euid
        if st_uid not in (owner, 0):
            raise DbCredentialsError(
                "password file must be owned by the service user or root"
            )


def _read_password_file(path: str) -> str:
    """Read the password file defensively (F1): symlink/TOCTOU resistant on POSIX.

    The password value never appears in exceptions or logs.
    """
    if os.name == "posix":
        try:
            pre = os.lstat(path)
        except OSError as exc:
            raise DbCredentialsError(f"password file unreadable: {exc}") from exc
        if stat_module.S_ISLNK(pre.st_mode):
            raise DbCredentialsError("password file must not be a symlink")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise DbCredentialsError(f"password file unreadable: {exc}") from exc
        try:
            fst = os.fstat(fd)  # stat the opened object: TOCTOU-safe
            check_password_file_stat(fst.st_mode, fst.st_uid)
            data = os.read(fd, 65536)
        finally:
            os.close(fd)
    else:
        # Windows: POSIX mode/uid semantics do not apply; content checks below
        try:
            data = Path(path).read_bytes()[:65536]
        except OSError as exc:
            raise DbCredentialsError(f"password file unreadable: {exc}") from exc
    lines = data.decode("utf-8", errors="replace").splitlines()
    password = next((line.strip() for line in lines if line.strip()), "")
    if not password:
        raise DbCredentialsError("password file contains no password")
    return password


def load_db_credentials() -> dict:
    """Load dedicated-role credentials from HTF_P2_DB_* (CP2 hardening).

    Fail-closed contract:
      * ``HTF_P2_DB_USER`` is REQUIRED and must be the dedicated observer role —
        there is no default user and shared/admin roles are rejected;
      * ``HTF_P2_DB_NAME`` is REQUIRED (no implicit database);
      * the password comes from ``HTF_P2_DB_PASSWORD`` or, preferably, from the
        file named by ``HTF_P2_DB_PASSWORD_FILE`` (first non-empty line). No
        password -> refuse to connect (never trust/peer fallback);
      * scanner/.env credentials are never read — only HTF_P2_DB_* is consulted.
    """
    user = os.environ.get("HTF_P2_DB_USER", "").strip()
    if not user:
        raise DbCredentialsError(
            "HTF_P2_DB_USER must explicitly name the dedicated observer role "
            "(no default user)"
        )
    if user in _FORBIDDEN_DB_USERS:
        raise DbCredentialsError(
            "HTF_P2_DB_USER must be the dedicated observer role, not a shared "
            "or administrative role (refusing)"
        )
    name = os.environ.get("HTF_P2_DB_NAME", "").strip()
    if not name:
        raise DbCredentialsError("HTF_P2_DB_NAME is required (no implicit database)")
    password = os.environ.get("HTF_P2_DB_PASSWORD")
    if password is None or password == "":
        pw_file = os.environ.get("HTF_P2_DB_PASSWORD_FILE", "").strip()
        if not pw_file:
            raise DbCredentialsError(
                "HTF_P2_DB_PASSWORD or HTF_P2_DB_PASSWORD_FILE is required "
                "(refusing password-less connection)"
            )
        password = _read_password_file(pw_file)
    return {
        "host": os.environ.get("HTF_P2_DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("HTF_P2_DB_PORT", "5432")),
        "user": user,
        "database": name,
        "password": password,
    }


def connect_db_from_env():
    """Open the observer database connection from HTF_P2_DB_* (CP2 hardening).

    SCRAM-SHA-256 is supported natively by pg8000, which is the preferred
    driver; psycopg2 (libpq) is the fallback. The password is passed only to
    the driver and is NEVER logged. Never called implicitly.
    """
    creds = load_db_credentials()
    log.info(
        "observer db connect host=%s port=%s db=%s user=%s (password redacted)",
        creds["host"], creds["port"], creds["database"], creds["user"],
    )
    try:
        import pg8000.dbapi

        return pg8000.dbapi.connect(
            host=creds["host"],
            port=creds["port"],
            user=creds["user"],
            database=creds["database"],
            password=creds["password"],
            timeout=DB_CONNECT_TIMEOUT_S,
        )
    except ImportError:
        import psycopg2

        return psycopg2.connect(
            host=creds["host"],
            port=creds["port"],
            user=creds["user"],
            database=creds["database"],
            password=creds["password"],
            connect_timeout=DB_CONNECT_TIMEOUT_S,
        )


def close_db_connection(conn) -> None:
    """Close a database connection defensively (explicit resource cleanup)."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("db connection close failed: %s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HTF Phase-2 observe-only observer (disabled by default)")
    parser.add_argument("--data-dir", required=True, help="CSV candle source directory")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--checkpoint", default="var/htf_phase2/state.json")
    parser.add_argument("--warmup-bars", type=int, default=400)
    parser.add_argument("--poll-seconds", type=float, default=300.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument(
        "--use-registration",
        action="store_true",
        help="production path: gate startup on the DB registration + freeze sources",
    )
    parser.add_argument(
        "--activation-config",
        default=None,
        help="operator-maintained runtime activation config (JSON); REQUIRED with --use-registration",
    )
    parser.add_argument(
        "--freeze-ts",
        default=None,
        help="explicit test-only freeze (ISO); dry-run only; never used in production",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = RunnerConfig(
        symbols=tuple(s for s in str(args.symbols).split(",") if s),
        checkpoint_path=args.checkpoint,
        warmup_bars=args.warmup_bars,
        poll_seconds=args.poll_seconds,
        once=args.once,
        max_cycles=args.max_cycles,
        dry_run=not args.use_registration,
    )
    writer = InMemoryWriter()
    db_conn = None
    try:
        if args.use_registration:
            # production path: the THREE independent freeze authorities must all
            # agree, otherwise exit fail-closed BEFORE any observation is created
            if not args.activation_config:
                log.error("registration gate: --activation-config is required")
                return EXIT_NOT_RUNNABLE
            try:
                activation = load_activation_config(args.activation_config)
                db_conn = connect_db_from_env()
                boundary = require_runnable(db_conn, config, activation=activation)
            except Exception as exc:
                log.error("registration gate refused startup: %s", exc)
                return EXIT_NOT_RUNNABLE
            from app.research.htf_baseline_short_phase2_persistence_v1 import (
                Phase2ObservationWriter,
            )

            writer = Phase2ObservationWriter(db_conn)
        elif args.freeze_ts:
            fingerprint = config.protocol_fingerprint
            try:
                boundary = build_boundary(
                    experiment_id=config.experiment_id,
                    protocol_freeze_ts=args.freeze_ts,
                    registry_freeze_ts=args.freeze_ts,
                    database_freeze_ts=args.freeze_ts,
                    protocol_fingerprint_hex=fingerprint,
                )
            except NotRunnableError as exc:
                log.error("%s", exc)
                return EXIT_NOT_RUNNABLE
        else:
            log.error("no freeze boundary supplied: fail closed (PHASE_2_NOT_ACTIVATED)")
            return EXIT_NOT_RUNNABLE

        runner = Phase2ObserverRunner(
            config,
            boundary=boundary,
            source=FileCandleSource(args.data_dir),
            writer=writer,
            db_conn=db_conn,
        )
        runner.install_signal_handlers()
        return runner.run_loop()
    finally:
        # F2: close exactly once on EVERY post-connect path (accepted gate,
        # refused gate, exception, early return); never leaks to process exit.
        if db_conn is not None:
            close_db_connection(db_conn)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
