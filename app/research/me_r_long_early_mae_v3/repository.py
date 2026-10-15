"""Dedicated PostgreSQL repository for independent V3 research storage."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .candles import ClosedCandle
from .lifecycle import can_finalize
from .protocol import (
    PROTOCOL_VERSION,
    FROZEN_AT,
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
    validate_measured_source_availability,
    validate_freeze_activation,
)


class MeEarlyMaeV3Repository:
    """Independent V3 repository with strict state and protocol guards."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def _cursor(self):
        return self.connection.cursor()

    def register_protocol(self) -> None:
        fingerprint = require_protocol_fingerprint(FROZEN_AT, ACTIVATION_AT)
        payload = protocol_payload(FROZEN_AT, ACTIVATION_AT)
        cursor = self._cursor()
        try:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (PROTOCOL_LOCK_KEY,))
            cursor.execute(
                f"""INSERT INTO {SOURCE_SCHEMA}.protocol (
                    protocol_version, experiment_id, scanner_name, scanner_version,
                    source_candle_version, direction, frozen_at, activation_at,
                    protocol_json, protocol_fingerprint
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (protocol_version) DO NOTHING""",
                (
                    PROTOCOL_VERSION, EXPERIMENT_ID, SCANNER_NAME, SCANNER_VERSION,
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
                        PROTOCOL_VERSION, variant.variant_id, variant.horizon_minutes,
                        variant.threshold_mae_r, variant.role, variant.is_primary,
                    ),
                )
            cursor.execute(
                f"""SELECT experiment_id, scanner_name, scanner_version, direction,
                           source_candle_version, frozen_at, activation_at,
                           protocol_json, protocol_fingerprint
                    FROM {SOURCE_SCHEMA}.protocol
                    WHERE protocol_version=%s FOR UPDATE""",
                (PROTOCOL_VERSION,),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("PROTOCOL_REGISTRATION_MISSING")
            stored = (
                row[0], row[1], row[2], row[3], row[4],
                parse_utc(row[5]), parse_utc(row[6]),
                json.loads(row[7]) if isinstance(row[7], str) else row[7],
                row[8],
            )
            expected = (
                EXPERIMENT_ID, SCANNER_NAME, SCANNER_VERSION, SOURCE_DIRECTION,
                SOURCE_CANDLE_VERSION, parse_utc(FROZEN_AT), parse_utc(ACTIVATION_AT),
                payload, fingerprint,
            )
            if stored != expected:
                raise RuntimeError("PROTOCOL_REGISTRATION_MISMATCH")
            cursor.execute(
                f"""SELECT variant_id, horizon_minutes, threshold_mae_r, role, is_primary
                    FROM {SOURCE_SCHEMA}.variant WHERE protocol_version=%s
                    ORDER BY variant_id FOR UPDATE""",
                (PROTOCOL_VERSION,),
            )
            rows = cursor.fetchall()
            expected_variants = sorted(
                (v.variant_id, v.horizon_minutes, Decimal(str(v.threshold_mae_r)), v.role, v.is_primary)
                for v in VARIANTS
            )
            stored_variants = sorted(
                (r[0], int(r[1]), Decimal(str(r[2])), r[3], bool(r[4]))
                for r in rows
            )
            if stored_variants != expected_variants:
                raise RuntimeError("PROTOCOL_VARIANT_REGISTRATION_MISMATCH")
            if sum(1 for row in stored_variants if row[4]) != 1:
                raise RuntimeError("PRIMARY_VARIANT_COUNT_INVALID")
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

    def insert_source_with_variants(self, source: dict[str, Any]) -> int:
        validate_freeze_activation(FROZEN_AT, ACTIVATION_AT, datetime.now(timezone.utc))
        fingerprint = require_protocol_fingerprint(FROZEN_AT, ACTIVATION_AT)
        detected = parse_utc(source["detected_at"])
        close_at = parse_utc(source["source_candle_close_at"])
        if close_at <= parse_utc(ACTIVATION_AT):
            raise ValueError("PRE_ACTIVATION_CANDLE_REJECTED")
        if detected <= parse_utc(ACTIVATION_AT):
            raise ValueError("PRE_ACTIVATION_SOURCE_REJECTED")
        measured_available = parse_utc(source["source_available_at"])
        validate_measured_source_availability(close_at, measured_available, detected)
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""SELECT source_event_id FROM {SOURCE_SCHEMA}.source_event
                    WHERE protocol_version=%s AND source_setup_id=%s FOR UPDATE""",
                (PROTOCOL_VERSION, str(source["source_setup_id"])),
            )
            existing = cursor.fetchone()
            if existing is not None:
                source_id = int(existing[0])
                self._verify_existing_source(cursor, source, source_id, fingerprint)
            else:
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
                    ) RETURNING source_event_id""",
                    (
                        PROTOCOL_VERSION, str(source["source_setup_id"]),
                        int(source["instrument_id"]), source["symbol"],
                        SCANNER_NAME, SCANNER_VERSION, SOURCE_CANDLE_VERSION,
                        SOURCE_DIRECTION, source["source_candle_open_at"], close_at,
                        source["source_fetch_started_at"], measured_available,
                        detected, source["entry_eligible_at"],
                        source["entry_zone_low"], source["entry_zone_high"],
                        source["reference_price"], source["stop_price"],
                        source["target_price"], source["risk_distance"],
                        json.dumps(source.get("features") or {}),
                        "DETECTED", "PENDING", None, fingerprint,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("SOURCE_INSERT_RETURNED_NO_ID")
                source_id = int(row[0])

            cursor.execute(
                f"""SELECT variant_id, variant_status, horizon_minutes, threshold_mae_r,
                           role, is_primary
                    FROM {SOURCE_SCHEMA}.variant_outcome
                    WHERE protocol_version=%s AND source_event_id=%s
                    ORDER BY variant_id FOR UPDATE""",
                (PROTOCOL_VERSION, source_id),
            )
            stored = {row[0]: row for row in cursor.fetchall()}
            expected = {v.variant_id: v for v in VARIANTS}
            if stored:
                if set(stored) != set(expected):
                    raise RuntimeError("SOURCE_VARIANT_TUPLE_CONFLICT")
                for variant_id, row in stored.items():
                    definition = expected[variant_id]
                    if (
                        row[2] != definition.horizon_minutes
                        or Decimal(str(row[3])) != Decimal(str(definition.threshold_mae_r))
                        or row[4] != definition.role
                        or bool(row[5]) != definition.is_primary
                    ):
                        raise RuntimeError(f"VARIANT_DEFINITION_MISMATCH:{variant_id}")
            else:
                for variant in VARIANTS:
                    cursor.execute(
                        f"""INSERT INTO {SOURCE_SCHEMA}.variant_outcome (
                            protocol_version, source_event_id, variant_id,
                            horizon_minutes, threshold_mae_r, role, is_primary,
                            candle_source_version, variant_status, coverage_status
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PENDING','PENDING')
                        ON CONFLICT (protocol_version, source_event_id, variant_id)
                        DO NOTHING""",
                        (
                            PROTOCOL_VERSION, source_id, variant.variant_id,
                            variant.horizon_minutes, variant.threshold_mae_r,
                            variant.role, variant.is_primary, SOURCE_CANDLE_VERSION,
                        ),
                    )
            self.connection.commit()
            return source_id
        except Exception:
            self.connection.rollback()
            raise

    def _verify_existing_source(self, cursor, source: dict[str, Any], source_id: int, fingerprint: str) -> None:
        cursor.execute(
            f"""SELECT instrument_id, symbol, source_candle_open_at, source_candle_close_at,
                       source_fetch_started_at, source_available_at, detected_at,
                       entry_eligible_at, reference_price, stop_price, target_price,
                       risk_distance, protocol_fingerprint
                FROM {SOURCE_SCHEMA}.source_event WHERE source_event_id=%s""",
            (source_id,),
        )
        row = cursor.fetchone()
        expected = (
            int(source["instrument_id"]), source["symbol"],
            parse_utc(source["source_candle_open_at"]), parse_utc(source["source_candle_close_at"]),
            parse_utc(source["source_fetch_started_at"]), parse_utc(source["source_available_at"]),
            parse_utc(source["detected_at"]), parse_utc(source["entry_eligible_at"]),
            Decimal(str(source["reference_price"])), Decimal(str(source["stop_price"])),
            Decimal(str(source["target_price"])), Decimal(str(source["risk_distance"])), fingerprint,
        )
        actual = (
            int(row[0]), row[1], parse_utc(row[2]), parse_utc(row[3]), parse_utc(row[4]),
            parse_utc(row[5]), parse_utc(row[6]), parse_utc(row[7]), Decimal(str(row[8])),
            Decimal(str(row[9])), Decimal(str(row[10])), Decimal(str(row[11])), row[12],
        )
        if actual != expected:
            raise ValueError("SOURCE_IDENTITY_COLLISION")

    def update_source_entry(self, source_event_id: int, entry: dict[str, Any]) -> None:
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""SELECT source_status, entry_filled_at FROM {SOURCE_SCHEMA}.source_event
                    WHERE source_event_id=%s FOR UPDATE""",
                (source_event_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("SOURCE_NOT_FOUND")
            if row[0] == "FINALIZED":
                raise ValueError("FINALIZED_SOURCE_IMMUTABLE")
            if row[0] not in ("DETECTED", "ENTRY_PENDING"):
                raise ValueError("SOURCE_NOT_ENTRY_PENDING")
            if row[1] is not None:
                raise ValueError("DUPLICATE_ENTRY_UPDATE")
            cursor.execute(
                f"""UPDATE {SOURCE_SCHEMA}.source_event
                    SET source_status='ENTRY_FILLED', coverage_status='PENDING',
                        entry_filled_at=%s, entry_price=%s,
                        stop_price=%s, target_price=%s,
                        risk_distance=%s, baseline_deadline_at=%s
                    WHERE source_event_id=%s
                      AND source_status IN ('DETECTED','ENTRY_PENDING')
                      AND entry_filled_at IS NULL""",
                (
                    entry["entry_filled_at"], entry["entry_price"],
                    entry["stop_price"], entry["target_price"],
                    entry["risk_distance"], entry["baseline_deadline_at"],
                    source_event_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("CONFLICTING_OR_DUPLICATE_ENTRY_UPDATE")
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
        if baseline.get("baseline_exit_reason") == "AMBIGUOUS_STOP_FIRST":
            raise ValueError("AMBIGUOUS_BASELINE_CANNOT_FINALIZE")
        if not can_finalize(evaluations):
            raise ValueError("INCOMPLETE_NONFINAL_OUTCOME")
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""SELECT source_status FROM {SOURCE_SCHEMA}.source_event
                    WHERE source_event_id=%s FOR UPDATE""",
                (source_event_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("SOURCE_NOT_FOUND")
            if row[0] == "FINALIZED":
                raise ValueError("FINALIZED_SOURCE_IMMUTABLE")
            if row[0] != "ENTRY_FILLED":
                raise ValueError("SOURCE_NOT_ENTRY_FILLED")
            cursor.execute(
                f"""SELECT variant_id, variant_status FROM {SOURCE_SCHEMA}.variant_outcome
                    WHERE protocol_version=%s AND source_event_id=%s ORDER BY variant_id FOR UPDATE""",
                (PROTOCOL_VERSION, source_event_id),
            )
            stored = {r[0]: r[1] for r in cursor.fetchall()}
            expected = {v.variant_id for v in VARIANTS}
            if set(stored) != expected:
                raise ValueError("INCOMPLETE_VARIANT_TUPLE")
            if any(status == "FINALIZED" for status in stored.values()):
                raise ValueError("FINALIZED_VARIANT_IMMUTABLE")
            if any(
                row.get("variant_status") not in ("EVALUATED", "BASELINE_EXITED_EARLY")
                or row.get("ambiguous_ordering") is not False
                for row in evaluations
            ):
                raise ValueError("INVALID_FINALIZED_VARIANT_STATUS")
            for evaluation in evaluations:
                cursor.execute(
                    f"""UPDATE {SOURCE_SCHEMA}.variant_outcome
                        SET variant_status=%s, evaluated_at=now(),
                            trigger_result=%s, mae_r=%s,
                            mfe_r=%s, window_low=%s,
                            window_high=%s, horizon_end_at=%s,
                            variant_exit_at=%s,
                            variant_exit_price=%s,
                            variant_exit_reason=%s,
                            variant_gross_r=%s,
                            net_r_normal=%s, net_r_elevated=%s,
                            delta_net_r_normal=%s,
                            delta_net_r_elevated=%s,
                            ambiguous_ordering=%s,
                            coverage_status='COMPLETE', coverage_reason=NULL,
                            execution_price_semantics=%s
                        WHERE protocol_version=%s
                          AND source_event_id=%s
                          AND variant_id=%s
                          AND variant_status<>'FINALIZED'""",
                    (
                        "EVALUATED", evaluation["trigger_result"], evaluation["mae_r"],
                        evaluation["mfe_r"], evaluation["window_low"],
                        evaluation["window_high"], evaluation["horizon_end_at"],
                        evaluation["variant_exit_at"], evaluation["variant_exit_price"],
                        evaluation["variant_exit_reason"], evaluation["variant_gross_r"],
                        evaluation["net_r_normal"], evaluation["net_r_elevated"],
                        evaluation["delta_net_r_normal"], evaluation["delta_net_r_elevated"],
                        evaluation["ambiguous_ordering"], evaluation["execution_price_semantics"],
                        PROTOCOL_VERSION, source_event_id, evaluation["variant_id"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("VARIANT_UPDATE_ROW_COUNT")
            cursor.execute(
                f"""UPDATE {SOURCE_SCHEMA}.source_event
                    SET source_status='FINALIZED', coverage_status='COMPLETE',
                        baseline_exit_at=%s,
                        baseline_exit_price=%s,
                        baseline_exit_reason=%s,
                        baseline_gross_r=%s,
                        baseline_net_r_normal=%s,
                        baseline_net_r_elevated=%s
                    WHERE source_event_id=%s
                      AND source_status='ENTRY_FILLED'""",
                (
                    baseline["baseline_exit_at"], baseline["baseline_exit_price"],
                    baseline["baseline_exit_reason"], baseline["baseline_gross_r"],
                    baseline["baseline_net_r_normal"], baseline["baseline_net_r_elevated"],
                    source_event_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("SOURCE_FINALIZATION_ROW_COUNT")
            for evaluation in evaluations:
                cursor.execute(
                    f"""UPDATE {SOURCE_SCHEMA}.variant_outcome
                        SET variant_status='FINALIZED'
                        WHERE protocol_version=%s AND source_event_id=%s
                          AND variant_id=%s AND variant_status<>'FINALIZED'""",
                    (PROTOCOL_VERSION, source_event_id, evaluation["variant_id"]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("VARIANT_FINALIZATION_ROW_COUNT")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


    def save_ambiguous_outcome(
        self,
        source_event_id: int,
        baseline: dict[str, Any],
        evaluations: list[dict[str, Any]],
    ) -> None:
        """Atomically persist seven diagnostic outcomes without finalization."""
        expected = {v.variant_id for v in VARIANTS}
        actual = [row.get("variant_id") for row in evaluations]

        if len(actual) != 7 or set(actual) != expected:
            raise ValueError("EXPECTED_EXACTLY_SEVEN_VARIANTS")

        if any(
            row.get("variant_status") == "INCOMPLETE_COVERAGE"
            or row.get("coverage_reason") is not None
            for row in evaluations
        ):
            raise ValueError("INCOMPLETE_DIAGNOSTIC_TUPLE")

        allowed = {"EVALUATED", "AMBIGUOUS", "BASELINE_EXITED_EARLY"}
        if any(
            row.get("variant_status") not in allowed
            for row in evaluations
        ):
            raise ValueError("INVALID_DIAGNOSTIC_VARIANT_STATUS")

        if not any(
            row.get("variant_status") == "AMBIGUOUS"
            or row.get("ambiguous_ordering") is True
            for row in evaluations
        ):
            raise ValueError("AMBIGUOUS_OUTCOME_REQUIRED")

        if baseline.get("source_status") != "AMBIGUOUS_NONFINAL":
            raise ValueError("INVALID_DIAGNOSTIC_SOURCE_STATUS")

        if baseline.get("coverage_status") != "COMPLETE":
            raise ValueError("INCOMPLETE_DIAGNOSTIC_BASELINE")

        required = (
            "variant_exit_at",
            "variant_exit_price",
            "variant_gross_r",
            "net_r_normal",
            "net_r_elevated",
            "delta_net_r_normal",
            "delta_net_r_elevated",
            "execution_price_semantics",
        )
        if any(
            any(row.get(field) is None for field in required)
            or type(row.get("ambiguous_ordering")) is not bool
            for row in evaluations
        ):
            raise ValueError("INCOMPLETE_DIAGNOSTIC_TUPLE")

        cursor = self._cursor()
        try:
            cursor.execute(
                f"""SELECT source_status
                    FROM {SOURCE_SCHEMA}.source_event
                    WHERE source_event_id=%s FOR UPDATE""",
                (source_event_id,),
            )
            source = cursor.fetchone()
            if source is None or source[0] != "ENTRY_FILLED":
                raise ValueError("DIAGNOSTIC_SOURCE_NOT_ENTRY_FILLED")

            cursor.execute(
                f"""SELECT variant_id, variant_status
                    FROM {SOURCE_SCHEMA}.variant_outcome
                    WHERE protocol_version=%s
                      AND source_event_id=%s
                    ORDER BY variant_id FOR UPDATE""",
                (PROTOCOL_VERSION, source_event_id),
            )
            stored = cursor.fetchall()
            if (
                len(stored) != 7
                or {row[0] for row in stored} != expected
                or any(row[1] == "FINALIZED" for row in stored)
            ):
                raise ValueError("DIAGNOSTIC_VARIANT_TUPLE_INVALID")

            for item in evaluations:
                cursor.execute(
                    f"""UPDATE {SOURCE_SCHEMA}.variant_outcome
                        SET variant_status=%s,
                            evaluated_at=now(),
                            trigger_result=%s,
                            mae_r=%s, mfe_r=%s,
                            window_low=%s, window_high=%s,
                            horizon_end_at=%s,
                            variant_exit_at=%s,
                            variant_exit_price=%s,
                            variant_exit_reason=%s,
                            variant_gross_r=%s,
                            net_r_normal=%s,
                            net_r_elevated=%s,
                            delta_net_r_normal=%s,
                            delta_net_r_elevated=%s,
                            ambiguous_ordering=%s,
                            coverage_status='COMPLETE',
                            coverage_reason=NULL,
                            execution_price_semantics=%s
                        WHERE protocol_version=%s
                          AND source_event_id=%s
                          AND variant_id=%s
                          AND variant_status<>'FINALIZED'""",
                    (
                        item["variant_status"],
                        item["trigger_result"],
                        item["mae_r"],
                        item["mfe_r"],
                        item["window_low"],
                        item["window_high"],
                        item["horizon_end_at"],
                        item["variant_exit_at"],
                        item["variant_exit_price"],
                        item["variant_exit_reason"],
                        item["variant_gross_r"],
                        item["net_r_normal"],
                        item["net_r_elevated"],
                        item["delta_net_r_normal"],
                        item["delta_net_r_elevated"],
                        item["ambiguous_ordering"],
                        item["execution_price_semantics"],
                        PROTOCOL_VERSION,
                        source_event_id,
                        item["variant_id"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("DIAGNOSTIC_VARIANT_UPDATE_ROW_COUNT")

            cursor.execute(
                f"""UPDATE {SOURCE_SCHEMA}.source_event
                    SET source_status='AMBIGUOUS_NONFINAL',
                        coverage_status='COMPLETE',
                        baseline_exit_at=%s,
                        baseline_exit_price=%s,
                        baseline_exit_reason=%s,
                        baseline_gross_r=%s,
                        baseline_net_r_normal=%s,
                        baseline_net_r_elevated=%s
                    WHERE source_event_id=%s
                      AND source_status='ENTRY_FILLED'""",
                (
                    baseline["baseline_exit_at"],
                    baseline["baseline_exit_price"],
                    baseline["baseline_exit_reason"],
                    baseline["baseline_gross_r"],
                    baseline["baseline_net_r_normal"],
                    baseline["baseline_net_r_elevated"],
                    source_event_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("DIAGNOSTIC_SOURCE_UPDATE_ROW_COUNT")

            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def mark_source_not_filled(self, source_event_id: int, reason: str = "ENTRY_NOT_FILLED") -> None:
        self._mark_source(source_event_id, "ENTRY_NOT_FILLED", None, reason)

    def mark_source_incomplete(self, source_event_id: int, reason: str) -> None:
        self._mark_source(source_event_id, None, "INCOMPLETE", reason)

    def mark_source_invalid(self, source_event_id: int, reason: str) -> None:
        self._mark_source(source_event_id, "INVALID", "INCOMPLETE", reason)

    def _mark_source(
        self,
        source_event_id: int,
        status: str | None,
        coverage: str | None,
        reason: str,
    ) -> None:
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""SELECT source_status FROM {SOURCE_SCHEMA}.source_event
                    WHERE source_event_id=%s FOR UPDATE""",
                (source_event_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("SOURCE_STATE_IMMUTABLE_OR_TERMINAL")

            current_state = row[0]
            coverage_only = status is None and coverage == "INCOMPLETE"

            allowed = current_state in ("DETECTED", "ENTRY_PENDING")
            if current_state == "ENTRY_FILLED" and coverage_only:
                allowed = True

            if not allowed:
                raise ValueError("SOURCE_STATE_IMMUTABLE_OR_TERMINAL")

            allowed_sql_states = (
                ("DETECTED", "ENTRY_PENDING", "ENTRY_FILLED")
                if coverage_only
                else ("DETECTED", "ENTRY_PENDING")
            )

            cursor.execute(
                f"""UPDATE {SOURCE_SCHEMA}.source_event
                    SET source_status=COALESCE(%s, source_status),
                        coverage_status=COALESCE(%s, coverage_status),
                        invalid_reason=%s
                    WHERE source_event_id=%s
                      AND source_status IN ({",".join(["%s"] * len(allowed_sql_states))})""",
                (
                    status,
                    coverage,
                    reason,
                    source_event_id,
                    *allowed_sql_states,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("SOURCE_STATE_TRANSITION_REJECTED")
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
                       stop_price, target_price, baseline_deadline_at, source_status,
                       entry_eligible_at, detected_at
                FROM {SOURCE_SCHEMA}.source_event
                WHERE protocol_version=%s AND source_status IN
                      ('DETECTED','ENTRY_PENDING','ENTRY_FILLED') ORDER BY detected_at""",
            (PROTOCOL_VERSION,),
        )
        return [
            {
                "source_event_id": int(r[0]), "instrument_id": int(r[1]),
                "entry_filled_at": r[2], "entry_price": float(r[3]) if r[3] is not None else None,
                "stop_price": float(r[4]) if r[4] is not None else None,
                "target_price": float(r[5]) if r[5] is not None else None,
                "baseline_deadline_at": r[6], "source_status": r[7],
                "entry_eligible_at": r[8], "detected_at": r[9],
            }
            for r in cursor.fetchall()
        ]


PROTOCOL_LOCK_KEY = 732188603
