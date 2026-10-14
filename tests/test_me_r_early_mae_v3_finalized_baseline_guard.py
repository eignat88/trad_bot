import pytest

from app.research.me_r_long_early_mae_v3.repository import (
    MeEarlyMaeV3Repository,
)
from app.research.me_r_long_early_mae_v3.protocol import VARIANTS


class ForbiddenConnection:
    def cursor(self):
        raise AssertionError("DATABASE_MUST_NOT_BE_ACCESSED")

    def commit(self):
        raise AssertionError("COMMIT_FORBIDDEN")

    def rollback(self):
        raise AssertionError("ROLLBACK_FORBIDDEN")


def test_ambiguous_baseline_rejected_before_database_access():
    repository = MeEarlyMaeV3Repository(ForbiddenConnection())

    baseline = {
        "baseline_exit_reason": "AMBIGUOUS_STOP_FIRST",
    }

    evaluations = [
        {
            "variant_id": variant.variant_id,
            "variant_status": "EVALUATED",
            "ambiguous_ordering": False,
        }
        for variant in VARIANTS
    ]

    with pytest.raises(
        ValueError,
        match="AMBIGUOUS_BASELINE_CANNOT_FINALIZE",
    ):
        repository.save_paired_outcome(
            source_event_id=999999,
            baseline=baseline,
            evaluations=evaluations,
        )
