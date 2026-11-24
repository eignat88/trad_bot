"""Issue #305: ME_INVERSE_SHORT_V3_UPSTREAM_SOURCE_CAPTURE_V1 — identity & contracts.

Covers the deterministic upstream source identity (``identity.py``) and the
immutable source event contracts (``contracts.py``):

* source_event_key determinism / restart-stability / re-detection stability;
* the key payload path (SHA-256 of the normalized identity) and its
  independence of the capture protocol version;
* signal candle open ms derivation (explicit value vs. 5-minute grid
  fallback) and deterministic IdentityError failures;
* build_source_event acceptance, verbatim feature preservation (no
  mutation of the input candidate) and deterministic rejection reason codes;
* lineage_hash determinism, the frozen comparable lineage subset
  (capture-side and post-scoring keys excluded) and normalize_reason_code.
"""

import copy
import hashlib
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.research.me_upstream_source_v1 import protocol
from app.research.me_upstream_source_v1.contracts import (
    SourceValidationError,
    build_source_event,
    comparable_lineage_hash,
    comparable_lineage_view,
    lineage_hash,
    normalize_reason_code,
)
from app.research.me_upstream_source_v1.identity import (
    IdentityError,
    identity_key_from_normalized,
    normalized_identity,
    normalized_identity_values,
    signal_candle_open_ms,
    source_event_key,
)
from app.scanners.models import SetupCandidate, SetupState

UTC = timezone.utc

#: Fixed signal-candle open (ms since epoch) of the 2026-11-23 15:10 UTC 5m
#: candle (mirrors the value used across the research test suite).
CANDLE_OPEN_MS = 1_795_446_600_000

#: Detection timestamp INSIDE that 15:10-15:15 candle.
DETECTED_AT = datetime(2026, 11, 23, 15, 12, 34, tzinfo=UTC)

#: Another detection timestamp inside the SAME candle (re-detection).
DETECTED_AT_REDETECT = datetime(2026, 11, 23, 15, 14, 0, tzinfo=UTC)

FREEZE_TS = datetime(2026, 11, 23, 0, 0, 0, tzinfo=UTC)
CAPTURED_AT = datetime(2026, 11, 23, 15, 10, 1, tzinfo=UTC)

FIXED_SETUP_ID = UUID("12345678-1234-5678-1234-567812345678")
OTHER_SETUP_ID = UUID("87654321-4321-8765-4321-876543218765")

BASE_FEATURES = {
    "rsi_14": 55.0,
    "rsi_delta_3": 1.25,
    "entry_confirmation_rsi_rising": True,
    "volume_ratio": 0.5,
}


def features_with(**overrides) -> dict:
    feats = dict(BASE_FEATURES)
    feats.update(overrides)
    return feats


def make_candidate(**overrides) -> SetupCandidate:
    base = {
        "setup_id": FIXED_SETUP_ID,
        "scanner_name": protocol.SOURCE_SCANNER,
        "scanner_version": "2.3.1",
        "symbol": "BTCUSDT",
        "direction": protocol.SOURCE_DIRECTION,
        "entry_timeframe": "5m",
        "detected_at": DETECTED_AT,
        "setup_started_at": DETECTED_AT,
        "signal_candle_open_time": CANDLE_OPEN_MS,
        "reference_price": 50_000.0,
        "entry_zone_low": 49_900.0,
        "entry_zone_high": 50_100.0,
        "invalidation_price": 49_500.0,
        "target_1": 51_000.0,
        "target_2": 52_000.0,
        "score": 0.75,
        "market_regime": "trend",
        "reasons": ("rsi_exhaustion", "volume_climax"),
        "features": features_with(),
        "state": SetupState.DETECTED,
    }
    base.update(overrides)
    return SetupCandidate(**base)


def build(candidate: SetupCandidate):
    return build_source_event(
        candidate,
        activation_freeze_ts=FREEZE_TS,
        captured_at=CAPTURED_AT,
    )


def rejects(candidate: SetupCandidate, code: str) -> None:
    with pytest.raises(SourceValidationError) as excinfo:
        build(candidate)
    assert excinfo.value.reason_code == code
    assert str(excinfo.value) == code


