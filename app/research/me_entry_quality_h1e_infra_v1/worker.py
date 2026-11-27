"""Issue #324 / H1-E — C.4: isolated H1-E observer worker (default OFF).

Explicit, gated entry point that wires the frozen Phase A cycle together
with the Phase C adapters:

``PgSourceReader`` (#305 source, READ-ONLY) -> ``run_capture_cycle`` ->
``PgH1EStore`` (atomic durable ledger) with the H1-C/H1-D-based ASOF
classifier behind the H1-E HTF transport adapter.

Hard rules:

* NOTHING runs at import time — importing this module performs no
  database call and no API call;
* ``main()`` is DISABLED by default: without the explicit deployment
  gate it logs one line and exits 0 WITHOUT touching the database or the
  API;
* separate explicit gates are required for deployment, for the applied
  DDL, for the H1-E freeze and for the activation (all fail closed);
* safe stop (SIGINT/SIGTERM ends the CURRENT cycle first) and
  checkpoint-based resume after restart;
* no backfill path and no OOS transition exist in this worker.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from app.research.me_entry_quality_h1e_v1.capture import (
    CaptureConfig,
    CycleReport,
    run_capture_cycle,
)
from app.research.me_entry_quality_h1e_v1.classification import (
    DEFAULT_RETRY_POLICY,
    RetryPolicy,
    classify_h1e_event,
)
from app.research.me_entry_quality_h1e_v1.contracts import H1EFreeze
from app.research.me_entry_quality_h1e_v1.htf_transport import (
    MappedH1EHtfTransport,
)

H1E_WORKER_VERSION = "H1E_WORKER_V1"


class WorkerGateError(RuntimeError):
    """A required gate is missing — fail closed, nothing is started."""


@dataclass(frozen=True)
class WorkerConfig:
    """Explicit configuration of ONE isolated H1-E observer worker."""

    pg_dsn: str
    activation_id: str
    freeze_ts_us: int
    protocol_fingerprint: str
    approval_ref: str
    approved_by: str
    worker_id: str = "h1e-observer-v1"
    batch_limit: int = 200
    retry_batch_limit: int = 50
    max_attempts: int = 3
    retry_backoff_us: int = 0
    poll_interval_s: float = 60.0
    max_cycles: int | None = None
    retry_policy: RetryPolicy = field(default=DEFAULT_RETRY_POLICY)

    def capture_config(self) -> CaptureConfig:
        return CaptureConfig(
            batch_limit=int(self.batch_limit),
            retry_batch_limit=int(self.retry_batch_limit),
            max_attempts=int(self.max_attempts),
            retry_backoff_us=int(self.retry_backoff_us),
            allow_fixture_freeze=False,
        )

    def freeze(self) -> H1EFreeze:
        return H1EFreeze.from_owner_approval(
            freeze_ts_us=int(self.freeze_ts_us),
            approval_ref=self.approval_ref,
            approved_by=self.approved_by,
        )


def load_config_from_env(env: Mapping[str, str]) -> WorkerConfig:
    """Fail-closed configuration load (every gate is explicit)."""

    def require(name: str) -> str:
        value = str(env.get(name, "")).strip()
        if not value:
            raise WorkerGateError(f"missing required gate/config {name}")
        return value

    if require("H1E_WORKER_ENABLE") != "1":
        raise WorkerGateError("H1E_WORKER_ENABLE must be exactly '1'")
    if require("H1E_DEPLOY_GATE") != "1":
        raise WorkerGateError("H1E_DEPLOY_GATE must be exactly '1'")
    if require("H1E_DDL_APPLIED") != "1":
        raise WorkerGateError(
            "H1E_DDL_APPLIED must be exactly '1' (DDL applied by owner GO)")
    try:
        freeze_ts_us = int(require("H1E_FREEZE_TS_US"))
    except ValueError as exc:
        raise WorkerGateError("H1E_FREEZE_TS_US must be integer µs") from exc

    def optional_int(name: str, default: int) -> int:
        raw = str(env.get(name, "")).strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise WorkerGateError(f"{name} must be an integer") from exc

    return WorkerConfig(
        pg_dsn=require("H1E_PG_DSN"),
        activation_id=require("H1E_ACTIVATION_ID"),
        freeze_ts_us=freeze_ts_us,
        protocol_fingerprint=require("H1E_PROTOCOL_FINGERPRINT"),
        approval_ref=require("H1E_APPROVAL_REF"),
        approved_by=require("H1E_APPROVED_BY"),
        worker_id=str(env.get("H1E_WORKER_ID", "h1e-observer-v1")),
        batch_limit=optional_int("H1E_BATCH_LIMIT", 200),
        retry_batch_limit=optional_int("H1E_RETRY_BATCH_LIMIT", 50),
        max_attempts=optional_int("H1E_MAX_ATTEMPTS", 3),
        retry_backoff_us=optional_int("H1E_RETRY_BACKOFF_US", 0),
        poll_interval_s=float(str(env.get("H1E_POLL_INTERVAL_S", "60")).strip() or "60"),
    )


def build_classifier(
    transport: Any,
    *,
    retry_policy: RetryPolicy | None = None,
) -> Callable[..., Any]:
    policy = retry_policy or DEFAULT_RETRY_POLICY

    def classifier(event: Any, prior_deferrals: int = 0) -> Any:
        return classify_h1e_event(
            event, transport,
            retry_policy=policy, prior_deferrals=prior_deferrals)

    return classifier


class H1EObserverWorker:
    """One isolated worker: bounded capture cycles over durable state."""

    def __init__(
        self,
        config: WorkerConfig,
        *,
        reader: Any,
        store: Any,
        classifier: Callable[..., Any],
    ) -> None:
        self.config = config
        self.reader = reader
        self.store = store
        self.classifier = classifier
        self.freeze = config.freeze()
        self._capture_config = config.capture_config()

    def run_once(self) -> CycleReport:
        cursor = self.store.checkpoint
        return run_capture_cycle(
            reader=self.reader,
            store=self.store,
            classifier=self.classifier,
            freeze=self.freeze,
            cursor=cursor,
            config=self._capture_config,
        )

    def run(self, stop_event: threading.Event) -> list[CycleReport]:
        reports: list[CycleReport] = []
        while not stop_event.is_set():
            reports.append(self.run_once())
            if self.config.max_cycles is not None:
                if len(reports) >= int(self.config.max_cycles):
                    break
            if stop_event.wait(timeout=float(self.config.poll_interval_s)):
                break
        return reports


def _default_connection_factory(dsn: str) -> Callable[[], Any]:
    def factory() -> Any:
        import psycopg2  # lazy: no driver import at module import time

        return psycopg2.connect(dsn)

    return factory


def _default_client_factory() -> Any:
    # lazy: the shared Bybit client (limiter / retry budget / breaker)
    # is imported only when the worker is actually started
    from app.config import Settings
    from app.exchange.bybit_client import BybitClient

    return BybitClient(Settings())


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    connection_factory: Callable[[str], Callable[[], Any]] | None = None,
    client_factory: Callable[[], Any] | None = None,
    reader_factory: Callable[[Callable[[], Any]], Any] | None = None,
    store_factory: Callable[..., Any] | None = None,
    transport_factory: Callable[..., Any] | None = None,
) -> int:
    """Explicit entry point — DISABLED without its gates (fail closed)."""
    environ = os.environ if env is None else env
    arguments = list(sys.argv[1:] if argv is None else argv)

    if str(environ.get("H1E_WORKER_ENABLE", "")).strip() != "1":
        # default OFF (repo convention: exit 3 = disabled / not activated):
        # NO database call, NO API call
        print("h1e observer worker DISABLED (H1E_WORKER_ENABLE != 1)")
        return 3

    try:
        config = load_config_from_env(environ)
    except WorkerGateError as exc:
        print(f"h1e observer worker gate failed: {exc}", file=sys.stderr)
        return 2

    if arguments and arguments[0] == "--health":
        return _healthcheck(config, connection_factory)

    from app.research.me_entry_quality_h1e_infra_v1.pg_ledger_store import (
        PgH1EStore,
    )
    from app.research.me_entry_quality_h1e_infra_v1.pg_source_reader import (
        PgSourceReader,
    )

    connection_factory = connection_factory or _default_connection_factory
    client_factory = client_factory or _default_client_factory
    reader_factory = reader_factory or PgSourceReader
    store_factory = store_factory or PgH1EStore
    transport_factory = transport_factory or MappedH1EHtfTransport

    open_connection = connection_factory(config.pg_dsn)
    reader = reader_factory(open_connection)
    store = store_factory(
        open_connection,
        activation=config.freeze(),
        worker_id=config.worker_id,
    )
    transport = transport_factory(client_factory())
    classifier = build_classifier(transport, retry_policy=config.retry_policy)
    worker = H1EObserverWorker(
        config, reader=reader, store=store, classifier=classifier)

    stop_event = threading.Event()

    def _stop(_signum: int, _frame: Any) -> None:
        # safe stop: the CURRENT cycle finishes, then the worker exits
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    worker.run(stop_event)
    return 0


def _healthcheck(
    config: WorkerConfig,
    connection_factory: Callable[[str], Callable[[], Any]] | None,
) -> int:
    """READ-ONLY health probe (no writes, no API)."""
    connection_factory = connection_factory or _default_connection_factory
    open_connection = connection_factory(config.pg_dsn)
    try:
        connection = open_connection()
        try:
            with connection.cursor() as statement:
                statement.execute("SELECT 1")
                statement.fetchone()
                statement.execute(
                    "SELECT status FROM "
                    "research_me_entry_quality_h1e.h1e_activation "
                    "WHERE experiment_id = %s",
                    (config.activation_id,),
                )
                row = statement.fetchone()
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
    except Exception as exc:  # noqa: BLE001 — health reports, never crashes
        print(f"h1e health FAILED: {exc}", file=sys.stderr)
        return 1
    if row is None or str(row[0]) not in ("APPROVED", "RUNNING"):
        print("h1e health FAILED: activation not approved/running", file=sys.stderr)
        return 1
    print("h1e health OK")
    return 0
