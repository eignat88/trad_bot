"""Protocol matrix tests for the ME_R_LONG early MAE V2 shadow pipeline."""
from __future__ import annotations

import inspect
import pytest
from datetime import datetime, timezone

from app.research.me_r_long_early_mae_v2.protocol import (
    ACTIVATION_AT,
    FROZEN_AT,
    BASELINE_MAX_HOLD_MINUTES,
    ENTRY_EXPIRY_MINUTES,
    INTRABAR_POLICY,
    PROTOCOL_VERSION,
    SCANNER_NAME,
    SCANNER_VERSION,
    SOURCE_DIRECTION,
    VARIANTS,
    protocol_payload,
    validate_source_geometry,
)
from app.research.me_r_long_early_mae_v2.pipeline import MeEarlyMaeV2Pipeline
from app.scanners.momentum_exhaustion_reverse_long_v1 import (
    MomentumExhaustionReverseLongV1Scanner,
)


UTC = timezone.utc


def test_protocol_version_is_distinct_from_v1():
    assert PROTOCOL_VERSION == "2.1.0"
    assert SCANNER_NAME == "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1"
    assert SCANNER_VERSION == "1.0.0"
    assert SOURCE_DIRECTION == "LONG"


def test_baseline_and_entry_windows_are_frozen():
    assert ENTRY_EXPIRY_MINUTES == 240
    assert BASELINE_MAX_HOLD_MINUTES == 240
    assert INTRABAR_POLICY == "STOP_FIRST"


def test_protocol_payload_is_strict_research_only():
    payload = protocol_payload(
        datetime(2026, 10, 9, 23, 59, tzinfo=UTC),
        datetime(2026, 10, 10, 0, 0, tzinfo=UTC),
    )
    assert payload["paper_live_side_effects"] == "FORBIDDEN"
    assert payload["historical_backfill"] == "FORBIDDEN"
    assert payload["reference_price_as_fill"] == "FORBIDDEN"
    assert payload["old_table_reuse"] == "FORBIDDEN"
    assert len(payload["variants"]) == 7


def test_v1_scanner_is_unchanged_by_protocol_import():
    scanner = MomentumExhaustionReverseLongV1Scanner()
    assert scanner.name == SCANNER_NAME
    assert scanner.version == SCANNER_VERSION
    assert scanner.swing_lookback == 5
    assert scanner.exhaustion_threshold == 0.003
    source = inspect.getsource(MomentumExhaustionReverseLongV1Scanner)
    assert "ME_R_LONG_EARLY_MAE_EXIT_OOS_V2" not in source


def test_frozen_geometry_rejects_invalid_stop_and_target():
    assert validate_source_geometry(99.8, 100.1, 97.5, 103.0) == (True, None)
    assert validate_source_geometry(99.8, 100.1, 100.0, 103.0)[0] is False
    assert validate_source_geometry(99.8, 100.1, 97.5, 100.0)[0] is False
    assert validate_source_geometry(100.1, 99.8, 97.5, 103.0)[0] is False


def test_no_pre_freeze_activation_from_historical_rows():
    assert FROZEN_AT is None
    assert ACTIVATION_AT is None
    with pytest.raises(RuntimeError, match="FREEZE_NOT_CONFIGURED"):
        protocol_payload()


def test_pipeline_public_api_is_isolated():
    source = inspect.getsource(MeEarlyMaeV2Pipeline)
    for forbidden in ("dds.paper_trade", "PaperTradingEngine", "paper_runner", "close_trade"):
        assert forbidden not in source
