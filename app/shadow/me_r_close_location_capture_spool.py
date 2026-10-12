"""Durable local capture ledger for ME clean OOS v1.2.0.

Research-only. No paper/live operations and no PostgreSQL writes.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ME_NAME = "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"
CLEAN_VERSION = "1.2.0"

FEATURE_KEYS = (
    "oos_clean_observer_version",
    "oos_signal_open",
    "oos_signal_high",
    "oos_signal_low",
    "oos_signal_close",
    "oos_signal_volume",
    "oos_signal_entry_price",
    "oos_signal_candle_open_ms",
    "close_location_source_timestamp",
    "close_location",
    "close_location_threshold",
    "close_location_passed",
    "rsi",
    "atr",
)


def _safe_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    raise ValueError(f"Unsupported capture value type: {type(value).__name__}")


class MECleanCaptureSpool:
    """Persistent, idempotent ME observation queue."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(
            str(self.path),
            timeout=10,
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=10000")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS capture (
                event_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_ack TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def enqueue(self, candidate) -> str:
        if candidate.scanner_name != ME_NAME:
            raise ValueError("Not an ME clean OOS candidate")
        if candidate.scanner_version != CLEAN_VERSION:
            raise ValueError("Not clean observer version 1.2.0")

        features = candidate.features or {}
        selected = {
            key: _safe_value(features[key])
            for key in FEATURE_KEYS
            if key in features
        }

        detected_at = candidate.detected_at
        if not isinstance(detected_at, datetime) or detected_at.tzinfo is None:
            raise ValueError("Missing timezone-aware decision timestamp")

        payload = {
            "scanner_name": candidate.scanner_name,
            "scanner_version": candidate.scanner_version,
            "symbol": candidate.symbol,
            "direction": candidate.direction,
            "detected_at": detected_at.isoformat(),
            "features": selected,
        }

        encoded = json.dumps(
            payload, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )

        # Stable per signal candle, independent of repeated scan time.
        identity = {
            "experiment": ME_NAME,
            "version": CLEAN_VERSION,
            "symbol": candidate.symbol,
            "source": selected.get(
                "oos_signal_candle_open_ms",
                selected.get("close_location_source_timestamp"),
            ),
        }
        if identity["source"] is None:
            identity["source"] = hashlib.sha256(
                encoded.encode("utf-8")
            ).hexdigest()

        identity_json = json.dumps(identity, sort_keys=True)
        key = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()

        with self.conn:
            self.conn.execute("""
                INSERT INTO capture (
                    event_key, payload, status,
                    created_at, updated_at
                )
                VALUES (?, ?, 'PENDING', ?, ?)
                ON CONFLICT(event_key) DO NOTHING
            """, (key, encoded, now, now))

            existing = self.conn.execute(
                "SELECT payload FROM capture WHERE event_key = ?",
                (key,),
            ).fetchone()

            if existing is None:
                raise RuntimeError("Durable capture row missing after enqueue")

            if existing[0] != encoded:
                previous = json.loads(existing[0])
                current = json.loads(encoded)

                # Repeated scans may have a later decision timestamp.
                # Preserve the first durable observation.
                previous_decision = previous.pop("detected_at", None)
                current_decision = current.pop("detected_at", None)

                if previous != current:
                    raise ValueError(
                        "ME clean capture identity collision or payload drift: "
                        + key
                    )

                if not previous_decision or not current_decision:
                    raise ValueError("Missing decision timestamp in capture")

        return key

    def pending(self, limit: int = 100):
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self.conn.execute("""
            SELECT event_key, payload, attempts
            FROM capture
            WHERE status = 'PENDING'
            ORDER BY attempts ASC, updated_at ASC, event_key ASC
            LIMIT ?
        """, (limit,)).fetchall()

        return [
            (key, json.loads(payload), attempts)
            for key, payload, attempts in rows
        ]

    def acknowledge(self, key: str, ack: str):
        if ack in ("INSERTED", "DUPLICATE"):
            status = "DELIVERED"
        elif ack in ("INVALID_SOURCE", "SKIPPED_VERSION"):
            status = "QUARANTINED"
        elif ack == "ERROR":
            status = "PENDING"
        else:
            raise ValueError(f"Unknown observation ACK: {ack}")

        now = datetime.now(timezone.utc).isoformat()

        with self.conn:
            result = self.conn.execute("""
                UPDATE capture
                SET status = ?,
                    last_ack = ?,
                    attempts = attempts + 1,
                    updated_at = ?
                WHERE event_key = ? AND status = 'PENDING'
            """, (status, ack, now, key))

        if result.rowcount != 1:
            raise ValueError("Unknown or already finalized capture event")

    def stats(self):
        rows = self.conn.execute("""
            SELECT status, COUNT(*)
            FROM capture
            GROUP BY status
        """).fetchall()

        counts = {
            "PENDING": 0,
            "DELIVERED": 0,
            "QUARANTINED": 0,
        }
        counts.update(dict(rows))
        counts["TOTAL"] = sum(
            counts[k] for k in ("PENDING", "DELIVERED", "QUARANTINED")
        )
        return counts
