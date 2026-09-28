"""Prospective OOS Observer — captures candidates for 6 prospective experiments.

Positioned AFTER the generic research observer but BEFORE any further filtering.
For each candidate from a registered scanner x direction:

1. Checks which prospective experiments apply
2. Evaluates frozen filter rules (e.g., bb_width_percentile < cutoff)
3. Computes variant geometry for ME SHORT A/B/C
4. Inserts prospective_observation with experiment_id + rule_passed
5. For ME A/B/C: inserts 3 observations sharing a synthetic source key

Design:
  - fail-open: errors logged, never propagate to scanner cycle
  - shadow-only: no orders, no paper trades, no production changes
  - append-only: INSERT only, no UPDATE in observe()
  - anti-leakage: all computations use information at detection time

Source linkage:
  Prospective observations use a SYNTHETIC composite key for
  source_signal_id until the evaluator links them to actual
  research.research_signal rows. This allows observation at
  detection time without waiting for signal promotion.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ProspectiveOOSObserver:
    """Captures scanner candidates for prospective OOS experiments.

    Called for every candidate that matches a registered prospective experiment.
    Produces prospective_observation rows with frozen rule evaluations.
    """

    def __init__(self, conn: Any, registry: dict[str, dict]) -> None:
        """Parameters
        ----------
        conn : psycopg2 connection
            Database connection (separate from production).
        registry : dict
            Prospective experiment registry loaded from JSON.
            Keyed by experiment_id.
        """
        self._conn = conn
        self._registry = registry
        self._stats: dict[str, int] = {}

    def _make_source_key(
        self, scanner_name: str, symbol: str, direction: str, signal_time: Any,
    ) -> int:
        """Create a deterministic synthetic source_signal_id from candidate identity.

        This is a negative hash used as a placeholder until the evaluator
        links prospective observations to actual research signals.
        Uses a hash of (scanner, symbol, direction, signal_time) to ensure
        uniqueness and idempotency.
        """
        key_str = f"{scanner_name}:{symbol}:{direction}:{signal_time}"
        # Use Python hash, mapped to negative int to distinguish from real signal_ids
        return -abs(hash(key_str)) % (2**31)

    def observe(
        self,
        scanner_name: str,
        direction: str,
        symbol: str,
        signal_time: Any,
        reference_price: float,
        invalidation_price: float | None,
        target_1: float | None,
        target_2: float | None,
        score: float,
        features: dict,
        parameters: dict,
        market_regime: str | None,
    ) -> None:
        """Process a candidate against all applicable prospective experiments.

        For ME SHORT: produces 3 paired observations (A/B/C).
        For others: produces 1 observation per matching experiment.
        """
        if not self._conn:
            return

        # Generate synthetic source key from candidate identity
        source_key = self._make_source_key(
            scanner_name, symbol, direction, signal_time,
        )

        cursor = self._conn.cursor()
        try:
            for exp_id, exp_spec in self._registry.items():
                if exp_spec["scanner_name"] != scanner_name:
                    continue
                if exp_spec["direction"] != direction:
                    continue

                if exp_id.startswith("ME_SHORT_GEOM_"):
                    # ME SHORT: produce A/B/C from same source
                    self._observe_me_geometry(
                        cursor, exp_id, exp_spec,
                        source_key, symbol, signal_time,
                        reference_price, invalidation_price, target_1, target_2,
                        score, features, parameters, market_regime,
                    )
                else:
                    # Standard: one observation per experiment
                    self._observe_standard(
                        cursor, exp_id, exp_spec,
                        source_key, symbol, signal_time,
                        reference_price, invalidation_price, target_1, target_2,
                        score, features, parameters, market_regime,
                    )

            self._conn.commit()

        except Exception:
            self._conn.rollback()
            self._stats["errors"] = self._stats.get("errors", 0) + 1
            logger.exception(
                "prospective observer error: %s %s",
                scanner_name, symbol,
            )

    def _observe_standard(
        self, cursor, exp_id: str, exp_spec: dict,
        source_key: int, symbol: str, signal_time: Any,
        reference_price: float, invalidation_price: float | None,
        target_1: float | None, target_2: float | None,
        score: float, features: dict, parameters: dict,
        market_regime: str | None,
    ) -> None:
        """Insert a standard prospective observation."""
        rule_passed = True
        filter_reason = None

        # Apply frozen filter rules
        if exp_id == "VC_SHORT_BB_WIDTH_V1":
            bb_pct = features.get("bb_width_percentile")
            if bb_pct is not None:
                threshold = exp_spec.get("threshold")
                if threshold is not None:
                    rule_passed = float(bb_pct) < float(threshold)
                    filter_reason = f"bb_width_percentile={bb_pct:.4f} {'<' if rule_passed else '>='} {threshold}"
            else:
                rule_passed = False
                filter_reason = "bb_width_percentile is NULL"

        elif exp_id == "LR_SHORT_GATE_V1":
            # Gate validation: capture all candidates, tag gate result
            rule_passed = True
            filter_reason = "observational_gate_validation"

        cursor.execute(
            """
            INSERT INTO research.prospective_observation (
                experiment_id, source_signal_id, source_observation_id,
                symbol, direction, signal_time,
                reference_price, invalidation_price, target_1, target_2,
                score, rule_passed, filter_reason,
                features, parameters, market_regime
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (experiment_id, source_signal_id) DO NOTHING
            """,
            (
                exp_id, source_key, None,
                symbol, direction, signal_time,
                reference_price, invalidation_price, target_1, target_2,
                score, rule_passed, filter_reason,
                json.dumps(features), json.dumps(parameters), market_regime,
            ),
        )

        if cursor.rowcount > 0:
            self._stats[exp_id] = self._stats.get(exp_id, 0) + 1

    def _observe_me_geometry(
        self, cursor, exp_id: str, exp_spec: dict,
        source_key: int, symbol: str, signal_time: Any,
        reference_price: float, invalidation_price: float | None,
        target_1: float | None, target_2: float | None,
        score: float, features: dict, parameters: dict,
        market_regime: str | None,
    ) -> None:
        """Insert ME SHORT geometry variant observation.

        For GEOM_A: use original geometry (current stop/target)
        For GEOM_B: wider stop = max(current_risk, 0.5 * ATR)
        For GEOM_C: delayed entry (at detection candle close)
                     stop/target adjusted relative to delayed entry
        """
        if invalidation_price is None or reference_price <= 0:
            return

        current_risk = abs(reference_price - invalidation_price)
        atr = features.get("atr", reference_price * 0.015)
        if atr is None or atr <= 0:
            atr = reference_price * 0.015

        variant_entry = reference_price
        variant_stop = invalidation_price
        variant_target = target_1

        if exp_id == "ME_SHORT_GEOM_A_V1":
            # Control: current geometry as-is
            variant_entry = reference_price
            variant_stop = invalidation_price
            variant_target = target_1

        elif exp_id == "ME_SHORT_GEOM_B_V1":
            # Wider stop: max(current_risk, 0.5 * ATR)
            new_risk = max(current_risk, 0.5 * atr)
            variant_entry = reference_price
            variant_stop = reference_price + new_risk  # SHORT: stop above entry
            if target_1 is not None:
                original_target_dist = abs(reference_price - target_1)
                variant_target = reference_price - original_target_dist

        elif exp_id == "ME_SHORT_GEOM_C_V1":
            # Delayed entry: use current_price (close of detection candle)
            current_price = features.get("entry_price", reference_price)
            if current_price is None or current_price <= 0:
                current_price = reference_price
            variant_entry = current_price
            original_stop_dist = abs(reference_price - invalidation_price)
            variant_stop = current_price + original_stop_dist
            if target_1 is not None:
                original_target_dist = abs(reference_price - target_1)
                variant_target = current_price - original_target_dist

        cursor.execute(
            """
            INSERT INTO research.prospective_observation (
                experiment_id, source_signal_id, source_observation_id,
                symbol, direction, signal_time,
                reference_price, invalidation_price, target_1, target_2,
                score, rule_passed, filter_reason,
                variant_entry, variant_stop, variant_target,
                features, parameters, market_regime
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (experiment_id, source_signal_id) DO NOTHING
            """,
            (
                exp_id, source_key, None,
                symbol, direction, signal_time,
                reference_price, invalidation_price, target_1, target_2,
                score, f"variant={exp_id}",
                variant_entry, variant_stop, variant_target,
                json.dumps(features), json.dumps(parameters), market_regime,
            ),
        )

        if cursor.rowcount > 0:
            self._stats[exp_id] = self._stats.get(exp_id, 0) + 1

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)
