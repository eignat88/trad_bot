from dataclasses import asdict
from datetime import datetime

import pytest

from app.research.srr_short_execution_outcome_persistence import SrrOutcomeWriter
from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
    SrrShortExecutionRExpansionProspectiveEvaluator,
)
from tests.test_srr_short_prospective_evaluator_policy_routing_v1 import (
    _ArchiveSource, _FakeConn, _clean_candles, _obs, ASOF,
)


def make_inputs():
    obs = _obs()
    obs["signal_time"] = datetime.fromisoformat(
        obs["signal_time"].replace("Z", "+00:00")
    )

    evaluator = SrrShortExecutionRExpansionProspectiveEvaluator(
        conn=_FakeConn(),
        candle_source=_ArchiveSource(_clean_candles()),
        dry_run=True,
    )

    result = evaluator.route(obs, ASOF)
    assert result.finalization_eligible
    return obs, asdict(result)


def test_real_policy_writer_uses_frozen_intrabar_policy():
    obs, result = make_inputs()

    assert result["policy_result"].get("intrabar_policy") is None

    values = SrrOutcomeWriter._build_values(
        observation_id=obs["observation_id"],
        experiment_id=obs["experiment_id"],
        observation=obs,
        result=result,
        is_final=True,
    )

    assert values["intrabar_policy"] == "STOP_FIRST"
    assert values["is_final"] is True
    assert values["path_class"] == "TIMEOUT"
    assert float(values["net_r_normal"]) == pytest.approx(-0.21)


def test_final_without_frozen_intrabar_policy_is_rejected():
    obs, result = make_inputs()

    import json

    features = json.loads(obs["features"])
    features.pop("_frozen_intrabar_policy")
    obs["features"] = json.dumps(features)

    with pytest.raises(ValueError, match="complete frozen economics"):
        SrrOutcomeWriter._build_values(
            observation_id=obs["observation_id"],
            experiment_id=obs["experiment_id"],
            observation=obs,
            result=result,
            is_final=True,
        )
