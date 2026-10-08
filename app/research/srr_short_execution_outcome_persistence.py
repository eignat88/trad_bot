"""Dedicated persistence contract for SRR SHORT execution-R outcomes.

This module intentionally does not use ``ResearchRepository.upsert_outcome``:
that method targets the generic ``research.research_outcome`` table and cannot
represent the frozen execution-R protocol. The writer here targets only the
dedicated SRR table created by migration 061.

Safety model:

- one transaction per observation;
- idempotent insert and non-final refresh;
- a finalized row is immutable;
- concurrent finalization is resolved by a row lock plus conditional updates;
- non-final source/boundary diagnostics are persisted without economics;
- rollback is always performed for failures;
- the complete frozen execution snapshot is stored explicitly.

The writer is intentionally inert unless explicitly constructed by an
evaluator. Production activation is not enabled by this module.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

logger = logging.getLogger(__name__)

SRR_ADAPTER_VERSION = "SRR_SHORT_PROSPECTIVE_EVALUATOR_POLICY_ROUTING_V1"
SRR_FROZEN_POLICY_VERSION = "SRR_SHORT_TIMEOUT_CANDLE_POLICY_V1"

# Optional activation-boundary guard. When a guard is attached, every write
# must pass the boundary check before any SQL is executed. The default writer
# (no guard) preserves the pre-existing behavior used by dry-run wiring.
SRR_WRITER_BOUNDARY_DISABLED = None


def _json_text(value: Any) -> str:
    """Serialize a diagnostics payload with deterministic JSON encoding."""
    if value is None:
        return "{}"
    if isinstance(value, str):
        try:
            normalized = json.loads(value)
        except (TypeError, ValueError):
            return json.dumps({"raw_text": value}, sort_keys=True, separators=(",", ":"))
        return json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"non-finite decimal value: {value!r}")
    return result


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _bool(value: Any) -> bool:
    return bool(value)


@dataclass(frozen=True)
class SrrOutcomeWriteResult:
    observation_id: int
    action: str
    is_final: bool


class SrrOutcomeWriter:
    """Atomic, idempotent writer for the SRR execution outcome table.

    The writer owns its transaction boundary for each write. It never calls
    ``ResearchRepository.upsert_outcome`` and never writes the generic
    ``prospective_outcome`` table. A failed operation always rolls back.
    """

    def __init__(
        self,
        conn: Any,
        *,
        activation_gate: Any = None,
    ) -> None:
        if conn is None:
            raise ValueError("SrrOutcomeWriter requires a PostgreSQL connection")
        self._conn = conn
        self._activation_gate = activation_gate

    def _enforce_activation_boundary(
        self,
        *,
        observation_id: int,
        experiment_id: str,
        observation: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> "SrrOutcomeWriteResult | None":
        """Reject any write that could bypass the activation boundary.

        Returns ``None`` when the write may proceed, otherwise a rejection
        result. The check runs before any SQL statement is issued so that a
        pre-activation observation cannot touch the database at all.
        """
        gate = self._activation_gate
        if gate is None:
            return None
        try:
            gate.assert_writer_allowed()
        except Exception as exc:
            logger.warning(
                "SRR outcome write rejected (activation gate): observation_id=%s error=%s",
                observation_id,
                exc,
            )
            return SrrOutcomeWriteResult(
                observation_id=int(observation_id),
                action="REJECTED_ACTIVATION_GATE",
                is_final=False,
            )

        boundary = gate.boundary
        if boundary is None:
            logger.warning(
                "SRR outcome write rejected (activation boundary missing): observation_id=%s",
                observation_id,
            )
            return SrrOutcomeWriteResult(
                observation_id=int(observation_id),
                action="REJECTED_ACTIVATION_GATE",
                is_final=False,
            )

        signal_time = observation.get("signal_time")
        direction = observation.get("direction")
        obs_experiment_id = observation.get("experiment_id", experiment_id)
        if not boundary.is_eligible(
            experiment_id=obs_experiment_id,
            direction=direction,
            signal_time=signal_time,
        ):
            logger.warning(
                "SRR outcome write rejected (pre-activation): observation_id=%s "
                "experiment_id=%s direction=%s signal_time=%s activation_ts=%s",
                observation_id,
                obs_experiment_id,
                direction,
                signal_time,
                boundary.activation_ts_text,
            )
            return SrrOutcomeWriteResult(
                observation_id=int(observation_id),
                action="REJECTED_PRE_ACTIVATION",
                is_final=False,
            )
        return None

    def write(
        self,
        *,
        observation_id: int,
        experiment_id: str,
        observation: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> SrrOutcomeWriteResult:
        observation_id = int(observation_id)

        rejected = self._enforce_activation_boundary(
            observation_id=observation_id,
            experiment_id=experiment_id,
            observation=observation,
            result=result,
        )
        if rejected is not None:
            return rejected

        is_final = _bool(result.get("finalization_eligible", False))
        status = str(result.get("status", ""))
        reason_code = str(result.get("reason_code", ""))
        if not status or not reason_code:
            raise ValueError("SRR outcome requires status and reason_code")
        if str(result.get("experiment_id", experiment_id)) != experiment_id:
            raise ValueError("SRR outcome result experiment_id does not match")
        if experiment_id != str(observation.get("experiment_id", experiment_id)):
            raise ValueError("SRR outcome experiment_id does not match observation")

        values = self._build_values(
            observation_id=observation_id,
            experiment_id=experiment_id,
            observation=observation,
            result=result,
            is_final=is_final,
        )
        columns, parameters = self._column_parameters(values)

        cursor = None
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT observation_id
                FROM research.srr_short_execution_prospective_outcome
                WHERE observation_id = %s
                FOR UPDATE
                """,
                (observation_id,),
            )
            existing = cursor.fetchone()

            if existing is None:
                cursor.execute(
                    f"""
                    INSERT INTO research.srr_short_execution_prospective_outcome (
                        {', '.join(columns)}
                    ) VALUES (
                        {', '.join(['%s'] * len(columns))}
                    )
                    ON CONFLICT (observation_id) DO NOTHING
                    RETURNING observation_id, is_final
                    """,
                    parameters,
                )
                inserted = cursor.fetchone()
                if inserted is not None:
                    self._conn.commit()
                    return SrrOutcomeWriteResult(observation_id, "INSERTED", is_final)

                # Another transaction inserted the row first.
                cursor.execute(
                    "SELECT observation_id FROM "
                    "research.srr_short_execution_prospective_outcome "
                    "WHERE observation_id = %s FOR UPDATE",
                    (observation_id,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise RuntimeError("SRR outcome missing after INSERT conflict")

            if is_final:
                # A second finalizer must never rewrite the already-final row.
                cursor.execute(
                    """
                    SELECT is_final
                    FROM research.srr_short_execution_prospective_outcome
                    WHERE observation_id = %s
                    """,
                    (observation_id,),
                )
                current = cursor.fetchone()
                if current and _bool(current[0]):
                    self._conn.rollback()
                    return SrrOutcomeWriteResult(observation_id, "ALREADY_FINALIZED", True)
                cursor.execute(
                    """
                    UPDATE research.srr_short_execution_prospective_outcome
                    SET {set_clause}
                    WHERE observation_id = %s
                      AND is_final = FALSE
                    RETURNING observation_id, is_final
                    """.format(set_clause=", ".join(f"{column} = %s" for column in columns)),
                    parameters + [observation_id],
                )
                updated = cursor.fetchone()
                if updated:
                    self._conn.commit()
                    return SrrOutcomeWriteResult(observation_id, "FINALIZED", True)
                self._conn.rollback()
                return SrrOutcomeWriteResult(observation_id, "ALREADY_FINALIZED", True)

            # Non-final rows may be refreshed as source evidence improves.
            # Existing finalized rows are protected by the outer row lock and
            # this conditional clause.
            cursor.execute(
                """
                UPDATE research.srr_short_execution_prospective_outcome
                SET {set_clause}
                WHERE observation_id = %s
                  AND is_final = FALSE
                RETURNING observation_id, is_final
                """.format(set_clause=", ".join(f"{column} = %s" for column in columns)),
                parameters + [observation_id],
            )
            updated = cursor.fetchone()
            self._conn.commit()
            if updated:
                return SrrOutcomeWriteResult(observation_id, "REFRESHED", False)
            return SrrOutcomeWriteResult(observation_id, "ALREADY_FINALIZED", True)
        except Exception:
            self._conn.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()

    @staticmethod
    def _column_parameters(values: Mapping[str, Any]) -> tuple[list[str], list[Any]]:
        return list(values), list(values.values())

    @classmethod
    def _build_values(
        cls,
        *,
        observation_id: int,
        experiment_id: str,
        observation: Mapping[str, Any],
        result: Mapping[str, Any],
        is_final: bool,
    ) -> dict[str, Any]:
        # The route/policy result is the authoritative source for economics.
        # The observation tuple is the authoritative frozen input snapshot.
        policy_result = result.get("policy_result") or {}
        source_diagnostics = result.get("source_diagnostics") or {}
        route_diagnostics = result.get("route_diagnostics") or {}
        provenance = policy_result.get("source_provenance") or {}

        # Explicitly require all frozen inputs for a finalizable outcome.
        # For non-final diagnostics these may be absent, but the resulting
        # economics must remain NULL.
        entry = _decimal(observation.get("reference_price"))
        invalidation = _decimal(observation.get("invalidation_price"))
        variant_entry = _decimal(observation.get("variant_entry"))
        variant_stop = _decimal(observation.get("variant_stop"))
        variant_target = _decimal(observation.get("variant_target"))
        structural_r = _decimal(policy_result.get("structural_r"))
        execution_r = _decimal(policy_result.get("execution_r"))
        max_hold_minutes = _int(policy_result.get("max_hold_minutes"))
        intrabar_policy = policy_result.get("intrabar_policy")
        if intrabar_policy is None:
            frozen_features = observation.get("features")
            if isinstance(frozen_features, str):
                frozen_features = json.loads(frozen_features)
            frozen_features = frozen_features or {}
            intrabar_policy = frozen_features.get("_frozen_intrabar_policy")
        coverage_complete = _bool(policy_result.get("coverage_complete", False))

        gross_r = _decimal(result.get("gross_r"))
        cost_r_normal = _decimal(result.get("cost_r_normal"))
        cost_r_elevated = _decimal(result.get("cost_r_elevated"))
        net_r_normal = _decimal(result.get("net_r_normal"))
        net_r_elevated = _decimal(result.get("net_r_elevated"))

        if is_final and not (
            gross_r is not None
            and cost_r_normal is not None
            and cost_r_elevated is not None
            and net_r_normal is not None
            and net_r_elevated is not None
            and structural_r is not None
            and execution_r is not None
            and max_hold_minutes is not None
            and intrabar_policy
            and coverage_complete
        ):
            raise ValueError("final SRR outcome requires complete frozen economics")
        if not is_final and any(
            value is not None
            for value in (
                gross_r,
                cost_r_normal,
                cost_r_elevated,
                net_r_normal,
                net_r_elevated,
            )
        ):
            raise ValueError("non-final SRR outcome cannot contain fabricated economics")

        signal_time = observation.get("signal_time")
        if not isinstance(signal_time, datetime):
            raise ValueError("SRR outcome requires observation signal_time datetime")
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)

        # The policy stores signal_time_ms and cutoff_time_ms, while the
        # observation carries the authoritative signal timestamp. The frozen
        # freeze boundary is carried in the observation features by the
        # observer; the route already rejected pre-freeze rows.
        frozen_features = observation.get("features")
        if isinstance(frozen_features, str):
            frozen_features = json.loads(frozen_features)
        frozen_features = frozen_features or {}
        freeze_ts_text = frozen_features.get("_frozen_freeze_ts")
        if not freeze_ts_text:
            raise ValueError("SRR outcome requires _frozen_freeze_ts in frozen features")
        freeze_ts = datetime.fromisoformat(str(freeze_ts_text).replace("Z", "+00:00"))
        if freeze_ts.tzinfo is None:
            freeze_ts = freeze_ts.replace(tzinfo=timezone.utc)
        if signal_time <= freeze_ts:
            raise ValueError("SRR outcome signal_time must be after freeze boundary")

        timeout_open_ms = _int(policy_result.get("selected_timeout_candle_ms"))
        timeout_close_ms = _int(policy_result.get("selected_timeout_close_ms"))
        timeout_close = _decimal(policy_result.get("selected_timeout_close"))
        return_at_120m = _decimal(policy_result.get("return_at_120m"))

        # The frozen execution snapshot is mandatory for any resolved policy
        # result. A non-final route may carry no geometry at all, but when
        # geometry is present it must be internally complete.
        if structural_r is not None or execution_r is not None:
            if not all(
                value is not None
                for value in (entry, invalidation, variant_entry, variant_stop, variant_target)
            ):
                raise ValueError("partial frozen geometry is not allowed")
            if structural_r is None or structural_r <= 0:
                raise ValueError("structural_r must be positive")
            if execution_r is None or execution_r <= 0:
                raise ValueError("execution_r must be positive")
            if max_hold_minutes is None or not intrabar_policy:
                raise ValueError("partial frozen execution snapshot is not allowed")

        return {
            "observation_id": observation_id,
            "experiment_id": experiment_id,
            "symbol": observation.get("symbol"),
            "direction": observation.get("direction"),
            "signal_time": signal_time,
            "freeze_ts": freeze_ts,
            "status": str(result.get("status", "")),
            "reason_code": str(result.get("reason_code", "")),
            "path_class": result.get("path_class"),
            "is_final": is_final,
            "entry_price": entry,
            "invalidation_price": invalidation,
            "variant_entry": variant_entry,
            "variant_stop": variant_stop,
            "variant_target": variant_target,
            "structural_r": structural_r,
            "execution_r": execution_r,
            "max_hold_minutes": max_hold_minutes,
            "intrabar_policy": intrabar_policy,
            "gross_r": gross_r,
            "cost_r_normal": cost_r_normal,
            "cost_r_elevated": cost_r_elevated,
            "net_r_normal": net_r_normal,
            "net_r_elevated": net_r_elevated,
            "eligible_candle_count": _int(result.get("eligible_candle_count")) or 0,
            "selected_timeout_open_ms": timeout_open_ms,
            "selected_timeout_close_ms": timeout_close_ms,
            "selected_timeout_close": timeout_close,
            "return_at_120m": return_at_120m,
            "signal_boundary_uncertain": _bool(
                policy_result.get("signal_boundary_uncertain", False)
            ),
            "cutoff_boundary_uncertain": _bool(
                policy_result.get("cutoff_boundary_uncertain", False)
            ),
            "coverage_complete": coverage_complete,
            "source_status": str(result.get("source_status", "")),
            "source_confidence": policy_result.get("source_confidence"),
            "source_kind": provenance.get("source_kind"),
            "source_id": provenance.get("source_id"),
            "retrieval_method": provenance.get("retrieval_method"),
            "retrieval_ts_ms": _int(provenance.get("retrieval_ts_ms")),
            "source_notes": provenance.get("notes"),
            "source_diagnostics": _json_text(source_diagnostics),
            "data_quality": _json_text(policy_result.get("data_quality")),
            "route_diagnostics": _json_text(route_diagnostics),
            "evaluation_asof_ms": _int(policy_result.get("evaluation_asof_ms"))
            or _int(result.get("evaluation_asof_ms"))
            or 0,
            "adapter_version": SRR_ADAPTER_VERSION,
            "frozen_policy_version": SRR_FROZEN_POLICY_VERSION,
        }
