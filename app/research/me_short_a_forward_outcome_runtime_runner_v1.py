"""Entrypoint for ``ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1``.

A real, independent, strictly forward-only **observe-only** runtime process.
It is started by the systemd unit ``trad-bot-me-short-a-forward-outcome`` and
does exactly four things per cycle, all through the collector API of
:mod:`app.research.me_short_a_forward_outcome_lineage_v1`:

``capture_source_signal`` -> ``record_event_lineage`` -> ``capture_post_signal_candle``
-> ``finalize_outcome``

plus one operational ``heartbeat`` row.

It never places an order, never routes to paper/live, never calls scanner code
and never writes to Phase 1 or the closed frozen OOS cohort.

Exit-code contract (mirrors the HTF observer runner):

* ``0``  EXIT_OK — clean stop or ``--max-cycles`` reached;
* ``3``  EXIT_NOT_RUNNABLE — fail-closed gate refusal (capture disabled,
  activation not ``RUNNING``, missing ``freeze_ts``, missing credentials).
  This is the EXPECTED outcome before the explicit activation checkpoint and
  must NOT be retried into a restart loop;
* ``4``  EXIT_STATE_ERROR — configuration/registry identity could not be
  verified;
* ``5``  EXIT_WRITER_ERROR — the runtime entered the FAILED state.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

from app.research.me_short_a_forward_outcome_runtime_v1 import (
    FEATURE_FLAG,
    PROTOCOL_VERSION,
    RUNNER_STATE_FAILED,
    ForwardOutcomeRuntime,
    resolve_config,
    runtime_protocol_hash,
    utcnow,
    verify_runtime_identity,
)

__all__ = [
    "EXIT_OK",
    "EXIT_NOT_RUNNABLE",
    "EXIT_STATE_ERROR",
    "EXIT_WRITER_ERROR",
    "main",
    "build_runtime",
    "connect",
    "connect_readonly",
]

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_NOT_RUNNABLE = 3
EXIT_STATE_ERROR = 4
EXIT_WRITER_ERROR = 5

DB_ENV_KEYS = (
    "ME_SHORT_A_RUNTIME_DB_HOST",
    "ME_SHORT_A_RUNTIME_DB_PORT",
    "ME_SHORT_A_RUNTIME_DB_NAME",
    "ME_SHORT_A_RUNTIME_DB_USER",
    "ME_SHORT_A_RUNTIME_DB_PASSWORD",
    "ME_SHORT_A_RUNTIME_DB_PASSWORD_FILE",
)


# ── database connection (dedicated least-privilege login) ─────────────

def _password_from_env(env: dict[str, str]) -> str:
    direct = env.get("ME_SHORT_A_RUNTIME_DB_PASSWORD")
    if direct:
        return direct
    path = env.get("ME_SHORT_A_RUNTIME_DB_PASSWORD_FILE")
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    return ""


def connect(env: dict[str, str] | None = None) -> Any:
    """Open a connection as the dedicated ``trad_bot_me_short_a_runtime`` role.

    The shared application ``.env`` is intentionally NOT loaded here: the
    runtime must never reuse scanner / paper / live credentials.
    """
    source = os.environ if env is None else env
    host = source.get("ME_SHORT_A_RUNTIME_DB_HOST")
    name = source.get("ME_SHORT_A_RUNTIME_DB_NAME")
    user = source.get("ME_SHORT_A_RUNTIME_DB_USER")
    if not host or not name or not user:
        raise RuntimeError(
            "ME_SHORT_A_RUNTIME_DB_HOST/NAME/USER are required; the runtime "
            "refuses to fall back to application credentials")
    port = int(source.get("ME_SHORT_A_RUNTIME_DB_PORT") or 5432)
    password = _password_from_env(source)

    try:
        import psycopg  # type: ignore

        return psycopg.connect(
            host=host, port=port, dbname=name, user=user, password=password,
            autocommit=False)
    except ImportError:
        pass

    import pg8000.dbapi  # type: ignore

    return pg8000.dbapi.connect(
        host=host, port=port, database=name, user=user, password=password)


def _close_quietly(conn: Any) -> None:
    """Close a connection without letting a driver error mask the real one."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception:  # pragma: no cover - defensive
        pass


def connect_readonly(env: dict[str, str] | None = None) -> Any:
    """Open a dedicated **READ ONLY** connection for the candidate intake.

    The intake reads exactly ``dds.scanner_setup`` and ``dds.instrument`` and
    must never write anywhere. The session is switched to
    ``default_transaction_read_only = on``, so PostgreSQL opens every
    transaction on it as a READ ONLY transaction and rejects any write with
    ``cannot execute INSERT in a read-only transaction``.

    This is deliberately a *separate connection* from the one used for the
    append-only capture writes: the write path needs ``default_transaction_read_only
    = off``, and mixing the two on one session would weaken both guarantees.
    """
    conn = connect(env)
    cursor_obj = conn.cursor()
    try:
        cursor_obj.execute("SET default_transaction_read_only = on")
        conn.commit()
    finally:
        cursor_obj.close()
    return conn


# ── runtime assembly ──────────────────────────────────────────────────

