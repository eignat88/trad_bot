import sys

import pytest

from app.research.me_r_long_early_mae_v3 import runner


def test_runner_remains_disabled_in_draft(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["runner", "--once"])

    def forbidden_pipeline(*args, **kwargs):
        raise AssertionError("PIPELINE_MUST_NOT_BE_CREATED")

    monkeypatch.setattr(
        runner,
        "MeEarlyMaeV3Pipeline",
        forbidden_pipeline,
    )

    with pytest.raises(
        SystemExit,
        match="ME_R_EARLY_MAE_V3_CAPTURE_DISABLED",
    ):
        runner.main()
