"""Issue #312 / FR-07 — H2 prospective runtime entrypoint (research only).

``python -m app.research.me_entry_quality_h2_prospective_v1.runner``

An INDEPENDENT research process.  It is NOT ``scanner_runner.py`` and shares
no code path with it: no trading signals are generated, no orders are
opened, no paper/live state is touched, no production detector / scoring /
gate is modified and no existing service is stopped or restarted.

DISABLED BY DEFAULT (criterion 17): with ``H2_PROSPECTIVE_ENABLED`` off the
runner exits BEFORE any database connection, any Bybit request and any
write.  The candle source additionally requires a CONFIRMED dedicated
research API budget (``H2_CANDLE_API_BUDGET_CONFIRMED``); without it the
provider stays OFF and the runtime reports ``BLOCKED_CANDLE_SOURCE`` while
source ingest keeps running.

Exit codes (systemd ``SuccessExitStatus=3`` convention):

* ``0`` — graceful stop / successful ``--once`` cycle;
* ``1`` — a ``--once`` cycle FAILED (config was fine; smoke-tests and
  systemd must distinguish PASS from FAIL — review P2-exit);
* ``2`` — invalid configuration (fail closed, never guessed);
* ``3`` — runtime disabled / not activated (operator action).

Systemd unit and env templates ship in ``deploy/`` but are NOT installed or
enabled anywhere (activation requires a separate GO).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any, Callable

from app.research.me_entry_quality_h2_prospective_v1.candle_adapter import (
    BybitClosedCandleProvider,
    CandleApiBudget,
)
from app.research.me_entry_quality_h2_prospective_v1.config import (
    H2ProspectiveConfig,
    H2ProspectiveConfigError,
    load_config,
)
from app.research.me_entry_quality_h2_prospective_v1.runtime import (
    H2ProspectiveRuntime,
)
from app.research.me_entry_quality_h2_prospective_v1.store import (
    H2ProspectiveStore,
    connect_pg_store,
)

H2P_RUNNER_VERSION = "H2P_RUNNER_1.0.0"

logger = logging.getLogger("h2_prospective_runner")

EXIT_OK = 0
EXIT_CYCLE_FAILED = 1
EXIT_CONFIG_ERROR = 2
EXIT_NOT_ACTIVE = 3


def _make_store(config: H2ProspectiveConfig) -> H2ProspectiveStore:
    return connect_pg_store(
        host=config.db_host,
        port=config.db_port,
        database=config.db_name,
        user=config.db_user,
        password=config.db_password,
        unix_sock=config.db_unix_sock or None,
        statement_timeout_ms=config.statement_timeout_ms,
    )


def _make_source_connection(config: H2ProspectiveConfig) -> Any:
    """Dedicated READ-ONLY-usage connection for the #305 source SELECTs."""
    import pg8000

    kwargs: dict[str, Any] = {
        "database": config.db_name,
        "user": config.db_user,
        "password": config.db_password,
        "timeout": 10,
    }
    if config.db_unix_sock:
        kwargs["unix_sock"] = config.db_unix_sock
    else:
        kwargs["host"] = config.db_host
        kwargs["port"] = config.db_port
    conn = pg8000.connect(**kwargs)
    cur = conn.cursor()
    cur.execute(f"SET statement_timeout = {int(config.statement_timeout_ms)}")
    conn.commit()
    return conn


def _make_bybit_client_factory(config: H2ProspectiveConfig) -> Callable[[], Any]:
    """Research-only Bybit client factory (public klines endpoint).

    Deliberately constructed from MINIMAL research settings — never from the
    production scanner configuration — and only when the dedicated research
    API budget is confirmed.
    """
    def factory() -> Any:
        from app.config.settings import Settings
        from app.exchange.bybit_client import BybitClient

        settings = Settings(
            bybit_timeout=config.bybit_timeout_s,
            bybit_rate_limit_rps=1.0,
            # Review P1-budget invariant: ONE pagination page must cost
            # EXACTLY ONE HTTP request.  ``Settings`` defaults would allow
            # up to 5 rate-limit retries per page — pinned to 0 here (and
            # asserted by tests) so the request budget can never be
            # undershot by invisible transport retries.
            bybit_max_attempts=1,
            bybit_rate_limit_max_retries=0,
        )
        return BybitClient(settings)

    return factory