# ---------------------------------------------------------------------------
# A. Identity determinism and stability
# ---------------------------------------------------------------------------


def test_identity_source_event_key_is_deterministic_64_hex():
    cand_a = make_candidate()
    cand_b = make_candidate()
    assert cand_a == cand_b  # equal candidates

    key_a = source_event_key(cand_a)
    key_b = source_event_key(cand_b)

    assert key_a == key_b
    assert len(key_a) == 64
    assert all(ch in "0123456789abcdef" for ch in key_a)
    # Repeated calls on the very same object are stable too.
    assert source_event_key(cand_a) == key_a


def test_identity_source_event_key_stable_across_redetection():
    # Same original signal re-detected later inside the same candle, with a
    # brand-new setup_id: the key must be identical (idempotent delivery).
    original = make_candidate()
    redetected = make_candidate(
        setup_id=OTHER_SETUP_ID,
        detected_at=DETECTED_AT_REDETECT,
    )
    assert original.setup_id != redetected.setup_id
    assert original.detected_at != redetected.detected_at

    assert source_event_key(original) == source_event_key(redetected)


def test_identity_source_event_key_changes_with_signal_candle_open():
    base = make_candidate()
    other_candle = make_candidate(
        signal_candle_open_time=CANDLE_OPEN_MS + 300_000,
    )
    assert source_event_key(base) != source_event_key(other_candle)


def test_identity_source_event_key_changes_with_symbol():
    base = make_candidate()
    other_symbol = make_candidate(symbol="ETHUSDT")
    assert source_event_key(base) != source_event_key(other_symbol)


def test_identity_source_event_key_payload_matches_manual_sha256():
    cand = make_candidate()
    normalized = normalized_identity(cand)

    payload = f"{protocol.IDENTITY_NAMESPACE}|{normalized}"
    manual_key = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    assert source_event_key(cand) == manual_key
    assert identity_key_from_normalized(normalized) == manual_key

    # The capture protocol version is CONTOUR metadata: it must never enter
    # the hashed payload construction.
    assert protocol.PROTOCOL_VERSION not in payload
    assert payload == f"ME_UPSTREAM_SOURCE_V1|{normalized}"


def test_identity_signal_candle_open_ms_prefers_explicit_value():
    # Explicit candle open wins even when detected_at sits in another candle.
    cand = make_candidate(
        signal_candle_open_time=CANDLE_OPEN_MS,
        detected_at=datetime(2026, 11, 23, 15, 22, 0, tzinfo=UTC),
    )
    assert signal_candle_open_ms(cand) == CANDLE_OPEN_MS


def test_identity_signal_candle_open_ms_falls_back_to_5m_grid():
    cand = make_candidate(
        signal_candle_open_time=0,
        detected_at=DETECTED_AT,
    )
    derived = signal_candle_open_ms(cand)

    detected_ms = int(DETECTED_AT.timestamp() * 1000)
    assert derived == detected_ms - detected_ms % 300_000
    assert derived == CANDLE_OPEN_MS
    assert derived % 300_000 == 0


def test_identity_signal_candle_open_ms_naive_detected_at_raises():
    cand = make_candidate(
        signal_candle_open_time=0,
        detected_at=datetime(2026, 11, 23, 15, 12, 34),  # naive
    )
    with pytest.raises(IdentityError) as excinfo:
        signal_candle_open_ms(cand)
    assert str(excinfo.value) == "NAIVE_DETECTED_AT"


def test_identity_signal_candle_open_ms_missing_detected_at_raises():
    cand = make_candidate(signal_candle_open_time=0, detected_at=None)
    with pytest.raises(IdentityError) as excinfo:
        signal_candle_open_ms(cand)
    assert str(excinfo.value) == "MISSING_DETECTED_AT"


def test_identity_normalized_identity_format():
    cand = make_candidate()
    assert normalized_identity(cand) == (
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|BTCUSDT|LONG|5m|1795446600000"
    )


