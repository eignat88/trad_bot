from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.scanners.models import SetupCandidate, SetupState

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
    ) -> None:
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._jsonl_path = jsonl_path
        self._conn: Any = None
        self._use_pg = False

        try:
            import pg8000
            self._conn = pg8000.connect(
                host=host, port=port, database=database, user=user
            )
            self._use_pg = True
            logger.info("scanner repository connected to PostgreSQL (%s:%d/%s)", host, port, database)
        except ImportError:
            logger.warning("pg8000 not installed, using JSONL fallback")
        except Exception:
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

    def ensure_instrument(self, symbol: str, base_asset: str = "", quote_asset: str = "USDT") -> int | None:
        if not self._use_pg:
            return None

        base = base_asset or symbol.replace(quote_asset, "")
        cursor = self._conn.cursor()
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

    def save_setup(self, candidate: SetupCandidate) -> None:
        if self._use_pg:
            self._save_pg(candidate)
        else:
            self._save_jsonl(candidate)

    def _save_pg(self, c: SetupCandidate) -> None:
        instrument_id = self.ensure_instrument(c.symbol)
        if instrument_id is None:
            return

        cursor = self._conn.cursor()
        cursor.execute(
            """
            INSERT INTO dds.scanner_setup (
                setup_id, scanner_name, scanner_version, instrument_id,
                direction, htf_timeframe, setup_timeframe, entry_timeframe,
                setup_started_at, signal_candle_open_time, detected_at, reference_price,
                entry_zone_low, entry_zone_high, invalidation_price,
                target_1, target_2, score, market_regime, status,
                reasons, features
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (
                instrument_id, scanner_name, direction, entry_timeframe,
                signal_candle_open_time
            ) WHERE signal_candle_open_time > 0 DO UPDATE SET
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
                features = EXCLUDED.features
            """,
            (
                str(c.setup_id), c.scanner_name, c.scanner_version, instrument_id,
                c.direction, c.htf_timeframe, c.setup_timeframe, c.entry_timeframe,
                c.setup_started_at, c.signal_candle_open_time, c.detected_at, c.reference_price,
                c.entry_zone_low, c.entry_zone_high, c.invalidation_price,
                c.target_1, c.target_2, c.score, c.market_regime,
                "READY" if (c.state.value == "SETUP_READY" if isinstance(c.state, SetupState) else c.state == "SETUP_READY") else (c.state.value if isinstance(c.state, SetupState) else c.state),
                json.dumps(list(c.reasons)), json.dumps(c.features),
            ),
        )
        self._conn.commit()

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
            "status": "READY" if c.state == SetupState.SETUP_READY else (c.state.value if isinstance(c.state, SetupState) else c.state),
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
                WHERE i.symbol = %s AND s.status IN ('CANDIDATE', 'READY')
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
                WHERE s.status IN ('CANDIDATE', 'READY')
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
            if record.get("status") not in ("CANDIDATE", "READY"):
                continue
            if symbol and record.get("symbol") != symbol:
                continue
            results.append(record)

        results.sort(key=lambda r: r.get("score", 0), reverse=True)
        return results[:20]

    def update_status(self, setup_id: str, new_status: str) -> None:
        if self._use_pg:
            cursor = self._conn.cursor()
            cursor.execute(
                "UPDATE dds.scanner_setup SET status = %s WHERE setup_id = %s",
                (new_status, setup_id),
            )
            self._conn.commit()

    def save_event(self, event_type: str, scanner_name: str, symbol: str,
                   **kwargs: Any) -> None:
        if self._use_pg:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO dds.scanner_event (
                    event_type, scanner_name, scanner_version, symbol,
                    timeframe, direction, score, detected_at, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event_type, scanner_name,
                    kwargs.get("scanner_version", "1.0.0"),
                    symbol, kwargs.get("timeframe"),
                    kwargs.get("direction"), kwargs.get("score"),
                    kwargs.get("detected_at"),
                    json.dumps(kwargs.get("payload", {})),
                ),
            )
            self._conn.commit()

    def get_stats(self) -> dict:
        if self._use_pg:
            cursor = self._conn.cursor()
            cursor.execute("SELECT * FROM dds.scanner_stats")
            rows = cursor.fetchall()
            cols = [d[0] for d in cursor.description] if cursor.description else []
            return {r[0]: dict(zip(cols[1:], r[1:])) for r in rows}
        return {"mode": "jsonl", "file": self._jsonl_path}

    def close(self) -> None:
        if self._conn:
            self._conn.close()