def build_runtime(
    config: H2ProspectiveConfig,
    *,
    store_factory: Callable[[], H2ProspectiveStore] = _make_store,
    source_connection_factory: Callable[[], Any] = _make_source_connection,
    clock: Callable[[], float] = time.time,
) -> tuple[H2ProspectiveRuntime, H2ProspectiveStore]:
    """Assemble the runtime (no connection is opened for the store here)."""
    budget = CandleApiBudget(
        max_requests_per_cycle=config.candle_max_requests_per_cycle,
        max_requests_per_hour=config.candle_max_requests_per_hour,
        max_retries=config.candle_max_retries,
        retry_backoff_s=config.candle_retry_backoff_s,
        request_pause_s=config.candle_request_pause_s,
        page_size=config.candle_page_size,
        max_pages=config.candle_max_pages,
        # Review P1-budget: the research client is built single-attempt
        # (``bybit_max_attempts=1`` above + zero rate-limit retries), so ONE
        # pagination page costs exactly ONE HTTP request.
        requests_per_page=1,
    )
    provider = BybitClosedCandleProvider(
        budget=budget,
        enabled=config.candle_budget_confirmed,
        client_factory=(
            _make_bybit_client_factory(config)
            if config.candle_budget_confirmed
            else None
        ),
    )
    store = store_factory()
    runtime = H2ProspectiveRuntime(
        config=config,
        store=store,
        candle_provider=provider,
        source_connection_factory=source_connection_factory,
        clock=clock,
    )
    return runtime, store


def run_forever(
    config: H2ProspectiveConfig,
    *,
    store_factory: Callable[[], H2ProspectiveStore] = _make_store,
    source_connection_factory: Callable[[], Any] = _make_source_connection,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Bounded polling loop with fail-closed reconnects (research only)."""
    runtime, store = build_runtime(
        config,
        store_factory=store_factory,
        source_connection_factory=source_connection_factory,
    )
    logger.info(
        "H2_PROSPECTIVE_RUNTIME_START version=%s policies=%s",
        H2P_RUNNER_VERSION,
        sorted(runtime.policy_manifest()),
    )
    while True:
        try:
            runtime.run_cycle()
        except KeyboardInterrupt:
            logger.info("H2_PROSPECTIVE_RUNTIME_STOP (interrupt)")
            store.close()
            return EXIT_OK
        except Exception:  # noqa: BLE001 - fail closed, keep the loop alive
            logger.exception("H2_PROSPECTIVE_CYCLE_FAILED")
            if store.broken:
                # Discard the dead dedicated connection and reconnect —
                # never hammer a broken connection (bounded by the poll
                # interval).
                try:
                    store.close()
                except Exception:  # pragma: no cover - defensive
                    pass
                try:
                    store = store_factory()
                    runtime, store = build_runtime(
                        config,
                        store_factory=lambda: store,
                        source_connection_factory=source_connection_factory,
                    )
                except Exception:  # pragma: no cover - defensive
                    logger.exception("H2_PROSPECTIVE_RECONNECT_FAILED")
        sleep(config.poll_interval_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="h2_prospective_runner",
        description=(
            "H2_RSI_ONLY prospective research runtime (research only; "
            "disabled by default)"
        ),
    )
    parser.add_argument(
        "--once", action="store_true", help="run exactly one cycle and exit"
    )
    parser.add_argument(
        "--log-level", default="INFO", help="logging level (default INFO)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        config = load_config()
    except H2ProspectiveConfigError as exc:
        logger.error("H2_PROSPECTIVE_CONFIG_ERROR %s", exc)
        return EXIT_CONFIG_ERROR

    if not config.enabled:
        # Criterion 17: nothing connects, nothing is requested, nothing is
        # written while the runtime is disabled.
        logger.info("H2_PROSPECTIVE_DISABLED (H2_PROSPECTIVE_ENABLED is off)")
        return EXIT_NOT_ACTIVE

    if not config.candle_budget_confirmed:
        logger.warning(
            "H2_CANDLE_API_BUDGET_UNCONFIRMED: candle provider stays OFF; "
            "runtime status BLOCKED_CANDLE_SOURCE (FR-02)"
        )

    if args.once:
        store: H2ProspectiveStore | None = None
        try:
            runtime, store = build_runtime(config)
            report = runtime.run_cycle()
            logger.info("H2_PROSPECTIVE_ONCE_DONE status=%s", report.status)
            return EXIT_OK
        except Exception:  # noqa: BLE001
            # Review P2-exit: a failed cycle must NEVER report success to a
            # smoke-test or systemd — exit non-zero, keep the failure loud.
            logger.exception("H2_PROSPECTIVE_ONCE_FAILED")
            return EXIT_CYCLE_FAILED
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:  # pragma: no cover - defensive
                    logger.exception("H2_PROSPECTIVE_STORE_CLOSE_FAILED")

    return run_forever(config)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