def build_runtime(
    conn: Any,
    args: argparse.Namespace,
    read_conn: Any = None,
) -> ForwardOutcomeRuntime:
    from app.research.me_short_a_forward_outcome_runtime_sources_v1 import (
        BybitClosedCandleSource,
        CompositeCandidateSource,
        DbScannerSetupCandidateSource,
        DbSourceIntakeCandidateSource,
        assert_read_only_session,
    )

    config = resolve_config(registry_path=args.registry)

    ok, problems = verify_runtime_identity(
        protocol_version=config.protocol_version,
        protocol_hash=config.protocol_hash)
    if not ok:
        raise RuntimeError(
            "runtime registry identity mismatch: " + "; ".join(problems))

    if args.candle_source == "bybit":
        from app.config import Settings
        from app.exchange.bybit_client import BybitClient

        client = BybitClient(Settings())
        candle_source = BybitClosedCandleSource(client)
    else:  # pragma: no cover - offline replay path
        raise RuntimeError(
            "--candle-source csv is intentionally not wired: without a real "
            "closed-candle source the runtime fails closed instead of "
            "replaying stale data")

    # The candidate intake runs on the READ ONLY connection. It never sees
    # the write connection, and it refuses to run unless the session really is
    # a PostgreSQL READ ONLY session.
    #
    # Issue #208 corrective: candidates are observed through TWO read-only
    # transports and merged losslessly by the composite:
    #   * dds.scanner_setup          — what survived the trading filters;
    #   * research source intake     — the ORIGINAL MOMENTUM_EXHAUSTION SHORT
    #     events captured before/independent of the trading regime filter
    #     (the class the confirmed TSLAUSDT incident belongs to).
    # The composite raises the freeze bound to the intake activation floor so
    # both transports observe the same strictly-forward-only cohort.
    intake_conn = conn if read_conn is None else read_conn
    candidate_source = CompositeCandidateSource((
        DbScannerSetupCandidateSource(
            intake_conn, candle_source, max_rows=args.max_rows),
        DbSourceIntakeCandidateSource(
            intake_conn, candle_source, max_rows=args.max_rows),
    ))
    if getattr(candidate_source, "requires_read_only_session", False):
        assert_read_only_session(intake_conn)

    return ForwardOutcomeRuntime(
        conn,
        config=config,
        candle_source=candle_source,
        candidate_source=candidate_source,
        clock=utcnow,
        runner_id=args.runner_id or f"me-short-a-forward-runtime-{os.getpid()}",
        generation=args.generation,
        sleep=time.sleep,
    )


def _install_signal_handlers(runtime: ForwardOutcomeRuntime) -> None:
    def _handler(signum, _frame):  # pragma: no cover - signal path
        log.info("ME SHORT A forward runtime received signal %s", signum)
        runtime.request_stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # pragma: no cover
            pass


# ── CLI ───────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="me_short_a_forward_outcome_runtime_runner_v1",
        description=(
            "Independent observe-only forward outcome runtime for "
            "MOMENTUM_EXHAUSTION SHORT (ME SHORT A). Never trades."))
    parser.add_argument("--registry", default=None,
                        help="path to the frozen runtime registry JSON")
    parser.add_argument("--runner-id", default=None)
    parser.add_argument("--generation", type=int, default=0)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--max-rows", type=int, default=500)
    parser.add_argument("--candle-source", default="bybit",
                        choices=("bybit",))
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    log.info("ME SHORT A forward outcome runtime starting: protocol=%s",
             PROTOCOL_VERSION)

    try:
        config = resolve_config(registry_path=args.registry)
    except Exception as exc:
        log.error("runtime registry could not be loaded: %s", exc)
        return EXIT_STATE_ERROR

    if config.protocol_hash != runtime_protocol_hash():
        log.error("runtime protocol hash mismatch; refusing to start")
        return EXIT_STATE_ERROR

    # Fail-closed gate: without the feature flag nothing is attempted at all.
    if not config.enabled:
        log.warning(
            "%s is not '1' — runtime is fail-closed and records nothing",
            FEATURE_FLAG)
        return EXIT_NOT_RUNNABLE

    try:
        conn = connect()
    except Exception as exc:
        log.error("dedicated runtime DB credentials are not usable: %s", exc)
        return EXIT_NOT_RUNNABLE

    read_conn = None
    try:
        read_conn = connect_readonly()
    except Exception as exc:
        log.error("READ ONLY intake connection could not be opened: %s", exc)
        _close_quietly(conn)
        return EXIT_NOT_RUNNABLE

    try:
        runtime = build_runtime(conn, args, read_conn=read_conn)
    except Exception as exc:
        log.error("runtime could not be assembled: %s", exc)
        _close_quietly(read_conn)
        _close_quietly(conn)
        return EXIT_STATE_ERROR

    _install_signal_handlers(runtime)

    from app.research.me_short_a_forward_outcome_runtime_v1 import (
        replace_activation,
    )

    status, freeze = runtime._activation_snapshot()
    runtime._config = replace_activation(runtime.config, status, freeze)
    if not runtime.config.activated:
        reason = runtime.config.fail_closed_reason
        log.warning("runtime is not activated (%s); nothing is captured", reason)
        # Still emit a heartbeat so operators can see the blocked state.
        runtime.metrics.add("skipped_not_activated")
        runtime._transition("RUNNING", "BLOCKED_NOT_ACTIVATED")
        runtime.heartbeat()
        return EXIT_NOT_RUNNABLE

    log.info("runtime activated: freeze_ts=%s", runtime.config.freeze_ts)

    interval = args.poll_seconds
    runtime.start()
    cycles = 0
    try:
        while True:
            runtime.run_cycle()
            cycles += 1
            if args.max_cycles is not None and cycles >= args.max_cycles:
                break
            if runtime.state == RUNNER_STATE_FAILED:
                return EXIT_WRITER_ERROR
            if runtime.state == "STOPPING":
                break
            time.sleep(interval)
    finally:
        if runtime.state != RUNNER_STATE_FAILED:
            runtime.stop("SIGTERM" if runtime.state == "STOPPING" else "RUN_COMPLETED")
            runtime.heartbeat()
        _close_quietly(read_conn)
        _close_quietly(conn)

    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