def test_identity_normalized_identity_rejects_wrong_scanner():
    cand = make_candidate(scanner_name="SOME_OTHER_SCANNER")
    with pytest.raises(IdentityError) as excinfo:
        normalized_identity(cand)
    assert str(excinfo.value) == "INVALID_SOURCE_SCANNER"


def test_identity_normalized_identity_rejects_wrong_direction():
    cand = make_candidate(direction="SHORT")
    with pytest.raises(IdentityError) as excinfo:
        normalized_identity(cand)
    assert str(excinfo.value) == "INVALID_SOURCE_DIRECTION"


def test_identity_normalized_identity_rejects_empty_symbol():
    cand = make_candidate(symbol="")
    with pytest.raises(IdentityError) as excinfo:
        normalized_identity(cand)
    assert str(excinfo.value) == "MISSING_SYMBOL"


def test_identity_normalized_identity_values_matches_candidate_derivation():
    # Cross-ledger match key property: raw ledger column values normalize to
    # the exact same string as the candidate-derived identity.
    cand = make_candidate()
    assert (
        normalized_identity_values(
            scanner=cand.scanner_name,
            symbol=cand.symbol,
            direction=cand.direction,
            entry_timeframe=cand.entry_timeframe,
            signal_candle_open_ms=signal_candle_open_ms(cand),
        )
        == normalized_identity(cand)
    )

    # Same for the grid-derived (no explicit candle open) variant.
    derived_cand = make_candidate(signal_candle_open_time=0)
    assert (
        normalized_identity_values(
            scanner=derived_cand.scanner_name,
            symbol=derived_cand.symbol,
            direction=derived_cand.direction,
            entry_timeframe=derived_cand.entry_timeframe,
            signal_candle_open_ms=signal_candle_open_ms(derived_cand),
        )
        == normalized_identity(derived_cand)
    )

    # Empty entry_timeframe normalizes to the "5m" default on BOTH sides.
    bare = make_candidate(entry_timeframe="")
    assert normalized_identity(bare) == normalized_identity_values(
        scanner=bare.scanner_name,
        symbol=bare.symbol,
        direction=bare.direction,
        entry_timeframe="",
        signal_candle_open_ms=signal_candle_open_ms(bare),
    )
    assert normalized_identity(bare).endswith("|5m|1795446600000")


# ---------------------------------------------------------------------------
# B. Contracts / event build
# ---------------------------------------------------------------------------


def test_contracts_build_source_event_success():
    cand = make_candidate()
    event = build(cand)

    assert event.source_event_key == source_event_key(cand)
    assert event.source_identity == normalized_identity(cand)
    assert event.symbol == "BTCUSDT"
    assert event.signal_time == DETECTED_AT
    assert event.captured_at == CAPTURED_AT
    assert event.activation_freeze_ts == FREEZE_TS
    assert event.signal_candle_open_time == CANDLE_OPEN_MS
    assert event.lineage["source_signal_candle_open_time"] == CANDLE_OPEN_MS
    assert event.capture_route == protocol.ROUTE_PRE_SCORING_SOURCE
    assert len(event.source_event_key) == 64
    assert len(event.lineage_hash) == 64
    assert len(event.comparable_lineage_hash) == 64


def test_contracts_features_stored_verbatim_with_extra_and_nested_keys():
    feats = features_with(
        extra_tag="keep-me",
        nested={"a": 1, "b": [1, 2], "c": {"d": "x"}},
    )
    cand = make_candidate(features=feats)
    event = build(cand)

    # Verbatim: exact values, exact extra keys, exact nested structure.
    assert event.features == feats
    assert event.lineage["source_features"] == feats
    assert event.features["rsi_14"] == 55.0
    assert event.features["rsi_delta_3"] == 1.25
    assert event.features["entry_confirmation_rsi_rising"] is True
    assert event.features["extra_tag"] == "keep-me"
    assert event.features["nested"] == {"a": 1, "b": [1, 2], "c": {"d": "x"}}


