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

import hashlib
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
                 research_repo: Any | None = None,
                 completed_event_repo: Any | None = None) -> None:
        """Parameters
        ----------
        conn : connection
            Direct DB connection for prospective_observation INSERTs.
        registry : dict
            Registry of prospective experiments keyed by experiment_id.
        research_repo : ResearchRepository, optional
            If provided, new observations are immediately promoted to
            research.research_signal after insertion.
        completed_event_repo : ProspectiveCompletedEventRepository, optional
            Repository that freezes immutable HTF Point-B completion state.
        """
        self._conn = conn
        self._registry = registry
        self._research_repo = research_repo
        self._completed_event_repo = completed_event_repo
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

    @staticmethod
    def _make_htf_source_key(exp_id: str, setup_event_id: str) -> int:
        """Build restart-stable HTF observation identity.

        Generic scanner identity may use process-randomized Python ``hash``.
        HTF prospective OOS cannot: the same immutable completed event must
        retain the same source_signal_id across scanner restarts and retries.
        The stable digest covers experiment_id + setup_event_id, so Point-B
        and BASELINE remain distinct while each experiment's touch identity
        remains stable.
        """
        payload = f"{exp_id}:{setup_event_id}".encode("utf-8")
        digest = hashlib.sha256(payload).digest()
        value = int.from_bytes(digest[:8], "big") % (2**31 - 1)
        return -(value + 1)

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
        detection_result: Any | None = None,
    ) -> list[int]:
        """Process a candidate against all applicable prospective experiments.

        For ME SHORT: produces 3 paired observations (A/B/C).
        For others: produces 1 observation per matching experiment.

        Each new observation is immediately promoted to research_signal
        if a research_repo was provided at construction.

        For HTF key-level experiments, ``detection_result`` is frozen through
        the immutable completion lifecycle only after registry routing,
        lifecycle checks, direction eligibility, and the strict post-freeze
        boundary have passed.  The detector itself remains unchanged and
        stateless; durable lifecycle state lives in this observer layer.

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

                # HTF prospective capture is valid only strictly after the
                # frozen protocol timestamp.  Registry presence alone must
                # never backfill historical detector candidates.
                if exp_id.startswith("HTF_KEYLEVEL_"):
                    setup_event_id = (
                        detection_result.signal.features.get("setup_event_id")
                        if detection_result is not None
                        and getattr(detection_result, "signal", None) is not None
                        and isinstance(getattr(detection_result.signal, "features", None), dict)
                        else features.get("setup_event_id")
                    )
                    if not setup_event_id:
                        continue

                    freeze_ts = exp_spec.get("freeze_ts")
                    if not freeze_ts:
                        continue
                    freeze_dt = datetime.fromisoformat(
                        str(freeze_ts).replace("Z", "+00:00")
                    )
                    signal_dt = (
                        signal_time
                        if isinstance(signal_time, datetime)
                        else datetime.fromisoformat(
                            str(signal_time).replace("Z", "+00:00")
                        )
                    )

                    if signal_dt.tzinfo is None:
                        signal_dt = signal_dt.replace(tzinfo=timezone.utc)
                    if freeze_dt.tzinfo is None:
                        freeze_dt = freeze_dt.replace(tzinfo=timezone.utc)

                    # Strict prospective boundary:
                    # eligible iff signal_time > freeze_ts.
                    if signal_dt <= freeze_dt:
                        continue

                    htf_source_key = self._make_htf_source_key(exp_id, setup_event_id)

                    # Only the Point-B experiment owns immutable completion
                    # state. BASELINE remains observational/control-only.
                    authoritative: dict[str, Any] | None = None
                    if (
                        exp_id == "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE"
                        and detection_result is not None
                    ):
                        authoritative = self._freeze_htf_point_b_completion(detection_result)
                        if authoritative is None:
                            continue

                    obs_id = self._observe_htf_keylevel(
                        cursor, exp_id, exp_spec,
                        htf_source_key, symbol, direction, signal_time,
                        reference_price, invalidation_price, target_1, target_2,
                        score, features, parameters, market_regime,
                        setup_event_id=setup_event_id,
                        authoritative=authoritative,
                    )
                elif exp_id.startswith("ME_SHORT_GEOM_"):
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

    @staticmethod
    def _completion_snapshot_from_features(features: dict) -> dict[str, Any] | None:
        """Derive a completion snapshot directly from detector candidate features."""
        if not features or features.get("cohort") != "POINT_B":
            return None
        if not features.get("setup_event_id"):
            return None
        direction = features.get("direction")
        if direction not in {"LONG", "SHORT"}:
            return None
        required = {
            "symbol",
            "key_level_price",
            "key_level_type",
            "touch_time",
            "touch_price",
            "reaction_time",
            "reaction_high",
            "reaction_low",
            "reaction_candles",
            "structure_reference_time",
            "structure_reference_price",
            "break_time",
            "break_price",
            "point_b_time",
            "point_b_price",
            "point_b_retrace_pct",
            "signal_time",
            "entry_reference_price",
            "structural_stop_price",
            "risk_abs",
            "risk_pct",
        }
        missing = [field for field in required if features.get(field) is None]
        if missing:
            logger.debug(
                "HTF Point-B completion skipped; detector features missing: %s",
                missing,
            )
            return None
        return {
            "symbol": features["symbol"],
            "direction": direction,
            "level_price": features["key_level_price"],
            "level_type": features["key_level_type"],
            "touch_time": features["touch_time"],
            "touch_price": features["touch_price"],
            "reaction_time": features["reaction_time"],
            "reaction_high": features["reaction_high"],
            "reaction_low": features["reaction_low"],
            "reaction_candles": features["reaction_candles"],
            "structure_reference_time": features["structure_reference_time"],
            "structure_reference_price": features["structure_reference_price"],
            "break_time": features["break_time"],
            "break_price": features["break_price"],
            "point_b_time": features["point_b_time"],
            "point_b_price": features["point_b_price"],
            "point_b_retrace_pct": features["point_b_retrace_pct"],
            "signal_time": features["signal_time"],
            "entry_reference_price": features["entry_reference_price"],
            "structural_stop_price": features["structural_stop_price"],
            "risk_abs": features["risk_abs"],
            "risk_pct": features["risk_pct"],
            "target_1": features.get("target_1"),
            "target_2": features.get("target_2"),
            "experiment_id": features.get("experiment_id"),
            "setup_event_id": features["setup_event_id"],
            "frozen_at": features["signal_time"],
        }

    def _freeze_htf_point_b_completion(self, result: Any) -> dict[str, Any] | None:
        """Freeze and return the authoritative immutable Point-B completion.

        First-writer-wins is enforced by PostgreSQL:
        ``ON CONFLICT (experiment_id, setup_event_id) DO NOTHING`` followed by
        an authoritative SELECT.  Existing frozen geometry is never updated.
        """
        if self._completed_event_repo is None:
            return None
        signal = getattr(result, "signal", None)
        features = getattr(signal, "features", None) if signal is not None else None
        if not isinstance(features, dict):
            return None
        snapshot = self._completion_snapshot_from_features(features)
        if snapshot is None:
            return None
        experiment_id = snapshot.get("experiment_id") or (
            "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE"
        )
        setup_event_id = snapshot.get("setup_event_id")
        try:
            frozen = self._completed_event_repo.freeze_completed_event(
                experiment_id=experiment_id,
                setup_event_id=setup_event_id,
                snapshot=snapshot,
            )
            if frozen is None:
                self._stats["htf_completion_errors"] = (
                    self._stats.get("htf_completion_errors", 0) + 1
                )
                return None
            key = f"{experiment_id}:completed"
            self._stats[key] = self._stats.get(key, 0) + 1
            logger.debug(
                "HTF Point-B completion authoritative for %s/%s",
                experiment_id,
                setup_event_id,
            )
            return frozen
        except Exception:
            self._stats["htf_completion_errors"] = (
                self._stats.get("htf_completion_errors", 0) + 1
            )
            logger.exception(
                "HTF Point-B completion freeze failed for %s/%s",
                experiment_id,
                setup_event_id,
            )
            return None

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

    def _observe_htf_keylevel(
        self, cursor, exp_id: str, exp_spec: dict,
        source_key: int, symbol: str, direction: str, signal_time: Any,
        reference_price: float, invalidation_price: float | None,
        target_1: float | None, target_2: float | None, score: float,
        features: dict, parameters: dict, market_regime: str | None,
        *,
        setup_event_id: str | None,
        authoritative: dict[str, Any] | None,
    ) -> int | None:
        """Capture the isolated HTF key-level Point-B/BASELINE cohort.

        This branch is additive and keyed only by the two experiment IDs. It
        never changes existing frozen experiment observation semantics.

        For Point-B, ``authoritative`` is the first immutable completion row
        returned by the completed-event repository. It overrides every
        recomputed detector value for restart-safe FIRST VALID POINT-B WINS.
        BASELINE continues to use its contemporaneous detector snapshot and
        never owns completion state.
        """
        if exp_id not in {"HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE",
                          "HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE"}:
            return None
        if not setup_event_id:
            return None
        if authoritative is not None:
            snapshot = authoritative.get("snapshot")
            if not isinstance(snapshot, dict):
                return None
            features = snapshot
            symbol = str(snapshot["symbol"])
            direction = str(snapshot["direction"])
            signal_time = snapshot["signal_time"]
            reference_price = float(snapshot["entry_reference_price"])
            invalidation_price = float(snapshot["structural_stop_price"])
            target_1 = snapshot.get("target_1")
            target_2 = snapshot.get("target_2")
        elif not features or features.get("setup_event_id") != setup_event_id:
            return None
        if direction not in {"LONG", "SHORT"}:
            return None
        if reference_price <= 0 or invalidation_price is None or invalidation_price <= 0:
            return None
        risk_abs = reference_price - invalidation_price if direction == "LONG" else invalidation_price - reference_price
        if risk_abs <= 0:
            return None
        if exp_id.startswith("HTF_KEYLEVEL_SR_BREAK_") and features.get("cohort") != "POINT_B":
            return None
        if exp_id.endswith("BASELINE_V1_PROSPECTIVE") and features.get("cohort") != "BASELINE":
            return None
        features_with_protocol = dict(features)
        features_with_protocol["_frozen_max_hold"] = 240
        features_with_protocol["_frozen_intrabar_policy"] = "STOP_FIRST"
        features_with_protocol["_structural_r"] = risk_abs
        features_with_protocol["_authoritative_completion"] = authoritative is not None
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
            (
                exp_id, source_key, None, symbol, direction, signal_time,
                reference_price, invalidation_price, target_1, target_2,
                score, True, "HTF_KEYLEVEL_PROSPECTIVE_COHORT_CAPTURE",
                json.dumps(features_with_protocol), json.dumps(parameters), market_regime,
            ),
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
