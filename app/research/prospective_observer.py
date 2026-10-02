"""Prospective OOS Observer — captures candidates for prospective OOS experiments.

Positioned AFTER the generic research observer but BEFORE any further filtering.
For each candidate from a registered scanner x direction:

1. Checks which prospective experiments apply
2. Evaluates frozen filter rules (e.g., bb_width_percentile < cutoff)
3. Computes variant geometry for ME SHORT A/B/C
4. Inserts prospective_observation with experiment_id + rule_passed
5. For ME A/B/C: inserts 3 observations sharing a synthetic source key
6. Immediately promotes each new observation to research.research_signal

Design:
  - fail-open: errors logged, never propagate to scanner cycle
  - shadow-only: no orders, no paper trades, no production changes
  - append-only: INSERT only, no UPDATE in observe()
  - anti-leakage: all computations use information at detection time

Source linkage:
  Each prospective observation is immediately promoted to research_signal
  via the dedicated research repository.  The research_signal row carries
  a prospective_observation_id column linking back to the source.

Persistence:
  The observer also respects terminal DB lifecycle states. A registry entry
  alone is not enough to reactivate an experiment whose DB row is CANCELLED
  or COMPLETED.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class ProspectiveOOSObserver:
    """Captures scanner candidates for prospective OOS experiments.

    Called for every candidate that matches a registered prospective experiment.
    Produces prospective_observation rows with frozen rule evaluations,
    then immediately promotes each to research.research_signal.
    """

    def __init__(self, conn: Any, registry: dict[str, dict],
                 research_repo: Any | None = None) -> None:
        """Parameters
        ----------
        conn : connection
            Direct DB connection for prospective_observation INSERTs.
        registry : dict
            Registry of prospective experiments keyed by experiment_id.
        research_repo : ResearchRepository, optional
            If provided, new observations are immediately promoted to
            research.research_signal after insertion.
        """
        self._conn = conn
        self._registry = registry
        self._research_repo = research_repo
        self._stats: dict[str, int] = {}
        self._active_cache: dict[str, bool] = {}
        self._closed_directions_cache: dict[str, set[str]] = {}

    @staticmethod
    def _active_status(db_status: str | None) -> bool:
        """Return whether a DB lifecycle state permits prospective capture.

        Terminal states must never be reactivated by registry reload,
        scanner restart, evaluator restart, or migration/bootstrap logic.
        """
        return db_status not in {"CANCELLED", "COMPLETED", "CLOSED_NEGATIVE"}

    @staticmethod
    def _load_active_registry(registry: dict[str, dict]) -> dict[str, dict]:
        """Filter out experiment specs that explicitly mark themselves inactive."""
        active: dict[str, dict] = {}
        for exp_id, spec in registry.items():
            if spec.get("status") in {"CANCELLED", "COMPLETED", "INACTIVE"}:
                continue
            active[exp_id] = spec
        return active

    def _is_db_active(self, exp_id: str) -> bool:
        """Check the experiment's DB lifecycle state before capture."""
        if self._conn is None:
            return True
        if exp_id in self._active_cache:
            return self._active_cache[exp_id]

        active = True
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                "SELECT status FROM research.prospective_experiment "
                "WHERE experiment_id = %s",
                (exp_id,),
            )
            row = cursor.fetchone()
            if row is not None and row[0] is not None:
                active = self._active_status(str(row[0]))
                self._active_cache[exp_id] = active
                if not active:
                    logger.info(
                        "prospective observer: skipping terminal experiment %s (status=%s)",
                        exp_id, row[0],
                    )
        except Exception:
            # Fail-open on lifecycle lookup errors; terminal persistence remains
            # protected by registry-level terminal flags and evaluator status checks.
            logger.debug(
                "prospective observer: lifecycle lookup failed for %s; registry routing retained",
                exp_id, exc_info=True,
            )
        finally:
            cursor.close()
        return active

    def _is_direction_active(self, exp_id: str, direction: str) -> bool:
        """Check the direction-level DB lifecycle state before capture.

        Returns True when no direction-level override exists, preserving
        backward compatibility. Terminal direction states block NEW capture
        for that direction only.
        """
        if self._conn is None:
            return True

        if exp_id not in self._closed_directions_cache:
            closed: set[str] = set()
            cursor = self._conn.cursor()
            try:
                cursor.execute(
                    "SELECT direction, status FROM research.prospective_experiment_direction_state "
                    "WHERE experiment_id = %s",
                    (exp_id,),
                )
                for row in cursor.fetchall():
                    if row[1] in {"CANCELLED", "COMPLETED", "CLOSED_NEGATIVE"}:
                        closed.add(row[0])
                self._closed_directions_cache[exp_id] = closed
                if closed:
                    logger.info(
                        "prospective observer: closed directions for %s: %s",
                        exp_id, sorted(closed),
                    )
            except Exception:
                self._closed_directions_cache[exp_id] = set()
                logger.debug(
                    "prospective observer: direction lifecycle lookup failed for %s; "
                    "falling back to experiment-level status",
                    exp_id, exc_info=True,
                )
            finally:
                cursor.close()

        return direction not in self._closed_directions_cache[exp_id]


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
    ) -> list[int]:
        """Process a candidate against all applicable prospective experiments.

        For ME SHORT: produces 3 paired observations (A/B/C).
        For others: produces 1 observation per matching experiment.

        Each new observation is immediately promoted to research_signal
        if a research_repo was provided at construction.

        Returns list of observation_ids created.
        """
        if not self._conn:
            return []

        source_key = self._make_source_key(
            scanner_name, symbol, direction, signal_time,
        )
        active_registry = self._load_active_registry(self._registry)

        observation_ids: list[int] = []
        cursor = self._conn.cursor()
        try:
            for exp_id, exp_spec in active_registry.items():
                if exp_spec["scanner_name"] != scanner_name:
                    continue
                if not self._is_db_active(exp_id):
                    continue
                if not self._is_direction_active(exp_id, direction):
                    logger.info(
                        "prospective observer: skipping closed direction %s for %s",
                        direction, exp_id,
                    )
                    continue

                # Check direction match: support multi-direction experiments
                allowed_directions = exp_spec.get("directions")
                if allowed_directions:
                    if direction not in allowed_directions:
                        continue
                elif exp_spec["direction"] != direction:
                    continue

                if exp_id.startswith("ME_SHORT_GEOM_"):
                    obs_id = self._observe_me_geometry(
                        cursor, exp_id, exp_spec,
                        source_key, symbol, direction, signal_time,
                        reference_price, invalidation_price, target_1, target_2,
                        score, features, parameters, market_regime,
                    )
                elif exp_id == "SRR_OOS_SCANNER_V1_PROSPECTIVE":
                    obs_id = self._observe_srr_oos_exit(
                        cursor, exp_id, exp_spec,
                        source_key, symbol, direction, signal_time,
                        reference_price, invalidation_price, target_1, target_2,
                        score, features, parameters, market_regime,
                    )
                else:
                    obs_id = self._observe_standard(
                        cursor, exp_id, exp_spec,
                        source_key, symbol, direction, signal_time,
                        reference_price, invalidation_price, target_1, target_2,
                        score, features, parameters, market_regime,
                    )

                if obs_id is not None:
                    observation_ids.append(obs_id)
                    self._stats[exp_id] = self._stats.get(exp_id, 0) + 1
                    # ── Immediately promote to research_signal ──
                    self._promote_observation(obs_id)

            self._conn.commit()

        except Exception:
            self._conn.rollback()
            self._stats["errors"] = self._stats.get("errors", 0) + 1
            logger.exception(
                "prospective observer error: %s %s", scanner_name, symbol,
            )

        return observation_ids

    def _promote_observation(self, observation_id: int) -> None:
        """Promote a newly created prospective observation to research_signal.

        Uses the dedicated research repository's promote_prospective_to_signal
        method.  Fail-open: errors logged, never propagated.
        """
        if self._research_repo is None:
            return
        try:
            ok = self._research_repo.promote_prospective_to_signal(observation_id)
            if ok:
                self._stats["promoted"] = self._stats.get("promoted", 0) + 1
                logger.debug(
                    "prospective observation %d promoted to research_signal",
                    observation_id,
                )
            else:
                self._stats["promote_skipped"] = self._stats.get("promote_skipped", 0) + 1
        except Exception:
            self._stats["promote_errors"] = self._stats.get("promote_errors", 0) + 1
            logger.exception(
                "prospective promotion failed for obs_id=%d", observation_id,
            )

    def _observe_standard(
        self, cursor, exp_id: str, exp_spec: dict,
        source_key: int, symbol: str, direction: str, signal_time: Any,
        reference_price: float, invalidation_price: float | None,
        target_1: float | None, target_2: float | None, score: float,
        features: dict, parameters: dict, market_regime: str | None,
    ) -> int | None:
        """Insert a standard prospective observation. Returns observation_id or None."""
        rule_passed = True
        filter_reason = None
        insert_rows = (
            exp_id, source_key, None, symbol, direction, signal_time,
            reference_price, invalidation_price, target_1, target_2,
            score, rule_passed, filter_reason,
            json.dumps(features), json.dumps(parameters), market_regime,
        )

        if exp_id == "VC_SHORT_EXECUTION_V1":
            freeze_ts = exp_spec["freeze_ts"]
            freeze_dt = datetime.fromisoformat(freeze_ts.replace("Z", "+00:00"))
            signal_dt = signal_time if isinstance(signal_time, datetime) else datetime.fromisoformat(signal_time.replace("Z", "+00:00"))
            if signal_dt.tzinfo is None:
                signal_dt = signal_dt.replace(tzinfo=timezone.utc)
            if signal_dt <= freeze_dt:
                return None

            bb_pct = features.get("bb_width_percentile")
            if bb_pct is None or float(bb_pct) >= float(exp_spec["threshold"]):
                return None
            if direction != "SHORT" or invalidation_price is None or reference_price <= 0:
                return None
            if invalidation_price <= reference_price or target_1 is None or target_1 >= reference_price:
                return None
            structural_r = abs(reference_price - invalidation_price)
            execution_features = dict(features)
            execution_features.update({
                "_frozen_entry": "reference_price",
                "_frozen_structural_r": structural_r,
                "_frozen_structural_r_pct": structural_r / reference_price,
                "_frozen_sl_r": 1.0,
                "_frozen_tp_r": abs(reference_price - target_1) / structural_r,
                "_frozen_max_hold": int(exp_spec["max_hold_minutes"]),
                "_frozen_intrabar_policy": "STOP_FIRST",
                "_frozen_freeze_ts": exp_spec["freeze_ts"],
            })
            insert_rows = (
                exp_id, source_key, None, symbol, direction, signal_time,
                reference_price, invalidation_price, target_1, target_2,
                score, True, "vc_bb_width_execution_geometry_frozen",
                json.dumps(execution_features), json.dumps(parameters), market_regime,
            )

        elif exp_id == "VC_SHORT_BB_WIDTH_V1":
            bb_pct = features.get("bb_width_percentile")
            if bb_pct is not None:
                threshold = exp_spec.get("threshold")
                if threshold is not None:
                    rule_passed = float(bb_pct) < float(threshold)
                    filter_reason = f"bb_width_percentile={bb_pct:.4f} {'<' if rule_passed else '>='} {threshold}"
            else:
                rule_passed = False
                filter_reason = "bb_width_percentile is NULL"

        elif exp_id in ("LR_SHORT_GATE_V1", "LR_LONG_GATE_V1"):
            rule_passed = True
            filter_reason = "observational_gate_validation"
            insert_rows = (
                exp_id, source_key, None, symbol, direction, signal_time,
                reference_price, invalidation_price, target_1, target_2,
                score, rule_passed, filter_reason,
                json.dumps(features), json.dumps(parameters), market_regime,
            )

        elif exp_id == "BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1":
            rule_passed = True
            filter_reason = "expectancy_rejection_oos_capture"
            features_with_expectancy = dict(features)
            features_with_expectancy["_expectancy_rejection_oos"] = True
            features_with_expectancy["_rejection_reason"] = "profit_factor_below_threshold"
            insert_rows = (
                exp_id, source_key, None, symbol, direction, signal_time,
                reference_price, invalidation_price, target_1, target_2,
                score, rule_passed, filter_reason,
                json.dumps(features_with_expectancy), json.dumps(parameters), market_regime,
            )

        elif exp_id == "FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1":
            rule_passed = True
            filter_reason = "expectancy_rejection_oos_capture"
            features_with_expectancy = dict(features)
            features_with_expectancy["_expectancy_rejection_oos"] = True
            features_with_expectancy["_rejection_reason"] = "negative_historical_performance"
            insert_rows = (
                exp_id, source_key, None, symbol, direction, signal_time,
                reference_price, invalidation_price, target_1, target_2,
                score, rule_passed, filter_reason,
                json.dumps(features_with_expectancy), json.dumps(parameters), market_regime,
            )

        elif exp_id == "TREND_PULLBACK_V3_HIGH_VOL_OOS_V1":
            rule_passed = True
            filter_reason = "regime_counterfactual_oos_capture"
            features_with_regime = dict(features)
            features_with_regime["_regime_counterfactual_oos"] = True
            features_with_regime["_actual_regime"] = market_regime
            features_with_regime["_required_regime"] = "TREND_UP"
            insert_rows = (
                exp_id, source_key, None, symbol, direction, signal_time,
                reference_price, invalidation_price, target_1, target_2,
                score, rule_passed, filter_reason,
                json.dumps(features_with_regime), json.dumps(parameters), market_regime,
            )

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
            RETURNING observation_id
            """,
            insert_rows,
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def _observe_me_geometry(
        self, cursor, exp_id: str, exp_spec: dict,
        source_key: int, symbol: str, direction: str, signal_time: Any,
        reference_price: float, invalidation_price: float | None,
        target_1: float | None, target_2: float | None, score: float,
        features: dict, parameters: dict, market_regime: str | None,
    ) -> int | None:
        """Insert ME SHORT geometry variant observation. Returns observation_id or None."""
        if invalidation_price is None or reference_price <= 0:
            return None

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
            if target_1 is not None:
                variant_target = reference_price - target_dist_A

        elif exp_id == "ME_SHORT_GEOM_C_V1":
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
            RETURNING observation_id
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
        row = cursor.fetchone()
        if row:
            return row[0]
        return None

    def _observe_srr_oos_exit(
        self, cursor, exp_id: str, exp_spec: dict,
        source_key: int, symbol: str, direction: str, signal_time: Any,
        reference_price: float, invalidation_price: float | None,
        target_1: float | None, target_2: float | None, score: float,
        features: dict, parameters: dict, market_regime: str | None,
    ) -> int | None:
        """Insert SRR OOS exit validation observation with frozen exit geometry.

        Captures BOTH LONG and SHORT from SUPPORT_RESISTANCE_REACTION.
        Computes variant_exit using frozen OOS geometry:
          R = abs(reference_price - invalidation_price)
          LONG:  stop = entry - 0.75R, target = entry + 1.50R
          SHORT: stop = entry + 0.75R, target = entry - 1.50R

        Returns observation_id or None.
        """
        if invalidation_price is None or reference_price <= 0:
            return None

        structural_r = abs(reference_price - invalidation_price)
        if structural_r <= 0:
            return None

        variant_entry = reference_price
        is_short = direction == "SHORT"

        if is_short:
            variant_stop = reference_price + 0.75 * structural_r
            variant_target = reference_price - 1.50 * structural_r
        else:
            variant_stop = reference_price - 0.75 * structural_r
            variant_target = reference_price + 1.50 * structural_r

        frozen_exit_features = dict(features)
        frozen_exit_features["_frozen_sl_r"] = 0.75
        frozen_exit_features["_frozen_tp_r"] = 1.50
        frozen_exit_features["_frozen_max_hold"] = 120
        frozen_exit_features["_structural_r"] = structural_r

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
            RETURNING observation_id
            """,
            (
                exp_id, source_key, None,
                symbol, direction, signal_time,
                reference_price, invalidation_price, target_1, target_2,
                score, "oos_exit_validation_frozen_geometry",
                variant_entry, variant_stop, variant_target,
                json.dumps(frozen_exit_features), json.dumps(parameters), market_regime,
            ),
        )
        row = cursor.fetchone()
        if row:
            return row[0]
        return None

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)
