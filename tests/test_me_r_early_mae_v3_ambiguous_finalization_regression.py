from app.research.me_r_long_early_mae_v3.lifecycle import can_finalize
from app.research.me_r_long_early_mae_v3.protocol import VARIANTS


def rows():
    return [
        {
            "variant_id": v.variant_id,
            "variant_status": "EVALUATED",
            "coverage_reason": None,
            "net_r_normal": 0.1,
            "net_r_elevated": 0.05,
            "delta_net_r_normal": 0.0,
            "delta_net_r_elevated": 0.0,
            "variant_exit_at": "2026-01-01T00:10:00Z",
            "ambiguous_ordering": False,
        }
        for v in VARIANTS
    ]


def test_clean_seven_variant_tuple_can_finalize():
    assert can_finalize(rows())


def test_one_ambiguous_variant_blocks_entire_tuple():
    evaluations = rows()
    evaluations[0]["variant_status"] = "AMBIGUOUS"
    evaluations[0]["ambiguous_ordering"] = True

    assert not can_finalize(evaluations)


def test_ambiguous_flag_blocks_finalization_even_if_status_evaluated():
    evaluations = rows()
    evaluations[0]["ambiguous_ordering"] = True

    assert not can_finalize(evaluations)


def test_all_ambiguous_variants_cannot_finalize():
    evaluations = rows()

    for evaluation in evaluations:
        evaluation["variant_status"] = "AMBIGUOUS"
        evaluation["ambiguous_ordering"] = True

    assert not can_finalize(evaluations)
