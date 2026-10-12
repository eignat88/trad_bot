"""Durable PostgreSQL persistence for the V2 prospective research pipeline."""
from __future__ import annotations

import json
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any

from .lifecycle import can_finalize_source
from .protocol import (
    calculate_protocol_fingerprint,
    ACTIVATION_AT,
    BASELINE_MAX_HOLD_MINUTES,
    BASELINE_POLICY,
    CANDLE_SOURCE_VERSION,
    ENTRY_EXPIRY_MINUTES,
    EXPERIMENT_ID,
    INTRABAR_POLICY,
    PROTOCOL_VERSION,
    SCANNER_NAME,
    SCANNER_VERSION,
    SLIPPAGE_PER_SIDE,
    SOURCE_DIRECTION,
    SOURCE_SCHEMA,
    TAKER_FEE_PER_SIDE,
    VARIANTS,
    protocol_payload,
    require_protocol_fingerprint,
    validate_source_geometry,
    FROZEN_AT,
    validate_activation_freeze,
)


class _NamedParameterCursor:
    """Translate named DBAPI placeholders to positional placeholders."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        import re

        is_pg8000 = type(self._cursor).__module__.startswith("pg8000.")

        if is_pg8000 and isinstance(params, dict) and "%(" in sql:
            pattern = r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s"
            names = re.findall(pattern, sql)

            if not names:
                raise ValueError("Named SQL parameters could not be parsed")

            missing = set(names) - set(params)
            if missing:
                raise ValueError(
                    f"Missing SQL parameters: {sorted(missing)}"
                )

            sql = re.sub(pattern, "%s", sql)
            params = tuple(params[name] for name in names)

        if params is None:
            return self._cursor.execute(sql)

        return self._cursor.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class MeEarlyMaeV2Repository:
    """Repository for source, virtual-fill and variant lifecycle persistence.

    The repository never references ``dds.paper_trade`` and never invokes
    production trading APIs. Each operation owns its transaction and commits
    only after all related rows are durably written.
    """

    def __init__(self, connection: Any) -> None:
        self._conn = connection

    def _cursor(self):
        return _NamedParameterCursor(self._conn.cursor())

    def ensure_instrument(self, symbol: str) -> int | None:
        """Read-only lookup; never insert into production instrument table."""
        cursor = self._cursor()
        try:
            cursor.execute(
                "SELECT instrument_id FROM dds.instrument WHERE symbol = %s",
                (symbol,),
            )
            row = cursor.fetchone()
            self._conn.rollback()
            return int(row[0]) if row else None
        except Exception:
            self._conn.rollback()
            raise

    def register_protocol(self) -> None:
        """Atomically register and verify the immutable V2 protocol."""
        validate_activation_freeze(
            FROZEN_AT, ACTIVATION_AT, datetime.now(timezone.utc)
        )
        cursor = self._cursor()
        payload = protocol_payload(FROZEN_AT, ACTIVATION_AT)
        fingerprint = calculate_protocol_fingerprint(payload)

        try:
            cursor.execute(
                f"""
                INSERT INTO {SOURCE_SCHEMA}.protocol (
                    protocol_version, experiment_id, scanner_name, scanner_version,
                    direction, frozen_at, activation_at, entry_rule,
                    entry_expiry_minutes, baseline_policy, baseline_max_hold_minutes,
                    taker_fee_per_side, slippage_per_side, intrabar_policy,
                    protocol_json
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                )
                ON CONFLICT (protocol_version) DO NOTHING
                RETURNING protocol_version
                """,
                (
                    PROTOCOL_VERSION,
                    EXPERIMENT_ID,
                    SCANNER_NAME,
                    SCANNER_VERSION,
                    SOURCE_DIRECTION,
                    FROZEN_AT,
                    ACTIVATION_AT,
                    payload["entry_rule"],
                    ENTRY_EXPIRY_MINUTES,
                    BASELINE_POLICY,
                    BASELINE_MAX_HOLD_MINUTES,
                    TAKER_FEE_PER_SIDE,
                    SLIPPAGE_PER_SIDE,
                    INTRABAR_POLICY,
                    json.dumps(payload),
                ),
            )

            protocol_created = cursor.fetchone() is not None

            cursor.execute(
                f"""
                SELECT experiment_id, scanner_name, scanner_version,
                       direction, frozen_at, activation_at, entry_rule,
                       entry_expiry_minutes, baseline_policy,
                       baseline_max_hold_minutes, taker_fee_per_side,
                       slippage_per_side, intrabar_policy, protocol_json
                FROM {SOURCE_SCHEMA}.protocol
                WHERE protocol_version = %s
                FOR UPDATE
                """,
                (PROTOCOL_VERSION,),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("ME V2 protocol registration missing")

            from decimal import Decimal

            expected = (
                EXPERIMENT_ID, SCANNER_NAME, SCANNER_VERSION,
                SOURCE_DIRECTION, FROZEN_AT, ACTIVATION_AT,
                payload["entry_rule"],
                ENTRY_EXPIRY_MINUTES, BASELINE_POLICY,
                BASELINE_MAX_HOLD_MINUTES,
                Decimal(str(TAKER_FEE_PER_SIDE)),
                Decimal(str(SLIPPAGE_PER_SIDE)),
                INTRABAR_POLICY,
            )

            actual = tuple(
                Decimal(str(value)) if index in (10, 11) else value
                for index, value in enumerate(row[:13])
            )

            if actual != expected:
                raise RuntimeError(
                    "ME V2 FROZEN_PROTOCOL_MISMATCH: protocol columns"
                )

            stored_json = row[13]
            if isinstance(stored_json, str):
                stored_json = json.loads(stored_json)

            if stored_json != payload:
                raise RuntimeError(
                    "ME V2 FROZEN_PROTOCOL_MISMATCH: protocol_json"
                )

            if protocol_created:
                for variant in VARIANTS:
                        cursor.execute(
                        f"""
                        INSERT INTO {SOURCE_SCHEMA}.variant (
                            protocol_version, variant_id, horizon_minutes,
                            threshold_mae_r, is_primary
                        ) VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (protocol_version, variant_id) DO NOTHING
                        """,
                        (
                            PROTOCOL_VERSION, variant.variant_id,
                            variant.horizon_minutes, variant.threshold_mae_r,
                            variant.is_primary,
                        ),
                    )

            cursor.execute(
                f"""
                SELECT variant_id, horizon_minutes,
                       threshold_mae_r, is_primary
                FROM {SOURCE_SCHEMA}.variant
                WHERE protocol_version = %s
                ORDER BY variant_id
                """,
                (PROTOCOL_VERSION,),
            )

            stored = [
                (variant_id, int(horizon), Decimal(str(threshold)), bool(primary))
                for variant_id, horizon, threshold, primary in cursor.fetchall()
            ]
            expected_variants = sorted([
                (
                    v.variant_id,
                    v.horizon_minutes,
                    Decimal(str(v.threshold_mae_r)),
                    v.is_primary,
                )
                for v in VARIANTS
            ])

            if stored != expected_variants:
                raise RuntimeError(
                    "ME V2 FROZEN_PROTOCOL_MISMATCH: variant definitions"
                )

            self._conn.commit()

        except Exception:
            self._conn.rollback()
            raise

    def insert_source(self, source: dict[str, Any]) -> int | None:
        """Persist one natural source event idempotently.

        Duplicate identities return ``None`` and do not mutate the existing
        row. Invalid geometry is stored as a separate INVALID record.
        """
        validate_activation_freeze(
            FROZEN_AT, ACTIVATION_AT, datetime.now(timezone.utc)
        )
        valid_geometry, invalid_reason = validate_source_geometry(
            float(source["entry_zone_low"]),
            float(source["entry_zone_high"]),
            float(source["invalidation_price"]),
            source.get("target_1"),
        )
        status = "INVALID" if not valid_geometry else "DETECTED"
        eligibility = "INVALID" if not valid_geometry else (
            "PRE_FREEZE" if source["detected_at"] <= ACTIVATION_AT else "PENDING"
        )
        if eligibility == "PRE_FREEZE":
            status = "INVALID"
            invalid_reason = "PRE_FREEZE"
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""
                INSERT INTO {SOURCE_SCHEMA}.source_event (
                    protocol_version, source_setup_id, instrument_id, symbol,
                    scanner_name, scanner_version, direction, detected_at,
                    signal_candle_open_at, entry_zone_low, entry_zone_high,
                    reference_price, invalidation_price, target_price,
                    source_features, source_status, invalid_reason, eligibility,
                    protocol_fingerprint
                ) VALUES (
                    %(protocol_version)s, %(source_setup_id)s, %(instrument_id)s, %(symbol)s,
                    %(scanner_name)s, %(scanner_version)s, %(direction)s, %(detected_at)s,
                    %(signal_candle_open_at)s, %(entry_zone_low)s, %(entry_zone_high)s,
                    %(reference_price)s, %(invalidation_price)s, %(target_price)s,
                    %(source_features)s::jsonb, %(source_status)s, %(invalid_reason)s,
                    %(eligibility)s, %(protocol_fingerprint)s
                )
                ON CONFLICT (protocol_version, source_setup_id) DO NOTHING
                RETURNING source_event_id
                """,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "source_setup_id": str(source["source_setup_id"]),
                    "instrument_id": int(source["instrument_id"]),
                    "symbol": source["symbol"],
                    "scanner_name": SCANNER_NAME,
                    "scanner_version": SCANNER_VERSION,
                    "direction": SOURCE_DIRECTION,
                    "detected_at": source["detected_at"],
                    "signal_candle_open_at": source["signal_candle_open_at"],
                    "entry_zone_low": float(source["entry_zone_low"]),
                    "entry_zone_high": float(source["entry_zone_high"]),
                    "reference_price": float(source["reference_price"]),
                    "invalidation_price": float(source["invalidation_price"]),
                    "target_price": float(source["target_1"]),
                    "source_features": json.dumps(source.get("features") or {}),
                    "source_status": status,
                    "invalid_reason": invalid_reason,
                    "eligibility": eligibility,
                    "protocol_fingerprint": require_protocol_fingerprint(FROZEN_AT, ACTIVATION_AT),
                },
            )
            row = cursor.fetchone()
            self._conn.commit()
            return int(row[0]) if row else None
        except Exception:
            self._conn.rollback()
            raise

    def insert_source_with_variants(self, source: dict[str, Any]) -> int | None:
        """Persist one natural source event idempotently.

        Register source and all seven variants in one transaction.

        An identical source replay repairs missing variant rows without
        overwriting existing outcomes. A conflicting identity is rejected.
        """
        validate_activation_freeze(
            FROZEN_AT, ACTIVATION_AT, datetime.now(timezone.utc)
        )
        valid_geometry, invalid_reason = validate_source_geometry(
            float(source["entry_zone_low"]),
            float(source["entry_zone_high"]),
            float(source["invalidation_price"]),
            source.get("target_1"),
        )
        status = "INVALID" if not valid_geometry else "DETECTED"
        eligibility = "INVALID" if not valid_geometry else (
            "PRE_FREEZE" if source["detected_at"] <= ACTIVATION_AT else "PENDING"
        )
        if eligibility == "PRE_FREEZE":
            status = "INVALID"
            invalid_reason = "PRE_FREEZE"
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""
                INSERT INTO {SOURCE_SCHEMA}.source_event (
                    protocol_version, source_setup_id, instrument_id, symbol,
                    scanner_name, scanner_version, direction, detected_at,
                    signal_candle_open_at, entry_zone_low, entry_zone_high,
                    reference_price, invalidation_price, target_price,
                    source_features, source_status, invalid_reason, eligibility,
                    protocol_fingerprint
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s::jsonb, %s, %s,
                    %s, %s
                )
                ON CONFLICT (protocol_version, source_setup_id) DO NOTHING
                RETURNING source_event_id
                """,
                tuple({
                    "protocol_version": PROTOCOL_VERSION,
                    "source_setup_id": str(source["source_setup_id"]),
                    "instrument_id": int(source["instrument_id"]),
                    "symbol": source["symbol"],
                    "scanner_name": SCANNER_NAME,
                    "scanner_version": SCANNER_VERSION,
                    "direction": SOURCE_DIRECTION,
                    "detected_at": source["detected_at"],
                    "signal_candle_open_at": source["signal_candle_open_at"],
                    "entry_zone_low": float(source["entry_zone_low"]),
                    "entry_zone_high": float(source["entry_zone_high"]),
                    "reference_price": float(source["reference_price"]),
                    "invalidation_price": float(source["invalidation_price"]),
                    "target_price": float(source["target_1"]),
                    "source_features": json.dumps(source.get("features") or {}),
                    "source_status": status,
                    "invalid_reason": invalid_reason,
                    "eligibility": eligibility,
                    "protocol_fingerprint": require_protocol_fingerprint(FROZEN_AT, ACTIVATION_AT),
                }[key] for key in (
                    "protocol_version", "source_setup_id", "instrument_id",
                    "symbol", "scanner_name", "scanner_version", "direction",
                    "detected_at", "signal_candle_open_at", "entry_zone_low",
                    "entry_zone_high", "reference_price", "invalidation_price",
                    "target_price", "source_features", "source_status",
                    "invalid_reason", "eligibility", "protocol_fingerprint",
                )),
            )
            row = cursor.fetchone()

            if row:
                source_id = int(row[0])
            else:
                cursor.execute(
                    f"""
                    SELECT source_event_id, instrument_id, symbol,
                           scanner_name, scanner_version, direction,
                           detected_at, signal_candle_open_at,
                           entry_zone_low, entry_zone_high,
                           reference_price, invalidation_price, target_price,
                           protocol_fingerprint
                    FROM {SOURCE_SCHEMA}.source_event
                    WHERE protocol_version = %s AND source_setup_id = %s
                    FOR UPDATE
                    """,
                    (PROTOCOL_VERSION, str(source["source_setup_id"])),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise ValueError(
                        "SOURCE_IDENTITY_COLLISION: distinct setup ID on "
                        "the same instrument/signal candle"
                    )

                expected = (
                    int(source["instrument_id"]),
                    source["symbol"],
                    SCANNER_NAME,
                    SCANNER_VERSION,
                    SOURCE_DIRECTION,
                    source["detected_at"],
                    source["signal_candle_open_at"],
                    float(source["entry_zone_low"]),
                    float(source["entry_zone_high"]),
                    float(source["reference_price"]),
                    float(source["invalidation_price"]),
                    float(source["target_1"]),
                    require_protocol_fingerprint(FROZEN_AT, ACTIVATION_AT),
                )

                actual = tuple(existing[1:])
                numeric_positions = {7, 8, 9, 10, 11}

                def same_value(index, actual_value, expected_value):
                    if index in numeric_positions:
                        return Decimal(str(actual_value)) == Decimal(
                            str(expected_value)
                        )
                    return actual_value == expected_value

                if len(actual) != len(expected) or any(
                    not same_value(i, a, b)
                    for i, (a, b) in enumerate(zip(actual, expected))
                ):
                    raise ValueError(
                        "SOURCE_IDENTITY_DRIFT: duplicate setup ID "
                        "with changed frozen source geometry or timestamp"
                    )

                source_id = int(existing[0])

            for variant in VARIANTS:
                cursor.execute(
                    f"""
                    INSERT INTO {SOURCE_SCHEMA}.variant_outcome (
                        protocol_version, source_event_id, variant_id,
                        horizon_minutes, threshold_mae_r,
                        candle_source_version
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (
                        protocol_version, source_event_id, variant_id
                    ) DO NOTHING
                    """,
                    (
                        PROTOCOL_VERSION,
                        source_id,
                        variant.variant_id,
                        variant.horizon_minutes,
                        variant.threshold_mae_r,
                        CANDLE_SOURCE_VERSION,
                    ),
                )

            cursor.execute(
                f"""
                SELECT variant_id, horizon_minutes, threshold_mae_r,
                       candle_source_version
                FROM {SOURCE_SCHEMA}.variant_outcome
                WHERE protocol_version = %s AND source_event_id = %s
                """,
                (PROTOCOL_VERSION, source_id),
            )
            rows = cursor.fetchall()

            expected_variants = {
                v.variant_id: v for v in VARIANTS
            }
            if len(rows) != len(expected_variants):
                raise ValueError(
                    "INCOMPLETE_VARIANT_REGISTRATION: expected seven variants"
                )

            for variant_id, horizon, threshold, candle_version in rows:
                definition = expected_variants.get(variant_id)
                if (
                    definition is None
                    or int(horizon) != definition.horizon_minutes
                    or float(threshold) != definition.threshold_mae_r
                    or candle_version != CANDLE_SOURCE_VERSION
                ):
                    raise ValueError(
                        "VARIANT_PROTOCOL_DRIFT: "
                        f"source={source_id}, variant={variant_id}"
                    )

            self._conn.commit()
            return source_id

        except Exception:
            self._conn.rollback()
            raise

    def ensure_variants(self, source_event_id: int) -> None:
        """Create all seven variant rows atomically for an eligible source."""
        cursor = self._cursor()
        try:
            for variant in VARIANTS:
                cursor.execute(
                    f"""
                    INSERT INTO {SOURCE_SCHEMA}.variant_outcome (
                        protocol_version, source_event_id, variant_id,
                        horizon_minutes, threshold_mae_r, candle_source_version
                    ) VALUES (%(protocol_version)s, %(source_event_id)s, %(variant_id)s,
                              %(horizon_minutes)s, %(threshold_mae_r)s, %(candle_source_version)s)
                    ON CONFLICT (protocol_version, source_event_id, variant_id) DO NOTHING
                    """,
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "source_event_id": source_event_id,
                        "variant_id": variant.variant_id,
                        "horizon_minutes": variant.horizon_minutes,
                        "threshold_mae_r": variant.threshold_mae_r,
                        "candle_source_version": CANDLE_SOURCE_VERSION,
                    },
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def update_source_entry(self, source_event_id: int, entry: dict[str, Any]) -> None:
        """Persist one deterministic virtual entry atomically."""
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""
                UPDATE {SOURCE_SCHEMA}.source_event
                SET source_status = 'ENTRY_FILLED',
                    eligibility = 'ELIGIBLE',
                    entry_pending_at = %(entry_pending_at)s,
                    entry_filled_at = %(entry_filled_at)s,
                    entry_price = %(entry_price)s,
                    virtual_stop_price = %(virtual_stop_price)s,
                    virtual_target_price = %(virtual_target_price)s,
                    risk_distance = %(risk_distance)s,
                    source_coverage = 'PENDING'
                WHERE source_event_id = %(source_event_id)s
                  AND source_status IN ('DETECTED', 'ENTRY_PENDING')
                  AND eligibility = 'PENDING'
                """,
                {"source_event_id": source_event_id, **entry},
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def mark_source_not_filled(self, source_event_id: int, reason: str = "ENTRY_NOT_FILLED") -> None:
        """Mark an expired source without touching any production table."""
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""
                UPDATE {SOURCE_SCHEMA}.source_event
                SET source_status = 'ENTRY_NOT_FILLED', invalid_reason = %(reason)s
                WHERE source_event_id = %(source_event_id)s
                  AND source_status IN ('DETECTED', 'ENTRY_PENDING')
                """,
                {"source_event_id": source_event_id, "reason": reason},
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def save_variant_evaluation(self, source_event_id: int, evaluation: dict[str, Any]) -> None:
        self.save_variant_batch(source_event_id, [evaluation])

    def save_baseline(self, source_event_id: int, baseline: dict[str, Any]) -> None:
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""
                UPDATE {SOURCE_SCHEMA}.source_event
                SET baseline_exit_at = %(exit_at)s,
                    baseline_exit_price = %(exit_price)s,
                    baseline_exit_reason = %(exit_reason)s,
                    baseline_gross_r = %(gross_r)s,
                    baseline_net_r = %(net_r)s,
                    source_coverage = %(coverage)s
                WHERE source_event_id = %(source_event_id)s
                """,
                {"source_event_id": source_event_id, **baseline},
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def save_variant_batch(
        self,
        source_event_id: int,
        evaluations: list[dict[str, Any]],
    ) -> None:
        """Atomically persist exactly seven non-terminal diagnostic outcomes."""
        expected = {variant.variant_id for variant in VARIANTS}
        actual = [row["variant_id"] for row in evaluations]

        if len(actual) != len(expected) or set(actual) != expected:
            raise ValueError("Expected exactly seven unique frozen variants")

        if any(
            row.get("variant_status") != "INCOMPLETE_COVERAGE"
            or row.get("trigger_result") != "INCOMPLETE_COVERAGE"
            or row.get("variant_exit_at") is not None
            or row.get("variant_net_r") is not None
            or row.get("delta_net_r") is not None
            for row in evaluations
        ):
            raise ValueError("Diagnostic batch requires INCOMPLETE_COVERAGE only")

        cursor = self._cursor()
        try:
            cursor.execute(
                f"""
                SELECT source_status
                FROM {SOURCE_SCHEMA}.source_event
                WHERE source_event_id = %(source_event_id)s
                  AND protocol_version = %(protocol_version)s
                FOR UPDATE
                """,
                {
                    "source_event_id": source_event_id,
                    "protocol_version": PROTOCOL_VERSION,
                },
            )
            source = cursor.fetchone()
            if source is None or source[0] != "ENTRY_FILLED":
                raise ValueError("Source is not in ENTRY_FILLED state")

            cursor.execute(
                f"""
                SELECT variant_id, variant_status
                FROM {SOURCE_SCHEMA}.variant_outcome
                WHERE source_event_id = %(source_event_id)s
                  AND protocol_version = %(protocol_version)s
                FOR UPDATE
                """,
                {
                    "source_event_id": source_event_id,
                    "protocol_version": PROTOCOL_VERSION,
                },
            )
            stored = dict(cursor.fetchall())

            if len(stored) != len(expected) or set(stored) != expected:
                raise ValueError("Incomplete frozen variant tuple")

            if any(status == "FINALIZED" for status in stored.values()):
                raise ValueError("Finalized variant cannot be overwritten")

            protected = {
                "AMBIGUOUS",
                "EVALUATED_TRIGGERED",
                "EVALUATED_NO_TRIGGER",
                "BASELINE_EXITED_EARLY",
            }

            for evaluation in evaluations:
                if stored[evaluation["variant_id"]] in protected:
                    continue

                cursor.execute(
                    f"""
                    UPDATE {SOURCE_SCHEMA}.variant_outcome
                    SET mae_r = %(mae_r)s,
                        mfe_r = %(mfe_r)s,
                        window_low = %(window_low)s,
                        window_high = %(window_high)s,
                        trigger_result = %(trigger_result)s,
                        variant_exit_at = %(variant_exit_at)s,
                        variant_exit_price = %(variant_exit_price)s,
                        variant_exit_reason = %(variant_exit_reason)s,
                        variant_gross_r = %(variant_gross_r)s,
                        variant_net_r = %(variant_net_r)s,
                        delta_net_r = %(delta_net_r)s,
                        ambiguous_ordering = %(ambiguous_ordering)s,
                        coverage_reason = %(coverage_reason)s,
                        variant_status = %(variant_status)s
                    WHERE protocol_version = %(protocol_version)s
                      AND source_event_id = %(source_event_id)s
                      AND variant_id = %(variant_id)s
                      AND variant_status NOT IN ('FINALIZED', 'AMBIGUOUS')
                    """,
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "source_event_id": source_event_id,
                        **evaluation,
                    },
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "Variant update affected unexpected row count: "
                        + evaluation["variant_id"]
                    )

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def save_paired_outcome(
        self,
        source_event_id: int,
        baseline: dict[str, Any],
        evaluations: list[dict[str, Any]],
    ) -> None:
        """Atomically persist baseline and exactly seven paired outcomes."""
        expected = {variant.variant_id for variant in VARIANTS}
        actual = [row["variant_id"] for row in evaluations]

        if len(actual) != len(expected) or set(actual) != expected:
            raise ValueError("Expected exactly seven unique frozen variants")

        cursor = self._cursor()
        try:
            cursor.execute(
                f"""
                SELECT source_status
                FROM {SOURCE_SCHEMA}.source_event
                WHERE source_event_id = %(source_event_id)s
                  AND protocol_version = %(protocol_version)s
                FOR UPDATE
                """,
                {
                    "source_event_id": source_event_id,
                    "protocol_version": PROTOCOL_VERSION,
                },
            )
            row = cursor.fetchone()
            if row is None or row[0] != "ENTRY_FILLED":
                raise ValueError("Source is not in ENTRY_FILLED state")

            cursor.execute(
                f"""
                SELECT variant_id, variant_status,
                       mae_r, mfe_r, window_low, window_high,
                       trigger_result, variant_exit_at, variant_exit_price,
                       variant_exit_reason, variant_gross_r, variant_net_r,
                       delta_net_r, ambiguous_ordering, coverage_reason
                FROM {SOURCE_SCHEMA}.variant_outcome
                WHERE source_event_id = %(source_event_id)s
                  AND protocol_version = %(protocol_version)s
                FOR UPDATE
                """,
                {
                    "source_event_id": source_event_id,
                    "protocol_version": PROTOCOL_VERSION,
                },
            )
            existing = cursor.fetchall()

            if len(existing) != 7 or {r[0] for r in existing} != expected:
                raise ValueError("Incomplete frozen variant tuple")

            if any(r[1] == "FINALIZED" for r in existing):
                raise ValueError("Finalized variant cannot be overwritten")

            columns = (
                "variant_id", "variant_status", "mae_r", "mfe_r",
                "window_low", "window_high", "trigger_result",
                "variant_exit_at", "variant_exit_price",
                "variant_exit_reason", "variant_gross_r",
                "variant_net_r", "delta_net_r",
                "ambiguous_ordering", "coverage_reason",
            )
            stored = {
                row[0]: dict(zip(columns, row, strict=True))
                for row in existing
            }

            from decimal import Decimal
            from .candles import parse_utc

            numeric = {
                "mae_r", "mfe_r", "window_low", "window_high",
                "variant_exit_price", "variant_gross_r",
                "variant_net_r", "delta_net_r",
            }

            def same_value(key, left, right):
                if left is None or right is None:
                    return left is None and right is None
                if key in numeric:
                    return Decimal(str(left)) == Decimal(str(right))
                if key == "variant_exit_at":
                    return parse_utc(left) == parse_utc(right)
                return left == right

            for evaluation in evaluations:
                previous = stored[evaluation["variant_id"]]
                if previous["variant_status"] != "AMBIGUOUS":
                    continue
                if not all(
                    same_value(key, previous[key], evaluation[key])
                    for key in columns if key != "variant_id"
                ):
                    raise ValueError(
                        "Ambiguous outcome mismatch: " + evaluation["variant_id"]
                    )

            cursor.execute(
                f"""
                UPDATE {SOURCE_SCHEMA}.source_event
                SET baseline_exit_at = %(exit_at)s,
                    baseline_exit_price = %(exit_price)s,
                    baseline_exit_reason = %(exit_reason)s,
                    baseline_gross_r = %(gross_r)s,
                    baseline_net_r = %(net_r)s,
                    source_coverage = %(coverage)s
                WHERE source_event_id = %(source_event_id)s
                  AND protocol_version = %(protocol_version)s
                  AND source_status = 'ENTRY_FILLED'
                """,
                {
                    "source_event_id": source_event_id,
                    "protocol_version": PROTOCOL_VERSION,
                    **baseline,
                },
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Baseline update affected unexpected row count")

            for evaluation in evaluations:
                if stored[evaluation["variant_id"]]["variant_status"] == "AMBIGUOUS":
                    continue
                cursor.execute(
                    f"""
                    UPDATE {SOURCE_SCHEMA}.variant_outcome
                    SET mae_r = %(mae_r)s,
                        mfe_r = %(mfe_r)s,
                        window_low = %(window_low)s,
                        window_high = %(window_high)s,
                        trigger_result = %(trigger_result)s,
                        variant_exit_at = %(variant_exit_at)s,
                        variant_exit_price = %(variant_exit_price)s,
                        variant_exit_reason = %(variant_exit_reason)s,
                        variant_gross_r = %(variant_gross_r)s,
                        variant_net_r = %(variant_net_r)s,
                        delta_net_r = %(delta_net_r)s,
                        ambiguous_ordering = %(ambiguous_ordering)s,
                        coverage_reason = %(coverage_reason)s,
                        variant_status = %(variant_status)s
                    WHERE protocol_version = %(protocol_version)s
                      AND source_event_id = %(source_event_id)s
                      AND variant_id = %(variant_id)s
                      AND variant_status NOT IN ('FINALIZED', 'AMBIGUOUS')
                    """,
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "source_event_id": source_event_id,
                        **evaluation,
                    },
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "Variant update affected unexpected row count: "
                        + evaluation["variant_id"]
                    )

            if can_finalize_source(evaluations):
                cursor.execute(
                    f"""
                    UPDATE {SOURCE_SCHEMA}.source_event
                    SET source_status = 'FINALIZED'
                    WHERE source_event_id = %(source_event_id)s
                      AND protocol_version = %(protocol_version)s
                      AND source_status = 'ENTRY_FILLED'
                    """,
                    {
                        "source_event_id": source_event_id,
                        "protocol_version": PROTOCOL_VERSION,
                    },
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Source finalization affected unexpected row count")

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
    def load_pending_sources(self) -> list[dict[str, Any]]:
        """Load all non-terminal source records in deterministic order."""
        cursor = self._cursor()
        cursor.execute(
            f"""
            SELECT source_event_id, source_setup_id, instrument_id, symbol,
                   detected_at, signal_candle_open_at, entry_zone_low,
                   entry_zone_high, invalidation_price, target_price,
                   source_status, eligibility, entry_filled_at, entry_price,
                   virtual_stop_price, virtual_target_price, source_features
            FROM {SOURCE_SCHEMA}.source_event
            WHERE protocol_version = %(protocol_version)s
              AND source_status IN ('DETECTED', 'ENTRY_PENDING', 'ENTRY_FILLED')
            ORDER BY detected_at ASC
            """,
            {"protocol_version": PROTOCOL_VERSION},
        )
        rows = cursor.fetchall()
        return [
            {
                "source_event_id": int(row[0]),
                "source_setup_id": row[1],
                "instrument_id": int(row[2]),
                "symbol": row[3],
                "detected_at": row[4],
                "signal_candle_open_at": row[5],
                "entry_zone_low": float(row[6]),
                "entry_zone_high": float(row[7]),
                "invalidation_price": float(row[8]),
                "target_price": float(row[9]),
                "source_status": row[10],
                "eligibility": row[11],
                "entry_filled_at": row[12],
                "entry_price": float(row[13]) if row[13] is not None else None,
                "virtual_stop_price": float(row[14]) if row[14] is not None else None,
                "virtual_target_price": float(row[15]) if row[15] is not None else None,
                "features": row[16] if isinstance(row[16], dict) else {},
            }
            for row in rows
        ]

    def fetch_candles(self, instrument_id: int, start: datetime, end: datetime) -> list[Any]:
        """Fetch completed 5m candles strictly inside the requested interval."""
        cursor = self._cursor()
        cursor.execute(
            """
            SELECT open_time, close_time, open, high, low, close
            FROM market.candle
            WHERE instrument_id = %(instrument_id)s
              AND timeframe = '5'
              AND is_closed = TRUE
              AND close_time > %(start)s
              AND close_time <= %(end)s
            ORDER BY open_time ASC
            """,
            {"instrument_id": instrument_id, "start": start, "end": end},
        )
        from .candles import ClosedCandle

        return [
            ClosedCandle(
                open_at=row[0], close_at=row[1], open=float(row[2]),
                high=float(row[3]), low=float(row[4]), close=float(row[5]),
            )
            for row in cursor.fetchall()
        ]
