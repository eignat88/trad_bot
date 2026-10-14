import os
from datetime import datetime, timedelta, timezone
from app.research.me_r_long_early_mae_v3.runtime_integration import optionally_capture_source

class FakePipeline:
    def capture_source(self, source):
        raise AssertionError("capture must not run without explicit approval")

def test_disabled_runtime_is_noop():
    os.environ.pop("ME_EARLY_MAE_V3_CAPTURE_ENABLED", None)
    assert optionally_capture_source(FakePipeline(), {}, datetime.now(timezone.utc)) is None

def test_no_default_config_does_not_raise():
    for key in (
        "ME_EARLY_MAE_V3_CAPTURE_ENABLED",
        "ME_EARLY_MAE_V3_FROZEN_AT",
        "ME_EARLY_MAE_V3_ACTIVATION_AT",
        "ME_EARLY_MAE_V3_PROTOCOL_FINGERPRINT",
    ):
        os.environ.pop(key, None)
    assert optionally_capture_source(FakePipeline(), {}, datetime.now(timezone.utc)) is None