def test_contracts_build_does_not_mutate_candidate_features():
    feats = features_with(nested={"a": 1, "b": [1, 2]})
    cand = make_candidate(features=feats)
    snapshot = copy.deepcopy(feats)

    build(cand)

    assert feats == snapshot
    assert cand.features == snapshot
    assert cand.features is feats


def test_contracts_rejects_invalid_source_identity():
    rejects(make_candidate(scanner_name="SOME_OTHER_SCANNER"),
            "INVALID_SOURCE_SCANNER")
    rejects(make_candidate(direction="SHORT"), "INVALID_SOURCE_DIRECTION")
    rejects(make_candidate(symbol=""), "MISSING_SYMBOL")


def test_contracts_rejects_invalid_signal_time():
    rejects(make_candidate(detected_at=None), "MISSING_SIGNAL_TIME")
    rejects(
        make_candidate(detected_at=datetime(2026, 11, 23, 15, 12, 34)),
        "NAIVE_SIGNAL_TIME",
    )


@pytest.mark.parametrize("entry_timeframe", ["", "   "])
def test_contracts_rejects_invalid_entry_timeframe(entry_timeframe):
    rejects(make_candidate(entry_timeframe=entry_timeframe),
            "INVALID_ENTRY_TIMEFRAME")


@pytest.mark.parametrize(
    "missing",
    ["rsi_14", "rsi_delta_3", "entry_confirmation_rsi_rising"],
)
def test_contracts_rejects_incomplete_features(missing):
    feats = features_with()
    del feats[missing]
    rejects(make_candidate(features=feats), "INCOMPLETE_FEATURES")


def test_contracts_rejects_empty_features():
    rejects(make_candidate(features={}), "INCOMPLETE_FEATURES")


def test_contracts_feature_rsi14_out_of_range():
    rejects(make_candidate(features=features_with(rsi_14=120.0)),
            "FEATURE_RSI14_OUT_OF_RANGE")
    rejects(make_candidate(features=features_with(rsi_14=-0.5)),
            "FEATURE_RSI14_OUT_OF_RANGE")


def test_contracts_feature_rsi14_invalid_type_string():
    # No implicit coercion of numeric strings.
    rejects(make_candidate(features=features_with(rsi_14="55")),
            "FEATURE_RSI14_INVALID_TYPE")


def test_contracts_feature_rsi14_nan_rejected_before_cohort():
    # NaN / Inf are real floats that are not finite — they must yield the
    # precise NOT_FINITE reason code (validation runs on the RAW feature
    # values, before any JSON normalization) and never enter the cohort.
    rejects(make_candidate(features=features_with(rsi_14=float("nan"))),
            "FEATURE_RSI14_NOT_FINITE")
    rejects(make_candidate(features=features_with(rsi_14=float("inf"))),
            "FEATURE_RSI14_NOT_FINITE")


def test_contracts_feature_rsi_delta_3_nan_not_finite():
    rejects(make_candidate(features=features_with(rsi_delta_3=float("nan"))),
            "FEATURE_RSI_DELTA_3_NOT_FINITE")


def test_contracts_feature_rsi14_none_not_finite():
    rejects(make_candidate(features=features_with(rsi_14=None)),
            "FEATURE_RSI14_NOT_FINITE")


def test_contracts_feature_rsi_delta_3_none_not_finite():
    rejects(make_candidate(features=features_with(rsi_delta_3=None)),
            "FEATURE_RSI_DELTA_3_NOT_FINITE")


def test_contracts_feature_rsi_delta_3_bool_invalid_type():
    # Bool is not a valid numeric feature value (no truthiness coercion).
    rejects(make_candidate(features=features_with(rsi_delta_3=True)),
            "FEATURE_RSI_DELTA_3_INVALID_TYPE")


def test_contracts_feature_confirmation_flag_string_invalid_type():
    rejects(
        make_candidate(
            features=features_with(entry_confirmation_rsi_rising="true"),
        ),
        "FEATURE_CONFIRMATION_FLAG_INVALID_TYPE",
    )


