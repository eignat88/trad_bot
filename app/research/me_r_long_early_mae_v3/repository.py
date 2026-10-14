"""Dedicated PostgreSQL repository for independent V3 research storage."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .candles import ClosedCandle
from .lifecycle import can_finalize
from .protocol import (
    ACTIVATION_AT,
    BASELINE_MAX_HOLD_MINUTES,
    BASELINE_POLICY,
    ENTRY_EXPIRY_MINUTES,
    EXPERIMENT_ID,
    INTRABAR_POLICY,
    NORMAL_SLIPPAGE_PER_SIDE,
    ELEVATED_SLIPPAGE_PER_SIDE,
    SCANNER_NAME,
    SCANNER_VERSION,
    SOURCE_CANDLE_VERSION,
    SOURCE_DIRECTION,
    SOURCE_SCHEMA,
    TAKER_FEE_PER_SIDE,
    VARIANTS,
    parse_utc,
    protocol_payload,
    require_protocol_fingerprint,
    validate_freeze_activation,
)


class MeEarlyMaeV3Repository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def _cursor(self):
        return self.connection.cursor()

    def register_protocol(self) -> None:
        validate_freeze_activation(
            FROZEN_AT, ACTIVATION_AT, datetime.now(timezone.utc)
        )
        fingerprint = require_protocol_fingerprint(FROZEN_AT, ACTIVATION_AT)
        payload = protocol_payload(FROZEN_AT, ACTIVATION_AT)
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""INSERT INTO {SOURCE_SCHEMA}.protocol (
                    protocol_version, experiment_id, scanner_name, scanner_version,
                    source_candle_version, direction, frozen_at, activation_at,
                    protocol_json, protocol_fingerprint
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (protocol_version) DO NOTHING""",
                (
                    "3.0.0-DRAFT", EXPERIMENT_ID, SCANNER_NAME, SCANNER_VERSION,
                    SOURCE_CANDLE_VERSION, SOURCE_DIRECTION, FROZEN_AT, ACTIVATION_AT,
                    json.dumps(payload), fingerprint,
                ),
            )
            for variant in VARIANTS:
                cursor.execute(
                    f"""INSERT INTO {SOURCE_SCHEMA}.variant (
                        protocol_version, variant_id, horizon_minutes,
                        threshold_mae_r, role, is_primary
                    ) VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (protocol_version, variant_id) DO NOTHING""",
                    (
                        "3.0.0-DRAFT", variant.variant_id, variant.horizon_minutes,
                        variant.threshold_mae_r, variant.role, variant.is_primary,
                    ),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def ensure_instrument(self, symbol: str) -> int | None:
        cursor = self._cursor()
        try:
            cursor.execute(
                "SELECT instrument_id FROM dds.instrument WHERE symbol = %s",
                (symbol,),
            )
            row = cursor.fetchone()
            self.connection.rollback()
            return int(row[0]) if row else None
        except Exception:
            self.connection.rollback()
            raise

    def insert_source_with_variants(self, source: dict[str, Any]) -> int | None:
        validate_freeze_activation(
            FROZEN_AT, ACTIVATION_AT, datetime.now(timezone.utc)
        )
        fingerprint = require_protocol_fingerprint(FROZEN_AT, ACTIVATION_AT)
        if parse_utc(source["detected_at"]) <= parse_utc(ACTIVATION_AT):
            raise ValueError("PRE_ACTIVATION_SOURCE_REJECTED")
        close_at = parse_utc(source["source_candle_close_at"])
        if parse_utc(source["detected_at"]) < close_at:
            raise ValueError("UNCLOSED_SOURCE_CANDLE")
        if (parse_utc(source["detected_at"]) - close_at).total_seconds() > 300:
            raise ValueError("SOURCE_DELAY_EXCEEDS_300_SECONDS")
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""INSERT INTO {SOURCE_SCHEMA}.source_event (
                    protocol_version, source_setup_id, instrument_id, symbol,
                    scanner_name, scanner_version, source_candle_version,
                    direction, source_candle_open_at, source_candle_close_at,
                    source_fetch_started_at, source_available_at, detected_at,
                    entry_eligible_at, entry_zone_low, entry_zone_high,
                    reference_price, stop_price, target_price, risk_distance,
                    source_features, source_status, coverage_status,
                    invalid_reason, protocol_fingerprint
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s::jsonb,%s,%s,%s,%s
                )
                ON CONFLICT (protocol_version, source_setup_id) DO NOTHING
                RETURNING source_event_id""",
                (
                    "3.0.0-DRAFT", str(source["source_setup_id"]),
                    int(source["instrument_id"]), source["symbol"],
                    SCANNER_NAME, SCANNER_VERSION, SOURCE_CANDLE_VERSION,
                    SOURCE_DIRECTION, source["source_candle_open_at"], close_at,
                    source["source_fetch_started_at"],
                    close_at.replace(microsecond=0) + timedelta(seconds=300),
                    source["detected_at"], source["entry_eligible_at"],
                    source["entry_zone_low"], source["entry_zone_high"],
                    source["reference_price"], source["stop_price"],
                    source["target_price"], source["risk_distance"],
                    json.dumps(source.get("features") or {}),
                    "DETECTED", "PENDING", None, fingerprint,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("SOURCE_IDENTITY_COLLISION")
            source_id = int(row[0])
            for variant in VARIANTS:
                cursor.execute(
                    f"""INSERT INTO {SOURCE_SCHEMA}.variant_outcome (
                        protocol_version, source_event_id, variant_id,
                        horizon_minutes, threshold_mae_r, role, is_primary,
                        candle_source_version, variant_status, coverage_status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (protocol_version, source_event_id, variant_id)
                    DO NOTHING""",
                    (
                        "3.0.0-DRAFT", source_id, variant.variant_id,
                        variant.horizon_minutes, variant.threshold_mae_r,
                        variant.role, variant.is_primary, SOURCE_CANDLE_VERSION,
                        "PENDING", "PENDING",
                    ),
                )
            self.connection.commit()
            return source_id
        except Exception:
            self.connection.rollback()
            raise

    def update_source_entry(self, source_event_id: int, entry: dict[str, Any]) -> None:
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""UPDATE {SOURCE_SCHEMA}.source_event
                    SET source_status='ENTRY_FILLED', coverage_status='PENDING',
                        entry_filled_at=%(entry_filled_at)s, entry_price=%(entry_price)s,
                        stop_price=%(stop_price)s, target_price=%(target_price)s,
                        risk_distance=%(risk_distance)s, baseline_deadline_at=%(baseline_deadline_at)s
                    WHERE source_event_id=%(source_event_id)s
                      AND source_status IN ('DETECTED','ENTRY_PENDING')
                      AND entry_filled_at IS NULL""",
                {"source_event_id": source_event_id, **entry},
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def save_paired_outcome(
        self,
        source_event_id: int,
        baseline: dict[str, Any],
        evaluations: list[dict[str, Any]],
    ) -> None:
        if len(evaluations) != 7 or {r["variant_id"] for r in evaluations} != {
            v.variant_id for v in VARIANTS
        }:
            raise ValueError("EXPECTED_EXACTLY_SEVEN_VARIANTS")
        if not can_finalize(evaluations):
            if any(
                row.get("variant_status") != "INCOMPLETE_COVERAGE"
                for row in evaluations
            ):
                raise ValueError("INCOMPLETE_NONFINAL_OUTCOME")
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""SELECT source_status FROM {SOURCE_SCHEMA}.source_event
                    WHERE source_event_id=%s FOR UPDATE""",
                (source_event_id,),
            )
            status = cursor.fetchone()
            if status is None or status[0] not in ("ENTRY_FILLED", "FINALIZED"):
                raise ValueError("SOURCE_NOT_ENTRY_FILLED")
            cursor.execute(
                f"""UPDATE {SOURCE_SCHEMA}.source_event
                    SET source_status=%(source_status)s, coverage_status=%(coverage_status)s,
                        baseline_exit_at=%(baseline_exit_at)s,
                        baseline_exit_price=%(baseline_exit_price)s,
                        baseline_exit_reason=%(baseline_exit_reason)s,
                        baseline_gross_r=%(baseline_gross_r)s,
                        baseline_net_r_normal=%(baseline_net_r_normal)s,
                        baseline_net_r_elevated=%(baseline_net_r_elevated)s
                    WHERE source_event_id=%(source_event_id)s
                      AND source_status<>'FINALIZED'""",
                {"source_event_id": source_event_id, **baseline},
            )
            for row in evaluations:
                cursor.execute(
                    f"""UPDATE {SOURCE_SCHEMA}.variant_outcome
                        SET variant_status=%(variant_status)s,
                            trigger_result=%(trigger_result)s, mae_r=%(mae_r)s,
                            mfe_r=%(mfe_r)s, window_low=%(window_low)s,
                            window_high=%(window_high)s,
                            horizon_end_at=%(horizon_end_at)s,
                            variant_exit_at=%(variant_exit_at)s,
                            variant_exit_price=%(variant_exit_price)s,
                            variant_exit_reason=%(variant_exit_reason)s,
                            variant_gross_r=%(variant_gross_r)s,
                            net_r_normal=%(net_r_normal)s,
                            net_r_elevated=%(net_r_elevated)s,
                            delta_net_r_normal=%(delta_net_r_normal)s,
                            delta_net_r_elevated=%(delta_net_r_elevated)s,
                            ambiguous_ordering=%(ambiguous_ordering)s,
                            coverage_status=%(coverage_status)s,
                            coverage_reason=%(coverage_reason)s,
                            execution_price_semantics=%(execution_price_semantics)s
                        WHERE protocol_version=%s AND source_event_id=%s
                          AND variant_id=%(variant_id)s
                          AND variant_status<>'FINALIZED'""",
                    {
                        **row,
                        "source_event_id": source_event_id,
                    },
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("VARIANT_UPDATE_ROW_COUNT")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def fetch_candles(
        self, instrument_id: int, start: datetime, end: datetime
    ) -> list[ClosedCandle]:
        cursor = self._cursor()
        cursor.execute(
            """SELECT open_time, close_time, open, high, low, close
               FROM market.candle WHERE instrument_id=%s AND timeframe='5'
                 AND is_closed=TRUE AND close_time>%s AND close_time<=%s
               ORDER BY open_time""",
            (instrument_id, start, end),
        )
        return [
            ClosedCandle(
                open_at=row[0], close_at=row[1], open=float(row[2]),
                high=float(row[3]), low=float(row[4]), close=float(row[5]),
            )
            for row in cursor.fetchall()
        ]

    def load_pending_sources(self) -> list[dict[str, Any]]:
        cursor = self._cursor()
        cursor.execute(
            f"""SELECT source_event_id, instrument_id, entry_filled_at, entry_price,
                       stop_price, target_price, baseline_deadline_at, source_status
                FROM {SOURCE_SCHEMA}.source_event
                WHERE protocol_version=%s AND source_status IN
                      ('ENTRY_PENDING','ENTRY_FILLED') ORDER BY detected_at""",
            ("3.0.0-DRAFT",),
        )
        return [
            {
                "source_event_id": int(r[0]), "instrument_id": int(r[1]),
                "entry_filled_at": r[2], "entry_price": float(r[3]) if r[3] is not None else None,
                "stop_price": float(r[4]) if r[4] is not None else None,
                "target_price": float(r[5]) if r[5] is not None else None,
                "baseline_deadline_at": r[6], "source_status": r[7],
            }
            for r in cursor.fetchall()
        ]
