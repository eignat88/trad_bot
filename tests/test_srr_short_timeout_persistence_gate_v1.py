from datetime import datetime

import pytest

from app.research.srr_short_execution_outcome_persistence import (
    SrrOutcomeWriter,
)
from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
    SrrShortExecutionRExpansionProspectiveEvaluator,
)
from app.research.srr_short_timeout_persistence_gate import (
    prepare_srr_persistence_result,
)
from tests.test_srr_short_prospective_evaluator_policy_routing_v1 import (
    _ArchiveSource, _FakeConn, _clean_candles, _obs, SIGNAL_MS,
)

CUTOFF = SIGNAL_MS + 120 * 60_000


def make_result(asof):
    obs = _obs()
    obs["signal_time"] = datetime.fromisoformat(
        obs["signal_time"].replace("Z", "+00:00")
    )
    evaluator = SrrShortExecutionRExpansionProspectiveEvaluator(
        conn=_FakeConn(),
        candle_source=_ArchiveSource(_clean_candles()),
        dry_run=True,
    )
    return obs, evaluator.route(obs, asof)


@pytest.mark.parametrize("seconds_before", [50, 49, 1])
def test_early_timeout_is_nonfinal_and_has_no_economics(seconds_before):
    asof = CUTOFF - seconds_before * 1000
    obs, result = make_result(asof)

    assert result.finalization_eligible
    assert result.path_class == "TIMEOUT"

    persisted = prepare_srr_persistence_result(
        result, evaluation_asof_ms=asof,
    )

    assert persisted["finalization_eligible"] is False
    assert persisted["reason_code"] == "TIMEOUT_CUTOFF_NOT_REACHED"

    for key in (
        "gross_r", "cost_r_normal", "cost_r_elevated",
        "net_r_normal", "net_r_elevated",
    ):
        assert persisted[key] is None

    values = SrrOutcomeWriter._build_values(
        observation_id=obs["observation_id"],
        experiment_id=obs["experiment_id"],
        observation=obs,
        result=persisted,
        is_final=False,
    )
    assert values["is_final"] is False


def test_timeout_finalizes_at_cutoff():
    obs, result = make_result(CUTOFF)

    persisted = prepare_srr_persistence_result(
        result, evaluation_asof_ms=CUTOFF,
    )

    assert persisted["finalization_eligible"] is True
    assert persisted["path_class"] == "TIMEOUT"
    assert persisted["net_r_normal"] == pytest.approx(-0.21)


def test_frozen_policy_result_is_not_mutated():
    asof = CUTOFF - 1000
    _, result = make_result(asof)

    prepare_srr_persistence_result(
        result, evaluation_asof_ms=asof,
    )

    assert result.finalization_eligible is True
    assert result.path_class == "TIMEOUT"
    assert result.net_r_normal == pytest.approx(-0.21)


def test_callback_persistence_boundary_contract():
    from dataclasses import asdict

    for seconds_before in (50, 1, 0):
        asof = CUTOFF - seconds_before * 1000
        _, route_result = make_result(asof)

        payload = prepare_srr_persistence_result(
            route_result,
            evaluation_asof_ms=asof,
        )

        assert payload["finalization_eligible"] == (seconds_before == 0)

        if seconds_before:
            assert payload["path_class"] is None
            assert payload["gross_r"] is None
            assert payload["net_r_normal"] is None
        else:
            assert payload["path_class"] == "TIMEOUT"
            assert payload["net_r_normal"] == pytest.approx(-0.21)


def test_evaluator_callback_uses_persistence_gate():
    import inspect

    source = inspect.getsource(
        SrrShortExecutionRExpansionProspectiveEvaluator.run_evaluation_cycle
    )

    assert "prepare_srr_persistence_result(" in source
    assert "result=persistence_result" in source
    assert 'persistence_result["finalization_eligible"]' in source


def test_final_timeout_without_cutoff_is_rejected():
    from dataclasses import replace

    _, result = make_result(CUTOFF)
    invalid_result = replace(result, policy_result={})

    with pytest.raises(ValueError, match="frozen cutoff_time_ms"):
        prepare_srr_persistence_result(
            invalid_result,
            evaluation_asof_ms=CUTOFF,
        )
