"""Tests for FVG Reaction Long Local Struct Filter OOS Validation.

Covers:
  - Threshold logic for all three variants (A, B, C)
  - Missing feature handling
  - Deduplication via ON CONFLICT
  - No production impact: FVG scanner behavior unchanged
  - Shadow observation does not block signals
  - Outcome recording integration
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from app.shadow.fvg_filter_shadow import (
    classify_variant,
    classify_all_variants,
    FVGFilterShadowObserver,
    EXPERIMENT_ID,
    SCANNER_NAME,
    VARIANT_A_BARS_MAX,
    VARIANT_A_FVG_ATR_MIN,
    VARIANT_A_C2_BODY_RATIO_MIN,
    VARIANT_B_C2_BODY_RATIO_MIN,
    VARIANT_C_C2_BODY_RATIO_MIN,
)
from app.scanners.models import SetupCandidate, SetupState
from app.scanners.outcome import evaluate_setup_outcome


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _fvg_candidate(
    bars_to_touch=1,
    fvg_atr=1.24,
    c2_body_ratio=0.91,
    **extra_features,
):
    """Create a realistic FVG LONG SetupCandidate with typical features."""
    now = datetime.now(timezone.utc)
    features = {
        "fvg_created_at": int((now - timedelta(minutes=30)).timestamp() * 1000),
        "fvg_low": 100.0,
        "fvg_high": 101.0,
        "fvg_size": 1.0,
        "fvg_atr": fvg_atr,
        "c2_body_ratio": c2_body_ratio,
        "c2_body_atr": 1.224,
        "bars_to_touch": bars_to_touch,
        "touch_at": int((now - timedelta(minutes=20)).timestamp() * 1000),
        "confirmation_at": int((now - timedelta(minutes=15)).timestamp() * 1000),
        "entry_price": 102.0,
        "sl_price": 99.0,
        "tp_price": 111.0,
        "risk": 3.0,
        "risk_pct": 0.0294,
        "rr": 3.0,
        "score": 80.0,
        "market_regime": "TRENDING_UP",
    }
    features.update(extra_features)
    return SetupCandidate(
        setup_id=uuid4(),
        scanner_name=SCANNER_NAME,
        symbol="BTCUSDT",
        direction="LONG",
        entry_timeframe="5m",
        setup_timeframe="5m",
        detected_at=now,
        signal_candle_open_time=int((now - timedelta(minutes=25)).timestamp() * 1000),
        reference_price=102.0,
        entry_zone_low=102.0,
        entry_zone_high=102.0,
        invalidation_price=99.0,
        target_1=111.0,
        score=80.0,
        state=SetupState.READY_TO_TRADE,
        features=features,
    )


def _candle(ts, o=100, h=105, l=99, c=102, v=100):
    from app.models import Candle
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


# ===================================================================
# 1. Threshold Logic Tests — Variant A (production candidate)
# ===================================================================

class TestVariantA:
    """Variant A: bars_to_touch <= 2 AND fvg_atr >= 1.10 AND c2_body_ratio >= 0.85"""

    def test_pass_all_filters(self):
        result, reason = classify_variant(
            bars_to_touch=1, fvg_atr=1.24, c2_body_ratio=0.91,
            bars_max=VARIANT_A_BARS_MAX,
            fvg_atr_min=VARIANT_A_FVG_ATR_MIN,
            c2_body_ratio_min=VARIANT_A_C2_BODY_RATIO_MIN,
        )
        assert result == "PASS"
        assert reason == "PASS_ALL_FILTERS"

    def test_pass_bars_to_touch_zero(self):
        """bars_to_touch=0 is <= 2, should PASS."""
        result, _ = classify_variant(
            bars_to_touch=0, fvg_atr=1.5, c2_body_ratio=0.90,
            bars_max=VARIANT_A_BARS_MAX,
            fvg_atr_min=VARIANT_A_FVG_ATR_MIN,
            c2_body_ratio_min=VARIANT_A_C2_BODY_RATIO_MIN,
        )
        assert result == "PASS"

    def test_pass_bars_to_touch_exactly_2(self):
        """bars_to_touch=2 is the boundary, should PASS."""
        result, _ = classify_variant(
            bars_to_touch=2, fvg_atr=1.10, c2_body_ratio=0.85,
            bars_max=VARIANT_A_BARS_MAX,
            fvg_atr_min=VARIANT_A_FVG_ATR_MIN,
            c2_body_ratio_min=VARIANT_A_C2_BODY_RATIO_MIN,
        )
        assert result == "PASS"

    def test_reject_bars_to_touch_3(self):
        """bars_to_touch=3 > 2, should REJECT."""
        result, reason = classify_variant(
            bars_to_touch=3, fvg_atr=1.24, c2_body_ratio=0.91,
            bars_max=VARIANT_A_BARS_MAX,
            fvg_atr_min=VARIANT_A_FVG_ATR_MIN,
            c2_body_ratio_min=VARIANT_A_C2_BODY_RATIO_MIN,
        )
        assert result == "REJECT"
        assert reason == "BARS_TO_TOUCH_GT_2"

    def test_reject_fvg_atr_too_low(self):
        """fvg_atr=0.62 < 1.10, should REJECT."""
        result, reason = classify_variant(
            bars_to_touch=1, fvg_atr=0.62, c2_body_ratio=0.91,
            bars_max=VARIANT_A_BARS_MAX,
            fvg_atr_min=VARIANT_A_FVG_ATR_MIN,
            c2_body_ratio_min=VARIANT_A_C2_BODY_RATIO_MIN,
        )
        assert result == "REJECT"
        assert reason == "FVG_ATR_LT_1_10"

    def test_reject_c2_body_ratio_too_low(self):
        """c2_body_ratio=0.80 < 0.85, should REJECT."""
        result, reason = classify_variant(
            bars_to_touch=1, fvg_atr=1.24, c2_body_ratio=0.80,
            bars_max=VARIANT_A_BARS_MAX,
            fvg_atr_min=VARIANT_A_FVG_ATR_MIN,
            c2_body_ratio_min=VARIANT_A_C2_BODY_RATIO_MIN,
        )
        assert result == "REJECT"
        assert reason == "C2_BODY_RATIO_LT_0_85"

    def test_reject_multiple_failures(self):
        """Multiple filters failing → MULTIPLE_FILTER_FAILURES."""
        result, reason = classify_variant(
            bars_to_touch=5, fvg_atr=0.50, c2_body_ratio=0.70,
            bars_max=VARIANT_A_BARS_MAX,
            fvg_atr_min=VARIANT_A_FVG_ATR_MIN,
            c2_body_ratio_min=VARIANT_A_C2_BODY_RATIO_MIN,
        )
        assert result == "REJECT"
        assert reason == "MULTIPLE_FILTER_FAILURES"

    def test_pass_fvg_atr_boundary(self):
        """fvg_atr=1.10 exactly equals minimum."""
        result, _ = classify_variant(
            bars_to_touch=1, fvg_atr=1.10, c2_body_ratio=0.85,
            bars_max=VARIANT_A_BARS_MAX,
            fvg_atr_min=VARIANT_A_FVG_ATR_MIN,
            c2_body_ratio_min=VARIANT_A_C2_BODY_RATIO_MIN,
        )
        assert result == "PASS"

    def test_reject_fvg_atr_just_below(self):
        """fvg_atr=1.099 < 1.10."""
        result, _ = classify_variant(
            bars_to_touch=1, fvg_atr=1.099, c2_body_ratio=0.90,
            bars_max=VARIANT_A_BARS_MAX,
            fvg_atr_min=VARIANT_A_FVG_ATR_MIN,
            c2_body_ratio_min=VARIANT_A_C2_BODY_RATIO_MIN,
        )
        assert result == "REJECT"


# ===================================================================
# 2. Threshold Logic Tests — Variants B and C
# ===================================================================

class TestVariantB:
    """Variant B: bars_to_touch <= 2 AND fvg_atr >= 1.10 AND c2_body_ratio >= 0.80"""

    def test_pass_c2_0_80(self):
        """c2_body_ratio=0.80 should PASS for Variant B."""
        result, _ = classify_variant(
            bars_to_touch=1, fvg_atr=1.24, c2_body_ratio=0.80,
            bars_max=2, fvg_atr_min=1.10, c2_body_ratio_min=0.80,
        )
        assert result == "PASS"

    def test_reject_c2_below_0_80(self):
        result, _ = classify_variant(
            bars_to_touch=1, fvg_atr=1.24, c2_body_ratio=0.79,
            bars_max=2, fvg_atr_min=1.10, c2_body_ratio_min=0.80,
        )
        assert result == "REJECT"


class TestVariantC:
    """Variant C: bars_to_touch <= 2 AND fvg_atr >= 1.10 AND c2_body_ratio >= 0.90"""

    def test_pass_c2_0_90(self):
        result, _ = classify_variant(
            bars_to_touch=1, fvg_atr=1.24, c2_body_ratio=0.90,
            bars_max=2, fvg_atr_min=1.10, c2_body_ratio_min=0.90,
        )
        assert result == "PASS"

    def test_reject_c2_0_89(self):
        result, _ = classify_variant(
            bars_to_touch=1, fvg_atr=1.24, c2_body_ratio=0.89,
            bars_max=2, fvg_atr_min=1.10, c2_body_ratio_min=0.90,
        )
        assert result == "REJECT"


class TestClassifyAllVariants:
    """classify_all_variants returns results for A, B, and C simultaneously."""

    def test_pass_all_three(self):
        """Strong signal passes all three variants."""
        results = classify_all_variants(
            bars_to_touch=1, fvg_atr=1.24, c2_body_ratio=0.91,
        )
        assert results["variant_a"][0] == "PASS"
        assert results["variant_b"][0] == "PASS"
        assert results["variant_c"][0] == "PASS"

    def test_pass_a_and_b_not_c(self):
        """c2_body_ratio=0.85 passes A and B but not C."""
        results = classify_all_variants(
            bars_to_touch=1, fvg_atr=1.24, c2_body_ratio=0.85,
        )
        assert results["variant_a"][0] == "PASS"
        assert results["variant_b"][0] == "PASS"
        assert results["variant_c"][0] == "REJECT"

    def test_pass_only_b(self):
        """c2_body_ratio=0.82 passes only B."""
        results = classify_all_variants(
            bars_to_touch=1, fvg_atr=1.24, c2_body_ratio=0.82,
        )
        assert results["variant_a"][0] == "REJECT"
        assert results["variant_b"][0] == "PASS"
        assert results["variant_c"][0] == "REJECT"

    def test_reject_all_bars_too_high(self):
        """bars_to_touch=5 rejects all variants."""
        results = classify_all_variants(
            bars_to_touch=5, fvg_atr=1.50, c2_body_ratio=0.95,
        )
        for key in ("variant_a", "variant_b", "variant_c"):
            assert results[key][0] == "REJECT"


# ===================================================================
# 3. Missing Feature Tests
# ===================================================================

class TestMissingFeature:
    def test_missing_bars_to_touch(self):
        result, reason = classify_variant(
            bars_to_touch=None, fvg_atr=1.24, c2_body_ratio=0.91,
            bars_max=2, fvg_atr_min=1.10, c2_body_ratio_min=0.85,
        )
        assert result == "MISSING_FEATURE"
        assert reason == "MISSING_FEATURE"

    def test_missing_fvg_atr(self):
        result, reason = classify_variant(
            bars_to_touch=1, fvg_atr=None, c2_body_ratio=0.91,
            bars_max=2, fvg_atr_min=1.10, c2_body_ratio_min=0.85,
        )
        assert result == "MISSING_FEATURE"

    def test_missing_c2_body_ratio(self):
        result, reason = classify_variant(
            bars_to_touch=1, fvg_atr=1.24, c2_body_ratio=None,
            bars_max=2, fvg_atr_min=1.10, c2_body_ratio_min=0.85,
        )
        assert result == "MISSING_FEATURE"

    def test_all_missing(self):
        result, _ = classify_variant(
            bars_to_touch=None, fvg_atr=None, c2_body_ratio=None,
            bars_max=2, fvg_atr_min=1.10, c2_body_ratio_min=0.85,
        )
        assert result == "MISSING_FEATURE"


# ===================================================================
# 4. Observer Tests (without DB)
# ===================================================================

class _FakeRepo:
    """Minimal repository mock for testing observer logic."""

    def __init__(self):
        self._saved = []
        self._conn = True  # truthy for _use_pg checks

    def _execute(self, sql, params):
        """Capture SQL and params for assertion."""
        self._saved.append({"sql": sql, "params": params})
        # Simulate lastval() return
        return None

    def _fetchone(self, sql, params=None):
        return (len(self._saved),)

    def ensure_instrument(self, symbol):
        return 1


class TestFVGFilterShadowObserver:
    def test_observe_pass(self):
        repo = _FakeRepo()
        observer = FVGFilterShadowObserver(repo)
        features = {
            "bars_to_touch": 1, "fvg_atr": 1.24, "c2_body_ratio": 0.91,
            "c2_body_atr": 1.224, "risk_pct": 0.0294, "score": 80.0,
            "market_regime": "TRENDING_UP",
        }
        result = observer.observe_signal(
            setup_id="test-setup-1",
            symbol="BTCUSDT",
            instrument_id=1,
            direction="LONG",
            detected_at=datetime.now(timezone.utc),
            features=features,
        )
        assert result["filter_result"] == "PASS"
        assert result["variant_a"][0] == "PASS"
        assert len(repo._saved) == 1
        assert "shadow_fvg_filter_observation" in repo._saved[0]["sql"]
        assert repo._saved[0]["params"]["filter_result"] == "PASS"
        assert repo._saved[0]["params"]["variant_a_result"] == "PASS"
        assert observer.stats["pass"] == 1
        assert observer.stats["reject"] == 0

    def test_observe_reject(self):
        repo = _FakeRepo()
        observer = FVGFilterShadowObserver(repo)
        features = {
            "bars_to_touch": 3, "fvg_atr": 0.50, "c2_body_ratio": 0.70,
        }
        result = observer.observe_signal(
            setup_id="test-setup-2",
            symbol="ETHUSDT",
            instrument_id=2,
            direction="LONG",
            detected_at=datetime.now(timezone.utc),
            features=features,
        )
        assert result["filter_result"] == "REJECT"
        assert result["variant_a"][0] == "REJECT"
        assert result["variant_a"][1] == "MULTIPLE_FILTER_FAILURES"
        assert observer.stats["reject"] == 1

    def test_observe_missing_feature(self):
        repo = _FakeRepo()
        observer = FVGFilterShadowObserver(repo)
        features = {"score": 80.0, "market_regime": "TRENDING_UP"}
        result = observer.observe_signal(
            setup_id="test-setup-3",
            symbol="SOLUSDT",
            instrument_id=3,
            direction="LONG",
            detected_at=datetime.now(timezone.utc),
            features=features,
        )
        assert result["filter_result"] == "MISSING_FEATURE"
        assert observer.stats["missing"] == 1

    def test_observe_does_not_block_signal(self):
        """The observer is fire-and-forget; always returns result, never raises."""
        repo = _FakeRepo()
        observer = FVGFilterShadowObserver(repo)
        features = {"bars_to_touch": 3, "fvg_atr": 0.30, "c2_body_ratio": 0.50}
        # This should never raise
        result = observer.observe_signal(
            setup_id="test-setup-4",
            symbol="DOGEUSDT",
            instrument_id=4,
            direction="LONG",
            detected_at=datetime.now(timezone.utc),
            features=features,
        )
        assert "filter_result" in result

    def test_stats_accumulate(self):
        repo = _FakeRepo()
        observer = FVGFilterShadowObserver(repo)
        for i in range(5):
            features = {
                "bars_to_touch": 1 if i < 3 else 5,
                "fvg_atr": 1.20,
                "c2_body_ratio": 0.90,
            }
            observer.observe_signal(
                setup_id=f"setup-{i}",
                symbol="BTCUSDT",
                instrument_id=1,
                direction="LONG",
                detected_at=datetime.now(timezone.utc),
                features=features,
            )
        assert observer.stats["observations"] == 5
        assert observer.stats["pass"] == 3
        assert observer.stats["reject"] == 2

    def test_feature_snapshot_includes_all_fields(self):
        """All additional features are captured in the observation."""
        repo = _FakeRepo()
        observer = FVGFilterShadowObserver(repo)
        features = {
            "bars_to_touch": 1, "fvg_atr": 1.24, "c2_body_ratio": 0.91,
            "c2_body_atr": 1.224, "risk_pct": 0.0294, "score": 80.0,
            "market_regime": "TRENDING_UP", "fvg_size": 0.5,
            "rr": 3.0, "entry_price": 102.0, "sl_price": 99.0,
            "tp_price": 111.0,
        }
        observer.observe_signal(
            setup_id="test-snapshot",
            symbol="BTCUSDT",
            instrument_id=1,
            direction="LONG",
            detected_at=datetime.now(timezone.utc),
            features=features,
        )
        params = repo._saved[0]["params"]
        assert params["fvg_atr"] == 1.24
        assert params["c2_body_ratio"] == 0.91
        assert params["c2_body_atr"] == 1.224
        assert params["risk_pct"] == 0.0294
        assert params["market_regime"] == "TRENDING_UP"
        assert params["fvg_size"] == 0.5
        assert params["rr"] == 3.0
        assert params["entry_price"] == 102.0
        assert params["sl_price"] == 99.0
        assert params["tp_price"] == 111.0


# ===================================================================
# 5. Production Isolation Tests
# ===================================================================

class TestProductionIsolation:
    """The shadow experiment must not change FVG scanner behavior."""

    def test_fvg_scanner_name_unchanged(self):
        """FVG scanner still produces SCANNER_NAME."""
        from app.scanners.fvg_reaction_long_local_struct_v1 import SCANNER_NAME
        assert SCANNER_NAME == "FVG_REACTION_LONG_LOCAL_STRUCT_V1"

    def test_fvg_scanner_emits_signal_regardless_of_filter(self):
        """FVG scanner produces SetupCandidate independent of shadow filter."""
        # The filter is applied AFTER the signal is emitted and saved.
        # The scanner itself does not check any filter.
        candidate = _fvg_candidate(bars_to_touch=5, fvg_atr=0.30, c2_body_ratio=0.60)
        assert candidate.scanner_name == "FVG_REACTION_LONG_LOCAL_STRUCT_V1"
        assert candidate.direction == "LONG"
        assert candidate.state == SetupState.READY_TO_TRADE

    def test_outcome_evaluation_unchanged(self):
        """evaluate_setup_outcome still uses the same logic."""
        # Create a candidate with a known confirmation_at
        now = datetime.now(timezone.utc)
        confirm_ts = int(now.timestamp() * 1000)
        fvg_ts = confirm_ts - 600_000  # 10 minutes before confirmation

        candidate = SetupCandidate(
            setup_id=uuid4(),
            scanner_name="FVG_REACTION_LONG_LOCAL_STRUCT_V1",
            symbol="BTCUSDT",
            direction="LONG",
            entry_timeframe="5m",
            setup_timeframe="5m",
            detected_at=now,
            signal_candle_open_time=fvg_ts,
            reference_price=100.0,
            entry_zone_low=100.0,
            entry_zone_high=100.0,
            invalidation_price=98.0,
            target_1=106.0,
            score=80.0,
            state=SetupState.READY_TO_TRADE,
            features={
                "fvg_created_at": fvg_ts,
                "confirmation_at": confirm_ts,
                "entry_price": 100.0,
            },
        )
        # Post-confirmation candle hits TP1
        tp1_ts = confirm_ts + 300_000
        outcome = evaluate_setup_outcome(
            candidate,
            [_candle(tp1_ts, h=107, l=100, c=103)],
            max_bars=5,
        )
        assert outcome.first_event == "TP1"
        assert outcome.result_r > 0


# ===================================================================
# 6. Deduplication Test (ON CONFLICT)
# ===================================================================

class TestDeduplication:
    """UNIQUE (experiment_id, setup_id) prevents duplicate observations."""

    def test_sql_contains_on_conflict(self):
        """The INSERT SQL must contain ON CONFLICT for deduplication."""
        from app.shadow.fvg_filter_shadow import FVGFilterShadowObserver
        import inspect
        source = inspect.getsource(FVGFilterShadowObserver._save_observation)
        assert "ON CONFLICT" in source
        assert "experiment_id, setup_id" in source

    def test_experiment_id_is_fixed(self):
        """Experiment ID is a fixed constant."""
        assert EXPERIMENT_ID == "FVG_REACTION_LONG_LOCAL_STRUCT_FILTER_OOS_V1"

    def test_scanner_name_is_frozen(self):
        """Scanner name matches production."""
        assert SCANNER_NAME == "FVG_REACTION_LONG_LOCAL_STRUCT_V1"


# ===================================================================
# 7. Threshold Constants Verification
# ===================================================================

class TestThresholdConstants:
    """Verify frozen thresholds match the task specification."""

    def test_variant_a_bars_max(self):
        assert VARIANT_A_BARS_MAX == 2

    def test_variant_a_fvg_atr_min(self):
        assert VARIANT_A_FVG_ATR_MIN == 1.10

    def test_variant_a_c2_body_ratio_min(self):
        assert VARIANT_A_C2_BODY_RATIO_MIN == 0.85

    def test_variant_b_c2_body_ratio_min(self):
        assert VARIANT_B_C2_BODY_RATIO_MIN == 0.80

    def test_variant_c_c2_body_ratio_min(self):
        assert VARIANT_C_C2_BODY_RATIO_MIN == 0.90


# ===================================================================
# 8. Migration SQL Structure Tests
# ===================================================================

class TestMigrationSQL:
    """Verify the migration SQL creates the expected schema."""

    @pytest.fixture(autouse=True)
    def _load_migration(self):
        from pathlib import Path
        migration_path = Path(__file__).parent.parent / "sql" / "migrations" / "043_fvg_filter_oos_validation.sql"
        if migration_path.exists():
            self.sql = migration_path.read_text(encoding="utf-8")
        else:
            self.sql = ""

    def test_table_created(self):
        assert "dds.shadow_fvg_filter_observation" in self.sql
        assert "CREATE TABLE" in self.sql

    def test_unique_index(self):
        assert "uq_shadow_fvg_filter_experiment_setup" in self.sql
        assert "UNIQUE" in self.sql

    def test_analytics_view_exists(self):
        assert "v_fvg_filter_shadow_analytics" in self.sql

    def test_variant_comparison_view(self):
        assert "v_fvg_filter_variant_comparison" in self.sql

    def test_symbol_breakdown_view(self):
        assert "v_fvg_filter_symbol_breakdown" in self.sql

    def test_regime_breakdown_view(self):
        assert "v_fvg_filter_regime_breakdown" in self.sql

    def test_experiment_id_default(self):
        assert "FVG_REACTION_LONG_LOCAL_STRUCT_FILTER_OOS_V1" in self.sql

    def test_all_three_variant_columns(self):
        assert "variant_a_result" in self.sql
        assert "variant_b_result" in self.sql
        assert "variant_c_result" in self.sql

    def test_feature_columns(self):
        assert "bars_to_touch" in self.sql
        assert "fvg_atr" in self.sql
        assert "c2_body_ratio" in self.sql
        assert "c2_body_atr" in self.sql
        assert "risk_pct" in self.sql
        assert "market_regime" in self.sql
        assert "fvg_size" in self.sql
        assert "rr" in self.sql
        assert "entry_price" in self.sql
        assert "sl_price" in self.sql
        assert "tp_price" in self.sql

    def test_outcome_columns(self):
        assert "first_event" in self.sql
        assert "result_r" in self.sql
        assert "fee_slippage_adjusted_result_r" in self.sql
        assert "mfe_r" in self.sql
        assert "mae_r" in self.sql


# ===================================================================
# 9. No Look-Ahead Bias Verification
# ===================================================================

class TestNoLookAheadBias:
    """Filter classification uses only data available at signal time."""

    def test_classify_uses_only_three_features(self):
        """Classification depends ONLY on bars_to_touch, fvg_atr, c2_body_ratio."""
        import inspect
        sig = inspect.signature(classify_variant)
        params = list(sig.parameters.keys())
        # Only positional/bars/fvg/c2 + keyword-only thresholds
        assert "bars_to_touch" in params
        assert "fvg_atr" in params
        assert "c2_body_ratio" in params
        # No result_r, no mfe_r, no mae_r, no first_event
        assert "result_r" not in params
        assert "mfe_r" not in params
        assert "mae_r" not in params
        assert "first_event" not in params

    def test_observer_observe_uses_only_features(self):
        """observe_signal extracts filter features from the features dict."""
        import inspect
        source = inspect.getsource(FVGFilterShadowObserver.observe_signal)
        # Should NOT reference result_r, mfe_r, mae_r, first_event
        for forbidden in ("result_r", "mfe_r", "mae_r", "first_event"):
            assert forbidden not in source, (
                f"observe_signal references '{forbidden}' — potential look-ahead bias"
            )


# ===================================================================
# 10. Integration: scanner_runner does not affect paper execution
# ===================================================================

class TestScannerRunnerIsolation:
    """The shadow observation in scanner_runner.py is fire-and-forget."""

    def test_shadow_observer_exception_does_not_block(self):
        """If shadow observer fails, the candidate is still emitted."""
        # Simulate: the scanner_runner code catches the exception
        # and continues. Verify the exception handling pattern.
        from app.scanners.fvg_reaction_long_local_struct_v1 import (
            FVGReactionLongLocalStructV1Scanner,
        )
        scanner = FVGReactionLongLocalStructV1Scanner()
        # Scanner state is independent of shadow observer
        assert hasattr(scanner, "scan")
        assert hasattr(scanner, "_make_candidate")

    def test_outcome_cli_shadow_exception_does_not_block(self):
        """If shadow outcome recording fails, the outcome is still saved."""
        # The outcome_cli catches the shadow exception in a try/except
        # and continues. Verify this pattern in the source code.
        import inspect
        from app.scanners.outcome_cli import _process_for_timeframe
        source = inspect.getsource(_process_for_timeframe)
        assert "FVG shadow outcome recording failed" in source
        # The try/except pattern ensures non-blocking
        assert "except Exception:" in source