def test_contracts_feature_confirmation_flag_int_invalid_type():
    rejects(
        make_candidate(
            features=features_with(entry_confirmation_rsi_rising=1),
        ),
        "FEATURE_CONFIRMATION_FLAG_INVALID_TYPE",
    )


def test_contracts_lineage_hash_deterministic():
    event = build(make_candidate())
    assert event.lineage_hash == lineage_hash(event.lineage)
    assert event.lineage_hash == lineage_hash(dict(event.lineage))
    assert len(event.lineage_hash) == 64


@pytest.mark.parametrize(
    "changed_key",
    [
        "source_reference_price",
        "source_signal_time",
        "source_entry_zone",
        "source_features",
        "capture_protocol_version",
    ],
)
def test_contracts_lineage_hash_changes_with_any_lineage_value(changed_key):
    event = build(make_candidate())
    baseline = lineage_hash(event.lineage)

    modified = dict(event.lineage)
    modified[changed_key] = "MUTATED"

    assert lineage_hash(modified) != baseline


def test_contracts_lineage_hash_rejects_non_json_lineage():
    with pytest.raises(SourceValidationError) as excinfo:
        lineage_hash({"bad": float("nan")})
    assert excinfo.value.reason_code == "INVALID_LINEAGE_JSON"


def test_contracts_comparable_lineage_view_excludes_capture_and_post_scoring():
    event = build(make_candidate())
    lineage = event.lineage

    # Those keys DO exist on the full lineage (so the exclusion is real).
    excluded = {
        "capture_route",
        "capture_independent_of",
        "capture_protocol_version",
        "capture_protocol_fingerprint",
        "source_score",
        "source_state",
        "source_market_regime",
        "source_reasons",
    }
    assert excluded <= set(lineage)

    view = comparable_lineage_view(lineage)
    assert excluded.isdisjoint(view)
    assert excluded.isdisjoint(view.get("source_features", {}))

    # The comparable ORIGINAL facts are present.
    assert "source_identity" in view
    assert "source_reference_price" in view
    assert view["source_identity"] == event.source_identity
    assert "rsi_14" in view["source_features"]


def test_contracts_comparable_lineage_hash_ignores_post_scoring_fields():
    base = make_candidate()
    rewritten = make_candidate(
        score=0.05,
        market_regime="chop",
        reasons=("completely", "different", "reasons"),
        state=SetupState.CONFIRMED,
    )

    event_a = build(base)
    event_b = build(rewritten)

    # Full lineage DOES differ (post-scoring facts are recorded) ...
    assert event_a.lineage_hash != event_b.lineage_hash
    # ... but the comparable subset is IDENTICAL (cross-ledger property).
    assert (
        comparable_lineage_hash(event_a.lineage)
        == comparable_lineage_hash(event_b.lineage)
    )
    assert event_a.comparable_lineage_hash == event_b.comparable_lineage_hash


def test_contracts_comparable_lineage_hash_differs_when_reference_price_differs():
    event_a = build(make_candidate(reference_price=50_000.0))
    event_b = build(make_candidate(reference_price=50_001.0))

    assert (
        comparable_lineage_hash(event_a.lineage)
        != comparable_lineage_hash(event_b.lineage)
    )


def test_contracts_normalize_reason_code_source_validation_error():
    exc = SourceValidationError("INVALID_SOURCE_SCANNER")
    assert normalize_reason_code(exc) == "INVALID_SOURCE_SCANNER"
    assert normalize_reason_code(SourceValidationError("X")) == "X"


def test_contracts_normalize_reason_code_key_error():
    assert normalize_reason_code(KeyError("x")) == "MISSING_FIELD:x"


def test_contracts_normalize_reason_code_type_error():
    assert normalize_reason_code(TypeError("bad")) == "INVALID_FIELD_TYPE"
    # Plain ValueError shares the deterministic INVALID_FIELD_TYPE code.
    assert normalize_reason_code(ValueError("bad")) == "INVALID_FIELD_TYPE"


def test_contracts_normalize_reason_code_unexpected_error():
    assert (
        normalize_reason_code(RuntimeError("boom"))
        == "UNEXPECTED_VALIDATION_ERROR"
    )
