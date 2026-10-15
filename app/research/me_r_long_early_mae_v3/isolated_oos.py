"""Issue #154: strictly isolated, opt-in prospective market-data + V3 cycle.

Never imports a trade execution engine and never connects to the production DB.
Does not backfill missing OOS candles after downtime: incompleteness remains visible.
The existing #152 isolated test runner is deliberately untouched.
"""
from __future__ import annotations

import argparse
import os
import pwd
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from .detector import detect_v1_closed
from .guards import require_runtime_configuration
from .pipeline import MeEarlyMaeV3Pipeline
from .protocol import ACTIVATION_AT, parse_utc, require_protocol_fingerprint

DATABASE = "trad_bot_me_r_v3_oos"
ROLE = "trad_bot_me_r_v3_oos"
SOCKET = "/var/run/postgresql/.s.PGSQL.5432"
INTERVALS = (("candles_5m", "5", 300), ("candles_15m", "15", 900), ("candles_1h", "60", 3600))


def validate_identity() -> None:
    """Refuse execution as root, tradbot, or the #152 integration user."""
    expected = pwd.getpwnam(ROLE).pw_uid
    if os.getuid() != expected or os.geteuid() != expected:
        raise RuntimeError("ME_R_V3_ISOLATED_OS_USER_REQUIRED")


def connect():
    import pg8000.dbapi

    connection = pg8000.dbapi.connect(user=ROLE, database=DATABASE, unix_sock=SOCKET)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT current_database(), current_user, session_user, inet_client_addr() IS NULL")
        if tuple(cursor.fetchone()) != (DATABASE, ROLE, ROLE, True):
            raise RuntimeError("ME_R_V3_ISOLATED_DB_IDENTITY_MISMATCH")
        cursor.execute("SELECT to_regclass('market.candle'), to_regclass('dds.instrument')")
        if any(item is None for item in cursor.fetchone()):
            raise RuntimeError("ME_R_V3_LOCAL_MARKET_SCHEMA_MISSING")
        connection.rollback()
        return connection
    except BaseException:
        connection.close()
        raise


def configured_symbols() -> tuple[str, ...]:
    raw = os.environ.get("ME_R_V3_OOS_SYMBOLS", "")
    symbols = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not symbols or len(symbols) > 50 or len(set(symbols)) != len(symbols):
        raise RuntimeError("ME_R_V3_EXPLICIT_SYMBOL_UNIVERSE_REQUIRED")
    if any(re.fullmatch(r"[A-Z0-9]{3,32}", symbol) is None for symbol in symbols):
        raise RuntimeError("ME_R_V3_INVALID_SYMBOL")
    return symbols


def completed_only(candles, seconds: int, asof: datetime):
    """Exclude exchange-reported forming candles before detector validation."""
    selected = []
    for candle in candles:
        timestamp_ms = int(candle.timestamp)
        if timestamp_ms < 10_000_000_000:
            timestamp_ms *= 1000
        opened = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
        if opened + timedelta(seconds=seconds) <= asof:
            selected.append(candle)
    return tuple(selected)


def collect_context(client, symbol: str):
    """Timestamp the complete API fetch, including all three timeframes."""
    started = datetime.now(timezone.utc)
    data = {}
    for name, interval, seconds in INTERVALS:
        raw = client.get_klines(symbol, interval, 200)
        fetched = datetime.now(timezone.utc)
        data[name] = completed_only(raw, seconds, fetched)
    completed = datetime.now(timezone.utc)
    context = SimpleNamespace(symbol=symbol, source_fetch_started_at=started, **data)
    return context, completed


def store_latest_5m(connection, symbol: str, candle, observed_at: datetime) -> bool:
    """Write only a *newly available* completed candle; never backfill a gap."""
    opened_ms = int(candle.timestamp)
    if opened_ms < 10_000_000_000:
        opened_ms *= 1000
    opened = datetime.fromtimestamp(opened_ms / 1000, timezone.utc)
    closed = opened + timedelta(minutes=5)
    activation = parse_utc(ACTIVATION_AT)
    lag = (observed_at - closed).total_seconds()
    if closed <= activation or lag < 0 or lag > 300:
        return False
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO dds.instrument(symbol) VALUES (%s) "
            "ON CONFLICT (symbol) DO NOTHING",
            (symbol,),
        )
        cursor.execute("SELECT instrument_id FROM dds.instrument WHERE symbol=%s", (symbol,))
        instrument_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO market.candle (
               instrument_id, timeframe, open_time, close_time, open, high, low,
               close, volume, source_received_at
            ) VALUES (%s,'5',%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (exchange, market_type, instrument_id, timeframe, open_time)
            DO NOTHING""",
            (
                instrument_id, opened, closed, candle.open, candle.high,
                candle.low, candle.close, getattr(candle, "volume", 0.0),
                observed_at,
            ),
        )
        inserted = cursor.rowcount == 1
        connection.commit()
        return inserted
    except BaseException:
        connection.rollback()
        raise


def once(client=None):
    validate_identity()
    require_runtime_configuration(datetime.now(timezone.utc))
    require_protocol_fingerprint()
    symbols = configured_symbols()

    if client is None:
        from app.config import Settings
        from app.exchange.bybit_client import BybitClient
        client = BybitClient(Settings())

    connection = connect()
    try:
        pipeline = MeEarlyMaeV3Pipeline(connection)
        pipeline.initialize()
        stats = {"symbols": 0, "signals": 0, "new_candles": 0, "rejected": 0}
        for symbol in symbols:
            # Fail closed for this symbol; no synthesized or late signals.
            context, completed = collect_context(client, symbol)
            latest = context.candles_5m[-1] if context.candles_5m else None
            if latest is None:
                stats["rejected"] += 1
                continue
            inserted = store_latest_5m(connection, symbol, latest, completed)
            stats["new_candles"] += int(inserted)
            # Capture only on a newly observed, naturally closed 5m candle.
            if not inserted:
                continue
            detection = detect_v1_closed(context, completed, completed)
            if detection.status == "REJECTED":
                stats["rejected"] += 1
            elif detection.status == "SIGNAL":
                pipeline.capture_source(detection.source)
                stats["signals"] += 1
            stats["symbols"] += 1
        evaluation = pipeline.evaluate_cycle()
        if evaluation.get("errors", 0):
            raise RuntimeError("ME_R_V3_EVALUATOR_CYCLE_ERRORS")
        return {**stats, **{f"eval_{k}": v for k, v in evaluation.items()}}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description="Isolated ME_R V3 OOS collector")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.once:
        parser.error("explicit --once required; deployment and activation are separate")
    print(once())


if __name__ == "__main__":
    main()
