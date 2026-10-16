import hashlib
import json
from pathlib import Path

from app.research.me_r_long_early_mae_v3 import protocol as p


SPEC_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs/research/ME_R_V3_PROTOCOL_3_0_0_IMMUTABLE_SPEC.json"
)

APPROVED_SHA256 = (
    "b626700a04b679d67fa4db029e4968dd9feba62123d5145f428e22a87c2e5ed3"
)


def load_spec():
    return json.loads(SPEC_PATH.read_text())


def test_immutable_spec_sha256():
    spec = load_spec()
    canonical = json.dumps(
        spec, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    assert digest == APPROVED_SHA256


def test_protocol_versions_and_parameters_match_spec():
    spec = load_spec()

    expected = {
        "experiment_id": p.EXPERIMENT_ID,
        "protocol_version": p.PROTOCOL_VERSION,
        "scanner_name": p.SCANNER_NAME,
        "scanner_version": p.SCANNER_VERSION,
        "source_direction": p.SOURCE_DIRECTION,
        "source_schema": p.SOURCE_SCHEMA,
        "source_candle_version": p.SOURCE_CANDLE_VERSION,
        "ruleset": p.RULESET,
        "required_timeframes": list(p.REQUIRED_TIMEFRAMES),
        "max_source_delay_seconds": p.MAX_SOURCE_DELAY_SECONDS,
        "entry_model": p.ENTRY_MODEL,
        "entry_boundary": "STRICTLY_NEXT_5M_OPEN_AFTER_ELIGIBILITY",
        "entry_expiry_minutes": p.ENTRY_EXPIRY_MINUTES,
        "baseline_policy": p.BASELINE_POLICY,
        "stop_pct": p.STOP_PCT,
        "target_pct": p.TARGET_PCT,
        "max_hold_minutes": p.BASELINE_MAX_HOLD_MINUTES,
        "intrabar_policy": p.INTRABAR_POLICY,
        "timeout_policy": p.TIMEOUT_POLICY,
        "mae_exit_policy": p.MAE_EXIT_POLICY,
        "mae_execution_proxy": "COMPLETED_5M_CLOSE_AT_HORIZON_BOUNDARY",
        "immediate_first_touch_exit": "FORBIDDEN",
        "baseline_sl_tp_precedence": True,
        "taker_fee_per_side": p.TAKER_FEE_PER_SIDE,
        "normal_slippage_per_side": p.NORMAL_SLIPPAGE_PER_SIDE,
        "elevated_slippage_per_side": p.ELEVATED_SLIPPAGE_PER_SIDE,
        "funding": "NOT_MODELED",
        "variants": [
            {
                "variant_id": v.variant_id,
                "horizon_minutes": v.horizon_minutes,
                "threshold_mae_r": v.threshold_mae_r,
                "role": v.role,
                "is_primary": v.is_primary,
            }
            for v in p.VARIANTS
        ],
        "paper_live_side_effects": "FORBIDDEN",
        "historical_backfill": "FORBIDDEN",
    }

    assert spec == expected


def test_primary_and_secondary_roles():
    variants = load_spec()["variants"]

    assert len(variants) == 7
    primary = [v for v in variants if v["is_primary"]]

    assert len(primary) == 1
    assert primary[0]["variant_id"] == "MAE_15m_050"
    assert primary[0]["role"] == "PRIMARY"

    assert all(
        v["role"] == "SECONDARY"
        for v in variants
        if not v["is_primary"]
    )


def test_activation_not_part_of_immutable_spec():
    spec = load_spec()

    assert "activation_at" not in spec
    assert "frozen_at" not in spec
    assert "protocol_fingerprint" not in spec
    assert spec["paper_live_side_effects"] == "FORBIDDEN"
