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
        self._conn = conn
        self._registry = registry
        self._stats: dict[str, int] = {}

    def _make_source_key(
        self, scanner_name: str, symbol: str, direction: str, signal_time: Any,
    ) -> int:
        """Deterministic synthetic source_signal_id from candidate identity."""
        key_str = f"{scanner_name}:{symbol}:{direction}:{signal_time}"
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
                    self._observe_me_geometry(
                        cursor, exp_id, exp_spec,
                        source_key, symbol, direction, signal_time,
                        reference_price, invalidation_price, target_1, target_2,
                        score, features, parameters, market_regime,
                    )
                else:
                    self._observe_standard(
                        cursor, exp_id, exp_spec,
                        source_key, symbol, direction, signal_time,
                        reference_price, invalidation_price, target_1, target_2,
                        score, features, parameters, market_regime,
                    )

            self._conn.commit()

        except Exception:
            self._conn.rollback()
            self._stats["errors"] = self._stats.get("errors", 0) + 1
            logger.exception(
                "prospective observer error: %s %s", scanner_name, symbol,
            )

    def _observe_standard(
        self, cursor, exp_id: str, exp_spec: dict,
        source_key: int, symbol: str, direction: str, signal_time: Any,
        reference_price: float, invalidation_price: float | None,
        target_1: float | None, target_2: float | None,
        score: float, features: dict, parameters: dict,
        market_regime: str | None,
    ) -> None:
        """Insert a standard prospective observation."""
        rule_passed = True
        filter_reason = None

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
            rule_passed = True
            filter_reason = "observational_gate_validation"

        elif exp_id == "BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1":
            # This experiment captures candidates rejected by expectancy filter
            # The observation is made BEFORE the expectancy filter in the pipeline
            rule_passed = True
            filter_reason = "expectancy_rejection_oos_capture"
            # Add expectancy data to features for analysis
            features_with_expectancy = dict(features)
            features_with_expectancy["_expectancy_rejection_oos"] = True
            features_with_expectancy["_rejection_reason"] = "profit_factor_below_threshold"
            features = features_with_expectancy

        elif exp_id == "FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1":
            # This experiment captures candidates rejected by expectancy filter
            rule_passed = True
            filter_reason = "expectancy_rejection_oos_capture"
            features_with_expectancy = dict(features)
            features_with_expectancy["_expectancy_rejection_oos"] = True
            features_with_expectancy["_rejection_reason"] = "negative_historical_performance"
            features = features_with_expectancy

        elif exp_id == "TREND_PULLBACK_V3_HIGH_VOL_OOS_V1":
            # This experiment captures candidates rejected by regime filter
            rule_passed = True
            filter_reason = "regime_counterfactual_oos_capture"
            features_with_regime = dict(features)
            features_with_regime["_regime_counterfactual_oos"] = True
            features_with_regime["_actual_regime"] = market_regime
            features_with_regime["_required_regime"] = "TREND_UP"
            features = features_with_regime

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
        source_key: int, symbol: str, direction: str, signal_time: Any,
        reference_price: float, invalidation_price: float | None,
        target_1: float | None, target_2: float | None,
        score: float, features: dict, parameters: dict,
        market_regime: str | None,
    ) -> None:
        """Insert ME SHORT geometry variant observation.

        EXACT FROZEN FORMULAS (all distances are from reference_price):

        GEOM_A (CONTROL):
          entry_time_A    = signal_time (detection time)
          entry_price_A   = reference_price
          stop_A          = invalidation_price (= reference_price * 1.002)
          target_A        = target_1 (= current_price - ATR*2)
          risk_dist_A     = abs(reference_price - invalidation_price)
          target_dist_A   = abs(reference_price - target_1)

        GEOM_B (WIDER STOP):
          entry_time_B    = signal_time (same as A)
          entry_price_B   = reference_price (same as A)
          new_risk        = max(risk_dist_A, 0.5 * ATR_14_5m)
          stop_B          = reference_price + new_risk
          target_B        = reference_price - target_dist_A (SAME target distance as A)
          risk_dist_B     = new_risk (wider than A)
          target_dist_B   = target_dist_A (same as A)

        NOTE: B widens the stop but keeps the same target distance.
        This means B has worse RR than A (wider stop, same target).
        This is intentional: the hypothesis is that wider stop captures
        more favorable moves that A's tight stop misses.

        GEOM_C (DELAYED ENTRY):
          decision_time       = signal_time (when scanner detects)
          detection_candle_close = features["entry_price"] (close of the 5m candle at detection)
          entry_time_C        = detection_candle_close time
          entry_price_C       = detection_candle_close (= features["entry_price"])
          risk_dist_C         = risk_dist_A (same as original)
          target_dist_C       = target_dist_A (same as original)
          stop_C              = entry_price_C + risk_dist_C
          target_C            = entry_price_C - target_dist_C

        GEOM_C is a delayed-entry experiment: C enters at the CLOSE of the
        detection candle, not at detection time. Since detection happens
        during the candle, C's entry is 0-5 minutes later than A's.
        """
        if invalidation_price is None or reference_price <= 0:
            return

        risk_dist_A = abs(reference_price - invalidation_price)
        target_dist_A = abs(reference_price - target_1) if target_1 is not None else 0

        atr = features.get("atr", reference_price * 0.015)
        if atr is None or atr <= 0:
            atr = reference_price * 0.015

        variant_entry = reference_price
        variant_stop = invalidation_price
        variant_target = target_1

        if exp_id == "ME_SHORT_GEOM_A_V1":
            variant_entry = reference_price
            variant_stop = invalidation_price
            variant_target = target_1

        elif exp_id == "ME_SHORT_GEOM_B_V1":
            new_risk = max(risk_dist_A, 0.5 * atr)
            variant_entry = reference_price
            variant_stop = reference_price + new_risk
            # Target distance preserved from A (NOT adjusted for wider stop)
            if target_1 is not None:
                variant_target = reference_price - target_dist_A

        elif exp_id == "ME_SHORT_GEOM_C_V1":
            # Entry at detection candle close (available at detection time)
            current_price = features.get("entry_price", reference_price)
            if current_price is None or current_price <= 0:
                current_price = reference_price
            variant_entry = current_price
            variant_stop = current_price + risk_dist_A
            if target_1 is not None:
                variant_target = current_price - target_dist_A

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
