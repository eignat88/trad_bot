"""Terminal lifecycle regression tests."""

from app.research.me_r_long_early_mae_v2.lifecycle import can_finalize_source
from app.research.me_r_long_early_mae_v2.protocol import VARIANTS


def make_evaluations(status="EVALUATED_NO_TRIGGER"):
    return [
        {
            "variant_id": variant.variant_id,
            "variant_status": status,
            "variant_net_r": -0.084,
            "delta_net_r": 0.0,
            "variant_exit_at": "2026-10-12T12:00:00Z",
        }
        for variant in VARIANTS
    ]


def test_complete_seven_variants_finalize():
    assert can_finalize_source(make_evaluations())


def test_ambiguous_variants_allow_finalization():
    evaluations = make_evaluations()
    evaluations[0]["variant_status"] = "AMBIGUOUS"
    assert can_finalize_source(evaluations)


def test_incomplete_coverage_blocks_finalization():
    evaluations = make_evaluations()
    evaluations[0]["variant_status"] = "INCOMPLETE_COVERAGE"
    assert not can_finalize_source(evaluations)


def test_missing_variant_blocks_finalization():
    assert not can_finalize_source(make_evaluations()[:-1])


def test_duplicate_variant_blocks_finalization():
    evaluations = make_evaluations()
    evaluations[-1]["variant_id"] = evaluations[0]["variant_id"]
    assert not can_finalize_source(evaluations)


def test_missing_exit_blocks_finalization():
    evaluations = make_evaluations()
    evaluations[0]["variant_exit_at"] = None
    assert not can_finalize_source(evaluations)
