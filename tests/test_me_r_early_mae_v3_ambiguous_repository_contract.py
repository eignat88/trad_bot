import pytest

from app.research.me_r_long_early_mae_v3.protocol import VARIANTS
from app.research.me_r_long_early_mae_v3.repository import (
    MeEarlyMaeV3Repository,
)


class ForbiddenConnection:
    def cursor(self):
        raise AssertionError("DB_ACCESS_BEFORE_VALIDATION")


def rows():
    result = []
    for variant in VARIANTS:
        result.append({
            "variant_id": variant.variant_id,
            "variant_status": "EVALUATED",
            "ambiguous_ordering": False,
            "coverage_reason": None,
            "variant_exit_at": "2026-01-01T00:10:00Z",
            "variant_exit_price": 100.0,
            "variant_exit_reason": "TAKE_PROFIT",
            "variant_gross_r": 1.0,
            "net_r_normal": 0.9,
            "net_r_elevated": 0.8,
            "delta_net_r_normal": 0.1,
            "delta_net_r_elevated": 0.0,
            "trigger_result": "BASELINE_SL_TP_PRECEDENCE",
            "mae_r": 0.5,
            "mfe_r": 1.0,
            "window_low": 99.5,
            "window_high": 101.0,
            "horizon_end_at": "2026-01-01T00:10:00Z",
            "execution_price_semantics": "BASELINE_EXECUTION_PROXY",
        })
    return result


def baseline():
    return {
        "source_status": "AMBIGUOUS_NONFINAL",
        "coverage_status": "COMPLETE",
        "baseline_exit_at": "2026-01-01T00:10:00Z",
        "baseline_exit_price": 99.0,
        "baseline_exit_reason": "AMBIGUOUS_STOP_FIRST",
        "baseline_gross_r": -1.0,
        "baseline_net_r_normal": -1.1,
        "baseline_net_r_elevated": -1.2,
    }


def repository():
    return MeEarlyMaeV3Repository(ForbiddenConnection())


def test_diagnostic_method_exists():
    assert callable(getattr(repository(), "save_ambiguous_outcome", None))


def test_clean_tuple_cannot_enter_ambiguous_path():
    with pytest.raises(ValueError, match="AMBIGUOUS"):
        repository().save_ambiguous_outcome(1, baseline(), rows())


def test_incomplete_tuple_rejected_before_database():
    values = rows()
    values[0]["variant_status"] = "INCOMPLETE_COVERAGE"
    values[0]["coverage_reason"] = "MISSING_CANDLE"
    values[1]["variant_status"] = "AMBIGUOUS"
    values[1]["ambiguous_ordering"] = True

    with pytest.raises(ValueError, match="INCOMPLETE"):
        repository().save_ambiguous_outcome(1, baseline(), values)


def test_seven_variant_identity_required():
    values = rows()[:-1]
    values[0]["variant_status"] = "AMBIGUOUS"
    values[0]["ambiguous_ordering"] = True

    with pytest.raises(ValueError, match="SEVEN_VARIANTS"):
        repository().save_ambiguous_outcome(1, baseline(), values)
