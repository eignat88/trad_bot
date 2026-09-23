"""Shadow/FVG Filter OOS Experiment for FVG_REACTION_LONG_LOCAL_STRUCT_V1.

Records a classification observation for every FVG LONG signal:

  Variant A (production candidate):
    bars_to_touch <= 2 AND fvg_atr >= 1.10 AND c2_body_ratio >= 0.85

  Variant B:
    bars_to_touch <= 2 AND fvg_atr >= 1.10 AND c2_body_ratio >= 0.80

  Variant C:
    bars_to_touch <= 2 AND fvg_atr >= 1.10 AND c2_body_ratio >= 0.90

The classification is recorded but NEVER blocks the FVG trade.
FVG continues to operate identically — this is a shadow/control experiment.

When FVG opens a paper trade for a PASS signal, the actual trade outcome
is used.  For REJECT signals that FVG did NOT trade, a counterfactual
shadow outcome is computed using the same SL/TP/hold parameters as FVG.

Tables:
  dds.shadow_fvg_filter_observation  — per-signal observation records

Design principles:
  - No look-ahead bias: filter_result uses only data available at signal time
  - Deduplication: UNIQUE (experiment_id, setup_id) prevents duplicates
  - Frozen thresholds: thresholds are NOT tuned after OOS collection begins
  - Production isolation: never modifies FVG scanner logic or paper execution
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

EXPERIMENT_ID = "FVG_REACTION_LONG_LOCAL_STRUCT_FILTER_OOS_V1"
SCANNER_NAME = "FVG_REACTION_LONG_LOCAL_STRUCT_V1"

# --- Variant thresholds (FROZEN — do not change after deployment) ---

# Variant A — primary production candidate
VARIANT_A_BARS_MAX = 2
VARIANT_A_FVG_ATR_MIN = 1.10
VARIANT_A_C2_BODY_RATIO_MIN = 0.85

# Variant B — wider c2_body_ratio threshold
VARIANT_B_BARS_MAX = 2
VARIANT_B_FVG_ATR_MIN = 1.10
VARIANT_B_C2_BODY_RATIO_MIN = 0.80

# Variant C — tighter c2_body_ratio threshold
VARIANT_C_BARS_MAX = 2
VARIANT_C_FVG_ATR_MIN = 1.10
VARIANT_C_C2_BODY_RATIO_MIN = 0.90


def classify_variant(
    bars_to_touch: int | float | None,
    fvg_atr: float | None,
    c2_body_ratio: float | None,
    *,
    bars_max: int,
    fvg_atr_min: float,
    c2_body_ratio_min: float,
) -> tuple[str, str | None]:
    """Classify a signal against a single variant's thresholds.

    Returns (filter_result, filter_reason).

    No look-ahead: uses only features available at signal creation time.
    """
    # Missing features
    if bars_to_touch is None or fvg_atr is None or c2_body_ratio is None:
        return "MISSING_FEATURE", "MISSING_FEATURE"

    bars = int(bars_to_touch)
    fvg = float(fvg_atr)
    c2 = float(c2_body_ratio)

    # Collect rejection reasons
    reasons: list[str] = []
    if bars > bars_max:
        reasons.append("BARS_TO_TOUCH_GT_2")
    if fvg < fvg_atr_min:
        reasons.append("FVG_ATR_LT_1_10")
    if c2 < c2_body_ratio_min:
        reasons.append("C2_BODY_RATIO_LT_0_85" if c2_body_ratio_min == 0.85
                       else f"C2_BODY_RATIO_LT_{c2_body_ratio_min}")

    if not reasons:
        return "PASS", "PASS_ALL_FILTERS"

    if len(reasons) > 1:
        return "REJECT", "MULTIPLE_FILTER_FAILURES"
    return "REJECT", reasons[0]


def classify_all_variants(
    bars_to_touch: int | float | None,
    fvg_atr: float | None,
    c2_body_ratio: float | None,
) -> dict[str, tuple[str, str | None]]:
    """Classify a signal against all three variants.

    Returns dict with keys 'variant_a', 'variant_b', 'variant_c',
    each mapping to (filter_result, filter_reason).
    """
    return {
        "variant_a": classify_variant(
            bars_to_touch, fvg_atr, c2_body_ratio,
            bars_max=VARIANT_A_BARS_MAX,
            fvg_atr_min=VARIANT_A_FVG_ATR_MIN,
            c2_body_ratio_min=VARIANT_A_C2_BODY_RATIO_MIN,
        ),
        "variant_b": classify_variant(
            bars_to_touch, fvg_atr, c2_body_ratio,
            bars_max=VARIANT_B_BARS_MAX,
            fvg_atr_min=VARIANT_B_FVG_ATR_MIN,
            c2_body_ratio_min=VARIANT_B_C2_BODY_RATIO_MIN,
        ),
        "variant_c": classify_variant(
            bars_to_touch, fvg_atr, c2_body_ratio,
            bars_max=VARIANT_C_BARS_MAX,
            fvg_atr_min=VARIANT_C_FVG_ATR_MIN,
            c2_body_ratio_min=VARIANT_C_C2_BODY_RATIO_MIN,
        ),
    }


class FVGFilterShadowObserver:
    """Observes FVG LONG signals and records filter classification.

    This class is purely observational — it never prevents FVG from trading.
    """

    def __init__(self, repository: Any) -> None:
        self.repo = repository
        self._observation_count = 0
        self._pass_count = 0
        self._reject_count = 0
        self._missing_count = 0

    def observe_signal(
        self,
        setup_id: str,
        symbol: str,
        instrument_id: int | None,
        direction: str,
        detected_at: datetime | None,
        features: dict,
    ) -> dict[str, Any]:
        """Classify an FVG LONG signal and persist the observation.

        Parameters
        ----------
        setup_id : str
            The scanner_setup.setup_id for this signal.
        features : dict
            The features JSONB from scanner_setup — must contain
            'bars_to_touch', 'fvg_atr', 'c2_body_ratio'.

        Returns
        -------
        dict with keys: filter_result, variant results, observation_id
        """
        bars_to_touch = features.get("bars_to_touch")
        fvg_atr = features.get("fvg_atr")
        c2_body_ratio = features.get("c2_body_ratio")

        # Classify all variants
        variants = classify_all_variants(bars_to_touch, fvg_atr, c2_body_ratio)

        # Primary filter = Variant A
        primary_result, primary_reason = variants["variant_a"]

        self._observation_count += 1
        if primary_result == "PASS":
            self._pass_count += 1
        elif primary_result == "REJECT":
            self._reject_count += 1
        else:
            self._missing_count += 1

        # Build additional feature snapshot
        c2_body_atr = features.get("c2_body_atr")
        risk_pct = features.get("risk_pct")
        score = features.get("score")
        market_regime = features.get("market_regime")
        fvg_size = features.get("fvg_size")
        rr = features.get("rr")
        entry_price = features.get("entry_price")
        sl_price = features.get("sl_price")
        tp_price = features.get("tp_price")

        observation_id = self._save_observation(
            setup_id=setup_id,
            symbol=symbol,
            instrument_id=instrument_id,
            direction=direction,
            detected_at=detected_at,
            bars_to_touch=int(bars_to_touch) if bars_to_touch is not None else None,
            fvg_atr=float(fvg_atr) if fvg_atr is not None else None,
            c2_body_ratio=float(c2_body_ratio) if c2_body_ratio is not None else None,
            c2_body_atr=float(c2_body_atr) if c2_body_atr is not None else None,
            risk_pct=float(risk_pct) if risk_pct is not None else None,
            score=float(score) if score is not None else None,
            market_regime=market_regime,
            fvg_size=float(fvg_size) if fvg_size is not None else None,
            rr=float(rr) if rr is not None else None,
            entry_price=float(entry_price) if entry_price is not None else None,
            sl_price=float(sl_price) if sl_price is not None else None,
            tp_price=float(tp_price) if tp_price is not None else None,
            variant_a_result=variants["variant_a"][0],
            variant_a_reason=variants["variant_a"][1],
            variant_b_result=variants["variant_b"][0],
            variant_b_reason=variants["variant_b"][1],
            variant_c_result=variants["variant_c"][0],
            variant_c_reason=variants["variant_c"][1],
            filter_result=primary_result,
            filter_reason=primary_reason,
        )

        # Structured log per task §19
        if primary_result == "PASS":
            logger.info(
                "FVG_FILTER_OOS candidate: symbol=%s setup_id=%s "
                "bars_to_touch=%s fvg_atr=%.4f c2_body_ratio=%.4f "
                "filter_result=PASS experiment_id=%s",
                symbol, setup_id,
                bars_to_touch,
                float(fvg_atr) if fvg_atr is not None else 0.0,
                float(c2_body_ratio) if c2_body_ratio is not None else 0.0,
                EXPERIMENT_ID,
            )
        else:
            logger.info(
                "FVG_FILTER_OOS candidate: symbol=%s setup_id=%s "
                "bars_to_touch=%s fvg_atr=%s c2_body_ratio=%s "
                "filter_result=REJECT reason=%s experiment_id=%s",
                symbol, setup_id,
                bars_to_touch,
                f"{float(fvg_atr):.4f}" if fvg_atr is not None else "None",
                f"{float(c2_body_ratio):.4f}" if c2_body_ratio is not None else "None",
                primary_reason,
                EXPERIMENT_ID,
            )

        return {
            "filter_result": primary_result,
            "variant_a": variants["variant_a"],
            "variant_b": variants["variant_b"],
            "variant_c": variants["variant_c"],
            "observation_id": observation_id,
        }

    def record_outcome(
        self,
        setup_id: str,
        first_event: str,
        result_r: float,
        fee_slippage_adjusted_result_r: float,
        mfe_r: float,
        mae_r: float,
    ) -> None:
        """Update observation with outcome data from the standard outcome evaluator.

        Called by the outcome evaluator after it computes the standard
        signal_outcome for the setup.  This keeps the shadow experiment
        outcome in sync with the production outcome.
        """
        sql = """
        UPDATE dds.shadow_fvg_filter_observation
        SET first_event = %(first_event)s,
            result_r = %(result_r)s,
            fee_slippage_adjusted_result_r = %(fee_slippage_adjusted_result_r)s,
            mfe_r = %(mfe_r)s,
            mae_r = %(mae_r)s,
            status = 'EVALUATED',
            evaluated_at = now(),
            updated_at = now()
        WHERE experiment_id = %(experiment_id)s
          AND setup_id = %(setup_id)s
        """
        try:
            self.repo._execute(sql, {
                "experiment_id": EXPERIMENT_ID,
                "setup_id": setup_id,
                "first_event": first_event,
                "result_r": result_r,
                "fee_slippage_adjusted_result_r": fee_slippage_adjusted_result_r,
                "mfe_r": mfe_r,
                "mae_r": mae_r,
            })
            self.repo._conn.commit()
        except Exception:
            logger.exception(
                "Failed to record outcome for shadow FVG observation setup=%s",
                setup_id,
            )

    def _save_observation(
        self,
        setup_id: str,
        symbol: str,
        instrument_id: int | None,
        direction: str,
        detected_at: datetime | None,
        bars_to_touch: int | None,
        fvg_atr: float | None,
        c2_body_ratio: float | None,
        c2_body_atr: float | None,
        risk_pct: float | None,
        score: float | None,
        market_regime: str | None,
        fvg_size: float | None,
        rr: float | None,
        entry_price: float | None,
        sl_price: float | None,
        tp_price: float | None,
        variant_a_result: str,
        variant_a_reason: str | None,
        variant_b_result: str,
        variant_b_reason: str | None,
        variant_c_result: str,
        variant_c_reason: str | None,
        filter_result: str,
        filter_reason: str | None,
    ) -> int | None:
        """Persist a new shadow observation."""
        sql = """
        INSERT INTO dds.shadow_fvg_filter_observation (
            experiment_id, setup_id, scanner_name, direction,
            symbol, instrument_id, detected_at,
            bars_to_touch, fvg_atr, c2_body_ratio,
            c2_body_atr, risk_pct, score, market_regime,
            fvg_size, rr, entry_price, sl_price, tp_price,
            variant_a_result, variant_a_reason,
            variant_b_result, variant_b_reason,
            variant_c_result, variant_c_reason,
            filter_result, filter_reason, status
        ) VALUES (
            %(experiment_id)s, %(setup_id)s, %(scanner_name)s, %(direction)s,
            %(symbol)s, %(instrument_id)s, %(detected_at)s,
            %(bars_to_touch)s, %(fvg_atr)s, %(c2_body_ratio)s,
            %(c2_body_atr)s, %(risk_pct)s, %(score)s, %(market_regime)s,
            %(fvg_size)s, %(rr)s, %(entry_price)s, %(sl_price)s, %(tp_price)s,
            %(variant_a_result)s, %(variant_a_reason)s,
            %(variant_b_result)s, %(variant_b_reason)s,
            %(variant_c_result)s, %(variant_c_reason)s,
            %(filter_result)s, %(filter_reason)s, 'PENDING'
        )
        ON CONFLICT (experiment_id, setup_id) DO UPDATE SET
            bars_to_touch = EXCLUDED.bars_to_touch,
            fvg_atr = EXCLUDED.fvg_atr,
            c2_body_ratio = EXCLUDED.c2_body_ratio,
            variant_a_result = EXCLUDED.variant_a_result,
            variant_a_reason = EXCLUDED.variant_a_reason,
            variant_b_result = EXCLUDED.variant_b_result,
            variant_b_reason = EXCLUDED.variant_b_reason,
            variant_c_result = EXCLUDED.variant_c_result,
            variant_c_reason = EXCLUDED.variant_c_reason,
            filter_result = EXCLUDED.filter_result,
            filter_reason = EXCLUDED.filter_reason,
            updated_at = now()
        RETURNING observation_id
        """
        try:
            self.repo._execute(sql, {
                "experiment_id": EXPERIMENT_ID,
                "setup_id": setup_id,
                "scanner_name": SCANNER_NAME,
                "direction": direction,
                "symbol": symbol,
                "instrument_id": instrument_id,
                "detected_at": detected_at,
                "bars_to_touch": bars_to_touch,
                "fvg_atr": fvg_atr,
                "c2_body_ratio": c2_body_ratio,
                "c2_body_atr": c2_body_atr,
                "risk_pct": risk_pct,
                "score": score,
                "market_regime": market_regime,
                "fvg_size": fvg_size,
                "rr": rr,
                "entry_price": entry_price,
                "sl_price": sl_price,
                "tp_price": tp_price,
                "variant_a_result": variant_a_result,
                "variant_a_reason": variant_a_reason,
                "variant_b_result": variant_b_result,
                "variant_b_reason": variant_b_reason,
                "variant_c_result": variant_c_result,
                "variant_c_reason": variant_c_reason,
                "filter_result": filter_result,
                "filter_reason": filter_reason,
            })
            self.repo._conn.commit()
            row = self.repo._fetchone("SELECT lastval()")
            return row[0] if row else None
        except Exception:
            logger.exception(
                "Failed to save shadow FVG observation for setup=%s", setup_id
            )
            return None

    @property
    def stats(self) -> dict[str, int]:
        return {
            "observations": self._observation_count,
            "pass": self._pass_count,
            "reject": self._reject_count,
            "missing": self._missing_count,
        }
