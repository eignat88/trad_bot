from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Mapping
from uuid import UUID

from app.scanners.models import SetupCandidate, SetupState
from app.scanners.outcome import SignalOutcome

logger = logging.getLogger(__name__)


class ScannerRepository:
    """Dual-mode repository: PostgreSQL (pg8000) when available, JSONL fallback."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        database: str = "trad_bot",
        user: str = "postgres",
        jsonl_path: str = "data/scanner_setups.jsonl",
        backend: Literal["auto", "postgres", "jsonl"] = "auto",
    ) -> None:
        if backend not in {"auto", "postgres", "jsonl"}:
            raise ValueError("backend must be 'auto', 'postgres', or 'jsonl'")
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._jsonl_path = jsonl_path
        self._conn: Any = None
        self._use_pg = False

        # JSONL is also a supported explicit backend, not merely an emergency
        # fallback.  In particular, callers using an isolated JSONL file must
        # not silently read from a locally running PostgreSQL instance.
        if backend == "jsonl":
            logger.info("scanner repository using JSONL (%s)", jsonl_path)
            return

        try:
            import pg8000
            self._conn = pg8000.connect(
                host=host, port=port, database=database, user=user
            )
            self._use_pg = True
            logger.info("scanner repository connected to PostgreSQL (%s:%d/%s)", host, port, database)
        except ImportError:
            if backend == "postgres":
                raise
            logger.warning("pg8000 not installed, using JSONL fallback")
        except Exception:
            if backend == "postgres":
                raise
            logger.warning("PostgreSQL connection failed, using JSONL fallback")

    def ensure_schema(self) -> None:
        if not self._use_pg:
            return
        from pathlib import Path

        schema_path = Path(__file__).parent / "schema.sql"
        if schema_path.exists():
            sql = schema_path.read_text(encoding="utf-8")
            cursor = self._conn.cursor()
            try:
                for stmt in sql.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        cursor.execute(stmt)
                self._conn.commit()
                logger.info("scanner schema applied")
            except Exception as e:
                logger.warning("schema apply error (may already exist): %s", e)

    # ----------------------------------------------------------------
    # INSTRUMENT
    # ----------------------------------------------------------------
    def ensure_instrument(self, symbol: str, base_asset: str = "", quote_asset: str = "USDT") -> int | None:
        if not self._use_pg:
            return None

        base = base_asset or symbol.replace(quote_asset, "")
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                "SELECT instrument_id FROM dds.instrument WHERE symbol = %s",
                (symbol,),
            )
            row = cursor.fetchone()
            if row:
                return row[0]

            cursor.execute(
                "INSERT INTO dds.instrument (symbol, base_asset, quote_asset) "
                "VALUES (%s, %s, %s) RETURNING instrument_id",
                (symbol, base, quote_asset),
            )
            self._conn.commit()
            return cursor.fetchone()[0]
        except Exception:
            self._conn.rollback()
            raise

    # ----------------------------------------------------------------
    # SCANNER RUN
    # ----------------------------------------------------------------
    def start_run(self, symbols_total: int, universe_mode: str) -> int | None:
        if not self._use_pg:
            return None
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO dds.scanner_run (started_at, universe_mode, symbols_total, status) "
                "VALUES (now(), %s, %s, 'RUNNING') RETURNING run_id",
                (universe_mode, symbols_total),
            )
            self._conn.commit()
            return cursor.fetchone()[0]
        except Exception:
            self._conn.rollback()
            raise

    def save_run_universe(
        self,
        run_id: int | None,
        instruments: list[str | Mapping[str, Any]],
    ) -> None:
        """Persist the ranked symbol snapshot used by a scanner run."""
        if not self._use_pg or run_id is None:
            return
        cursor = self._conn.cursor()
        try:
            for position, item in enumerate(instruments, start=1):
                if isinstance(item, str):
                    symbol = item
                    rank = position
                    turnover_24h = None
                    volume_24h = None
                else:
                    symbol = str(item["symbol"])
                    rank = int(item.get("rank", position))
                    turnover_24h = item.get("turnover_24h")
                    volume_24h = item.get("volume_24h")
                instrument_id = self.ensure_instrument(symbol)
                cursor.execute(
                    """
                    INSERT INTO dds.scanner_run_instrument (
                        run_id, instrument_id, universe_rank,
                        turnover_24h, volume_24h
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, instrument_id) DO UPDATE SET
                        universe_rank = EXCLUDED.universe_rank,
                        turnover_24h = EXCLUDED.turnover_24h,
                        volume_24h = EXCLUDED.volume_24h
                    """,
                    (run_id, instrument_id, rank, turnover_24h, volume_24h),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def acquire_runner_lock(self, lock_id: int = 1_937_261) -> bool:
        """Hold a session advisory lock for the lifetime of this repository."""
        if not self._use_pg:
            return True
        cursor = self._conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
        return bool(cursor.fetchone()[0])

    def abort_stale_runs(self, stale_minutes: int = 10) -> int:
        if not self._use_pg:
            return 0
        cursor = self._conn.cursor()
        cursor.execute(
            """
            UPDATE dds.scanner_run SET status = 'ABORTED', finished_at = now(),
                duration_sec = EXTRACT(EPOCH FROM (now() - started_at))
            WHERE status = 'RUNNING'
              AND started_at < now() - (%s * interval '1 minute')
            """,
            (stale_minutes,),
        )
        count = cursor.rowcount
        self._conn.commit()
        return count

    def save_run_stat(
        self, run_id: int | None, scanner_name: str, **values: int | float,
    ) -> None:
        if not self._use_pg or run_id is None:
            return
        cursor = self._conn.cursor()
        cursor.execute(
            """
            INSERT INTO dds.scanner_run_stat (
                run_id, scanner_name, symbols_scanned, candidates_found,
                setups_saved, errors_count, duration_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, scanner_name) DO UPDATE SET
                symbols_scanned = EXCLUDED.symbols_scanned,
                candidates_found = EXCLUDED.candidates_found,
                setups_saved = EXCLUDED.setups_saved,
                errors_count = EXCLUDED.errors_count,
                duration_ms = EXCLUDED.duration_ms
            """,
            (run_id, scanner_name, values["symbols_scanned"],
             values["candidates_found"], values["setups_saved"],
             values["errors_count"], values["duration_ms"]),
        )
        self._conn.commit()

    def finish_run(
        self,
        run_id: int,
        *,
        symbols_scanned: int = 0,
        symbols_failed: int = 0,
        setups_found: int = 0,
        error_count: int = 0,
        status: str = "COMPLETED",
    ) -> None:
        if not self._use_pg or run_id is None:
            return
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                UPDATE dds.scanner_run SET
                    finished_at = now(),
                    symbols_scanned = %s,
                    symbols_failed = %s,
                    setups_found = %s,
                    error_count = %s,
                    duration_sec = EXTRACT(EPOCH FROM (now() - started_at)),
                    status = %s
                WHERE run_id = %s
                """,
                (symbols_scanned, symbols_failed, setups_found, error_count, status, run_id),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def get_run_history(self, limit: int = 50) -> list[dict]:
        if not self._use_pg:
            return []
        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT run_id, started_at, finished_at, symbols_scanned, symbols_total, "
            "setups_found, error_count, duration_sec, status "
            "FROM dds.scanner_run ORDER BY started_at DESC LIMIT %s",
            (limit,),
        )
        return [
            {
                "run_id": r[0], "started_at": r[1].isoformat() if r[1] else None,
                "finished_at": r[2].isoformat() if r[2] else None,
                "symbols_scanned": r[3], "symbols_total": r[4],
                "setups_found": r[5], "error_count": r[6],
                "duration_sec": float(r[7]) if r[7] else None, "status": r[8],
            }
            for r in cursor.fetchall()
        ]

    # ----------------------------------------------------------------
    # SCANNER SETUP
    # ----------------------------------------------------------------
    def save_setup(self, candidate: SetupCandidate, run_id: int | None = None) -> None:
        if not candidate.signal_candle_open_time:
            raise ValueError("signal_candle_open_time must be a non-zero candle timestamp")
        if self._use_pg:
            self._save_pg(candidate, run_id)
        else:
            self._save_jsonl(candidate)

    def _save_pg(self, c: SetupCandidate, run_id: int | None = None) -> None:
        instrument_id = self.ensure_instrument(c.symbol)
        if instrument_id is None:
            return

        status = "READY_TO_TRADE" if (c.state.value == "SETUP_READY" if isinstance(c.state, SetupState) else c.state == "SETUP_READY") else (c.state.value if isinstance(c.state, SetupState) else c.state)

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO dds.scanner_setup (
                    setup_id, scanner_name, scanner_version, instrument_id, run_id,
                    direction, htf_timeframe, setup_timeframe, entry_timeframe,
                    setup_started_at, signal_candle_open_time, detected_at, reference_price,
                    entry_zone_low, entry_zone_high, invalidation_price,
                    target_1, target_2, score, market_regime, status,
                    reasons, features
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (
                    instrument_id, scanner_name, direction, entry_timeframe,
                    signal_candle_open_time
                ) WHERE signal_candle_open_time > 0 DO UPDATE SET
                    run_id = EXCLUDED.run_id,
                    detected_at = EXCLUDED.detected_at,
                    reference_price = EXCLUDED.reference_price,
                    entry_zone_low = EXCLUDED.entry_zone_low,
                    entry_zone_high = EXCLUDED.entry_zone_high,
                    invalidation_price = EXCLUDED.invalidation_price,
                    target_1 = EXCLUDED.target_1,
                    target_2 = EXCLUDED.target_2,
                    score = EXCLUDED.score,
                    market_regime = EXCLUDED.market_regime,
                    status = EXCLUDED.status,
                    reasons = EXCLUDED.reasons,
                    features = EXCLUDED.features,
                    updated_at = now()
                """,
                (
                    str(c.setup_id), c.scanner_name, c.scanner_version, instrument_id, run_id,
                    c.direction, c.htf_timeframe, c.setup_timeframe, c.entry_timeframe,
                    c.setup_started_at, c.signal_candle_open_time, c.detected_at, c.reference_price,
                    c.entry_zone_low, c.entry_zone_high, c.invalidation_price,
                    c.target_1, c.target_2, c.score, c.market_regime, status,
                    json.dumps(list(c.reasons)), json.dumps(c.features),
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _save_jsonl(self, c: SetupCandidate) -> None:
        from pathlib import Path
        Path(self._jsonl_path).parent.mkdir(parents=True, exist_ok=True)
        record = {
            "setup_id": str(c.setup_id),
            "scanner_name": c.scanner_name,
            "scanner_version": c.scanner_version,
            "symbol": c.symbol,
            "direction": c.direction,
            "htf_timeframe": c.htf_timeframe,
            "setup_timeframe": c.setup_timeframe,
            "entry_timeframe": c.entry_timeframe,
            "setup_started_at": c.setup_started_at.isoformat(),
            "signal_candle_open_time": c.signal_candle_open_time,
            "detected_at": c.detected_at.isoformat(),
            "reference_price": c.reference_price,
            "entry_zone_low": c.entry_zone_low,
            "entry_zone_high": c.entry_zone_high,
            "invalidation_price": c.invalidation_price,
            "target_1": c.target_1,
            "target_2": c.target_2,
            "score": c.score,
            "market_regime": c.market_regime,
            "status": c.state.value if isinstance(c.state, SetupState) else c.state,
            "reasons": list(c.reasons),
            "features": c.features,
        }
        path = Path(self._jsonl_path)
        records = []
        if path.exists():
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        identity = self._signal_identity(record)
        for index, existing in enumerate(records):
            if self._signal_identity(existing) == identity:
                record["setup_id"] = existing["setup_id"]
                records[index] = record
                break
        else:
            records.append(record)
        path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

    @staticmethod
    def _signal_identity(record: dict) -> tuple:
        return (
            record.get("symbol"), record.get("scanner_name"),
            record.get("direction"), record.get("entry_timeframe"),
            record.get("signal_candle_open_time"),
        )

    def update_status(self, setup_id: str, new_status: str, status_reason: str | None = None) -> None:
        if not self._use_pg:
            return
        now = datetime.now(timezone.utc)
        ts_field = None
        if new_status == "CONFIRMED":
            ts_field = "confirmed_at"
        elif new_status == "EXECUTED":
            ts_field = "executed_at"
        elif new_status == "INVALIDATED":
            ts_field = "invalidated_at"
        elif new_status == "EXPIRED":
            ts_field = "expired_at"

        cursor = self._conn.cursor()
        if ts_field:
            cursor.execute(
                f"UPDATE dds.scanner_setup SET status = %s, status_reason = %s, {ts_field} = %s, updated_at = %s WHERE setup_id = %s",
                (new_status, status_reason, now, now, setup_id),
            )
        else:
            cursor.execute(
                "UPDATE dds.scanner_setup SET status = %s, status_reason = %s, updated_at = %s WHERE setup_id = %s",
                (new_status, status_reason, now, setup_id),
            )
        self._conn.commit()

    # ----------------------------------------------------------------
    # SCANNER ERROR
    # ----------------------------------------------------------------
    def save_error(
        self,
        symbol: str,
        scanner_name: str,
        error_type: str,
        error_message: str,
        run_id: int | None = None,
        retry_count: int = 0,
    ) -> None:
        if not self._use_pg:
            return
        cursor = self._conn.cursor()
        cursor.execute(
            "INSERT INTO dds.scanner_error (run_id, symbol, scanner_name, error_type, error_message, retry_count) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (run_id, symbol, scanner_name, error_type, error_message, retry_count),
        )
        self._conn.commit()

    # ----------------------------------------------------------------
    # SCANNER EVENT
    # ----------------------------------------------------------------
    def save_event(self, event_type: str, scanner_name: str, symbol: str,
                   run_id: int | None = None, **kwargs: Any) -> None:
        if not self._use_pg:
            return
        cursor = self._conn.cursor()
        cursor.execute(
            """
            INSERT INTO dds.scanner_event (
                event_type, scanner_name, scanner_version, symbol, run_id,
                timeframe, direction, score, detected_at, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_type, scanner_name,
                kwargs.get("scanner_version", "1.0.0"),
                symbol, run_id, kwargs.get("timeframe"),
                kwargs.get("direction"), kwargs.get("score"),
                kwargs.get("detected_at"),
                json.dumps(kwargs.get("payload", {})),
            ),
        )
        self._conn.commit()

    # ----------------------------------------------------------------
    # MARKET SIGNAL (aggregation)
    # ----------------------------------------------------------------
    def aggregate_signals(self, run_id: int | None = None) -> int:
        """Aggregate scanner_setup into market_signal. Returns count of active signals."""
        if not self._use_pg:
            return 0

        cursor = self._conn.cursor()

        # Pick only the latest setup from each independent scanner. Repeated
        # detections by one strategy must not manufacture confirmation.
        cursor.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (
                    s.instrument_id, s.direction, s.setup_timeframe, s.scanner_name
                ) s.*
                FROM dds.scanner_setup s
                WHERE s.status IN ('DETECTED', 'CONFIRMED', 'READY_TO_TRADE')
                  AND s.detected_at > now() - interval '4 hours'
                ORDER BY s.instrument_id, s.direction, s.setup_timeframe,
                         s.scanner_name, s.detected_at DESC
            ), aggregated AS (
                SELECT instrument_id, direction, setup_timeframe,
                    COUNT(*) AS scanner_count,
                    jsonb_agg(scanner_name ORDER BY scanner_name) AS scanners,
                    MAX(score) AS max_score,
                    ROUND(AVG(score) + (COUNT(*) - 1) * 5, 2) AS aggregate_score,
                    MIN(detected_at) AS first_detected_at,
                    MAX(detected_at) AS last_detected_at
                FROM latest
                GROUP BY instrument_id, direction, setup_timeframe
            )
            INSERT INTO dds.market_signal (
                instrument_id, direction, timeframe, scanner_count, scanners,
                max_score, aggregate_score, first_detected_at, last_detected_at, status
            )
            SELECT
                instrument_id, direction, setup_timeframe, scanner_count, scanners,
                max_score, aggregate_score, first_detected_at, last_detected_at,
                'ACTIVE' AS status
            FROM aggregated
            ON CONFLICT (instrument_id, direction, timeframe)
            WHERE status = 'ACTIVE' DO UPDATE SET
                scanner_count = EXCLUDED.scanner_count,
                scanners = EXCLUDED.scanners,
                max_score = EXCLUDED.max_score,
                aggregate_score = EXCLUDED.aggregate_score,
                last_detected_at = EXCLUDED.last_detected_at,
                updated_at = now()
        """)
        self._conn.commit()

        # Expire signals with no active setups
        cursor.execute("""
            UPDATE dds.market_signal ms SET
                status = 'EXPIRED',
                status_reason = 'NO_ACTIVE_SETUPS',
                updated_at = now()
            WHERE ms.status = 'ACTIVE'
              AND NOT EXISTS (
                SELECT 1 FROM dds.scanner_setup s
                WHERE s.instrument_id = ms.instrument_id
                  AND s.direction = ms.direction
                  AND s.setup_timeframe = ms.timeframe
                  AND s.status IN ('DETECTED', 'CONFIRMED', 'READY_TO_TRADE')
                  AND s.detected_at > now() - interval '4 hours'
              )
        """)
        self._conn.commit()

        cursor.execute("SELECT COUNT(*) FROM dds.market_signal WHERE status = 'ACTIVE'")
        return cursor.fetchone()[0]

    def expire_setups(self) -> int:
        """Expire active setups after their setup-timeframe candle TTL."""
        if not self._use_pg:
            return 0
        cursor = self._conn.cursor()
        cursor.execute("""
            UPDATE dds.scanner_setup SET status = 'EXPIRED', expired_at = now(),
                status_reason = 'TTL_EXCEEDED', updated_at = now()
            WHERE status IN ('DETECTED', 'CONFIRMED', 'READY_TO_TRADE')
              AND detected_at < now() - CASE setup_timeframe
                WHEN '5m' THEN interval '60 minutes'
                WHEN '15m' THEN interval '120 minutes'
                WHEN '1h' THEN interval '6 hours'
                WHEN '4h' THEN interval '16 hours'
                ELSE interval '2 hours' END
        """)
        count = cursor.rowcount
        self._conn.commit()
        return count

    # ----------------------------------------------------------------
    # SIGNAL OUTCOME
    # ----------------------------------------------------------------
    def save_signal_outcome(self, outcome: SignalOutcome) -> None:
        if not self._use_pg:
            return
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO dds.signal_outcome (
                    setup_id, symbol, scanner_name, direction, entry_touched,
                    first_event, result_r, mfe_r, mae_r, bars_to_entry,
                    bars_to_exit, entry_price, exit_price,
                    fee_slippage_adjusted_result_r, evaluated_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    now(), now()
                )
                ON CONFLICT (setup_id) DO UPDATE SET
                    symbol = EXCLUDED.symbol,
                    scanner_name = EXCLUDED.scanner_name,
                    direction = EXCLUDED.direction,
                    entry_touched = EXCLUDED.entry_touched,
                    first_event = EXCLUDED.first_event,
                    result_r = EXCLUDED.result_r,
                    mfe_r = EXCLUDED.mfe_r,
                    mae_r = EXCLUDED.mae_r,
                    bars_to_entry = EXCLUDED.bars_to_entry,
                    bars_to_exit = EXCLUDED.bars_to_exit,
                    entry_price = EXCLUDED.entry_price,
                    exit_price = EXCLUDED.exit_price,
                    fee_slippage_adjusted_result_r = EXCLUDED.fee_slippage_adjusted_result_r,
                    updated_at = now()
                """,
                (
                    outcome.setup_id, outcome.symbol, outcome.scanner_name,
                    outcome.direction, outcome.entry_touched,
                    outcome.first_event, outcome.result_r, outcome.mfe_r,
                    outcome.mae_r, outcome.bars_to_entry,
                    outcome.bars_to_exit, outcome.entry_price,
                    outcome.exit_price,
                    outcome.fee_slippage_adjusted_result_r,
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def get_setups_without_outcomes(
        self,
        *,
        limit: int = 100,
        min_age_minutes: int = 240,
    ) -> list[SetupCandidate]:
        if not self._use_pg:
            return []
        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT s.setup_id, s.scanner_name, i.symbol, s.direction,
                   s.htf_timeframe, s.setup_timeframe, s.entry_timeframe,
                   s.setup_started_at, s.signal_candle_open_time,
                   s.detected_at, s.reference_price,
                   s.entry_zone_low, s.entry_zone_high,
                   s.invalidation_price, s.target_1, s.target_2,
                   s.score, s.market_regime, s.reasons, s.features
            FROM dds.scanner_setup s
            JOIN dds.instrument i ON i.instrument_id = s.instrument_id
            LEFT JOIN dds.signal_outcome o ON o.setup_id = s.setup_id
            WHERE o.setup_id IS NULL
              AND s.signal_candle_open_time > 0
              AND s.detected_at < now() - (%s * interval '1 minute')
              AND s.entry_zone_low IS NOT NULL
              AND s.entry_zone_high IS NOT NULL
              AND s.invalidation_price IS NOT NULL
              AND s.target_1 IS NOT NULL
              AND (
                (s.direction = 'LONG'
                 AND s.invalidation_price < s.entry_zone_low
                 AND s.target_1 > s.entry_zone_high)
                OR
                (s.direction = 'SHORT'
                 AND s.invalidation_price > s.entry_zone_high
                 AND s.target_1 < s.entry_zone_low)
              )
            ORDER BY s.detected_at ASC
            LIMIT %s
            """,
            (min_age_minutes, limit),
        )
        return [self._row_to_setup_candidate(row) for row in cursor.fetchall()]

    @staticmethod
    def _row_to_setup_candidate(row: tuple) -> SetupCandidate:
        reasons = row[18]
        features = row[19]
        if isinstance(reasons, str):
            reasons = json.loads(reasons)
        if isinstance(features, str):
            features = json.loads(features)
        return SetupCandidate(
            setup_id=UUID(str(row[0])),
            scanner_name=row[1],
            symbol=row[2],
            direction=row[3],
            htf_timeframe=row[4],
            setup_timeframe=row[5],
            entry_timeframe=row[6],
            setup_started_at=row[7],
            signal_candle_open_time=int(row[8]),
            detected_at=row[9],
            reference_price=float(row[10]),
            entry_zone_low=float(row[11]),
            entry_zone_high=float(row[12]),
            invalidation_price=float(row[13]),
            target_1=float(row[14]),
            target_2=float(row[15]) if row[15] is not None else None,
            score=float(row[16]),
            market_regime=row[17],
            reasons=tuple(reasons or ()),
            features=dict(features or {}),
        )

    # ----------------------------------------------------------------
    # QUERIES
    # ----------------------------------------------------------------
    def get_active_setups(self, symbol: str | None = None) -> list[dict]:
        if self._use_pg:
            return self._get_active_pg(symbol)
        return self._get_active_jsonl(symbol)

    def _get_active_pg(self, symbol: str | None = None) -> list[dict]:
        cursor = self._conn.cursor()
        if symbol:
            cursor.execute(
                """
                SELECT s.setup_id, s.scanner_name, i.symbol, s.direction,
                       s.reference_price, s.score, s.status, s.detected_at,
                       s.entry_zone_low, s.entry_zone_high, s.invalidation_price,
                       s.target_1, s.target_2, s.reasons, s.features
                FROM dds.scanner_setup s
                JOIN dds.instrument i ON i.instrument_id = s.instrument_id
                WHERE i.symbol = %s AND s.status IN ('DETECTED', 'CONFIRMED', 'READY_TO_TRADE')
                ORDER BY s.score DESC
                """,
                (symbol,),
            )
        else:
            cursor.execute(
                """
                SELECT s.setup_id, s.scanner_name, i.symbol, s.direction,
                       s.reference_price, s.score, s.status, s.detected_at,
                       s.entry_zone_low, s.entry_zone_high, s.invalidation_price,
                       s.target_1, s.target_2, s.reasons, s.features
                FROM dds.scanner_setup s
                JOIN dds.instrument i ON i.instrument_id = s.instrument_id
                WHERE s.status IN ('DETECTED', 'CONFIRMED', 'READY_TO_TRADE')
                ORDER BY s.score DESC
                LIMIT 20
                """,
            )
        rows = cursor.fetchall()
        return [
            {
                "setup_id": str(r[0]), "scanner_name": r[1], "symbol": r[2],
                "direction": r[3], "reference_price": float(r[4]),
                "score": float(r[5]), "status": r[6], "detected_at": r[7].isoformat(),
                "entry_zone_low": float(r[8]) if r[8] else None,
                "entry_zone_high": float(r[9]) if r[9] else None,
                "invalidation_price": float(r[10]) if r[10] else None,
                "target_1": float(r[11]) if r[11] else None,
                "target_2": float(r[12]) if r[12] else None,
                "reasons": r[13], "features": r[14],
            }
            for r in rows
        ]

    def _get_active_jsonl(self, symbol: str | None = None) -> list[dict]:
        from pathlib import Path
        if not Path(self._jsonl_path).exists():
            return []
        results: list[dict] = []
        for line in Path(self._jsonl_path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("status") not in ("DETECTED", "CONFIRMED", "READY_TO_TRADE"):
                continue
            if symbol and record.get("symbol") != symbol:
                continue
            results.append(record)
        results.sort(key=lambda r: r.get("score", 0), reverse=True)
        return results[:20]

    def get_active_market_signals(self, limit: int = 20) -> list[dict]:
        if not self._use_pg:
            return []
        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT i.symbol, ms.direction, ms.scanner_count, ms.scanners,
                   ms.max_score, ms.aggregate_score, ms.first_detected_at,
                   ms.last_detected_at
            FROM dds.market_signal ms
            JOIN dds.instrument i ON i.instrument_id = ms.instrument_id
            WHERE ms.status = 'ACTIVE'
            ORDER BY ms.aggregate_score DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [
            {
                "symbol": r[0], "direction": r[1], "scanner_count": r[2],
                "scanners": r[3], "max_score": float(r[4]),
                "aggregate_score": float(r[5]),
                "first_detected_at": r[6].isoformat() if r[6] else None,
                "last_detected_at": r[7].isoformat() if r[7] else None,
            }
            for r in cursor.fetchall()
        ]

    def get_stats(self) -> dict:
        if self._use_pg:
            cursor = self._conn.cursor()
            cursor.execute("SELECT * FROM dds.scanner_stats")
            rows = cursor.fetchall()
            cols = [d[0] for d in cursor.description] if cursor.description else []
            return {r[0]: dict(zip(cols[1:], r[1:])) for r in rows}
        return {"mode": "jsonl", "file": self._jsonl_path}

    # ----------------------------------------------------------------
    # PAPER TRADING
    # ----------------------------------------------------------------
    def save_paper_trade(self, trade: Any) -> int | None:
        """Insert a new paper trade and return its trade_id."""
        if not self._use_pg:
            return None
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO dds.paper_trade (
                    setup_id, symbol, scanner_name, direction, score,
                    entry_price, entry_fee, stop_price, target_1, target_2,
                    position_size, risk_usdt, balance_before, market_regime,
                    status, entered_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'OPEN', %s
                ) RETURNING trade_id
                """,
                (
                    trade.setup_id, trade.symbol, trade.scanner_name,
                    trade.direction, trade.score, trade.entry_price,
                    trade.entry_fee, trade.stop_price, trade.target_1,
                    trade.target_2, trade.position_size, trade.risk_usdt,
                    trade.balance_before, trade.market_regime, trade.entered_at,
                ),
            )
            self._conn.commit()
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception:
            self._conn.rollback()
            logger.exception("save_paper_trade failed for %s", trade.symbol)
            raise

    def close_paper_trade(
        self,
        trade_id: int | None,
        exit_price: float,
        exit_reason: str,
        exit_fee: float,
        pnl_usdt: float,
        pnl_r: float,
        pnl_percent: float,
        slippage: float,
        balance_after: float,
        duration_sec: float,
    ) -> None:
        """Update a paper trade with exit data."""
        if not self._use_pg or trade_id is None:
            return
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                UPDATE dds.paper_trade SET
                    exit_price = %s,
                    exit_reason = %s,
                    exit_fee = %s,
                    pnl_usdt = %s,
                    pnl_r = %s,
                    pnl_percent = %s,
                    slippage = %s,
                    balance_after = %s,
                    duration_sec = %s,
                    status = 'CLOSED',
                    closed_at = now(),
                    updated_at = now()
                WHERE trade_id = %s
                """,
                (
                    exit_price, exit_reason, exit_fee, pnl_usdt,
                    pnl_r, pnl_percent, slippage, balance_after,
                    duration_sec, trade_id,
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            logger.exception("close_paper_trade failed for trade_id=%s", trade_id)
            raise

    def get_open_paper_trades(self) -> list[dict]:
        """Load all OPEN paper trades (for engine restart recovery)."""
        if not self._use_pg:
            return []
        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT trade_id, setup_id, symbol, scanner_name, direction, score,
                   entry_price, entry_fee, stop_price, target_1, target_2,
                   position_size, risk_usdt, balance_before, market_regime,
                   entered_at
            FROM dds.paper_trade
            WHERE status = 'OPEN'
            ORDER BY entered_at DESC
            """
        )
        rows = cursor.fetchall()
        return [
            {
                "trade_id": r[0], "setup_id": r[1], "symbol": r[2],
                "scanner_name": r[3], "direction": r[4], "score": float(r[5]),
                "entry_price": float(r[6]), "entry_fee": float(r[7]),
                "stop_price": float(r[8]),
                "target_1": float(r[9]) if r[9] is not None else None,
                "target_2": float(r[10]) if r[10] is not None else None,
                "position_size": float(r[11]), "risk_usdt": float(r[12]),
                "balance_before": float(r[13]),
                "market_regime": r[14],
                "entered_at": r[15],
            }
            for r in rows
        ]

    def expire_stale_setups(self, max_age_minutes: int = 120) -> int:
        """Mark old READY_TO_TRADE setups as EXPIRED."""
        if not self._use_pg:
            return 0
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                UPDATE dds.scanner_setup SET status = 'EXPIRED', expired_at = now(), updated_at = now()
                WHERE status = 'READY_TO_TRADE'
                  AND detected_at < now() - make_interval(mins := %s)
                """,
                (max_age_minutes,),
            )
            count = cursor.rowcount
            self._conn.commit()
            return count
        except Exception:
            self._conn.rollback()
            raise

    def get_paper_trade_stats(self) -> list[dict]:
        """Get aggregated paper trade performance by scanner/direction."""
        if not self._use_pg:
            return []
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM dds.paper_trade_stats")
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description] if cursor.description else []
        return [dict(zip(cols, r)) for r in rows]

    def save_paper_account_snapshot(
        self,
        balance: float,
        equity: float,
        open_positions: int,
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        total_pnl: float,
        max_drawdown: float,
    ) -> None:
        """Save an account equity snapshot."""
        if not self._use_pg:
            return
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO dds.paper_account (
                    balance, equity, open_positions, total_trades,
                    winning_trades, losing_trades, total_pnl, max_drawdown
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    balance, equity, open_positions, total_trades,
                    winning_trades, losing_trades, total_pnl, max_drawdown,
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        if self._conn:
            self._conn.close()
