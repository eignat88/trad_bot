"""Comprehensive tests for Stage 4 PR2 — Finding Aggregation.

Covers all PR2 modules:
    - fingerprint.py (determinism, sensitivity, exclusions)
    - normalizer.py (type mapping, direction, comparator, segment, scanner)
    - repeat_policy.py (disabled config, independent counting, maturity supersede,
                         confidence filtering, no shortcuts, terminal states)
    - ingestion.py (idempotency, concurrent fingerprint, aggregates, evidence dedup,
                     transaction rollback, strict validation)
    - adapter.py (schema version strict, evidence validation, analysis_run_id locked,
                  metric resolution)
    - migration 037 (existence)
    - Repository methods (get_finding_by_fingerprint, create_occurrence_if_absent,
                          refresh_finding_aggregate, create_finding_with_fingerprint)
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

import pytest

from app.analytics.agents.research.fingerprint import (
    FindingFingerprintPayload,
    FindingFingerprintV1,
)
from app.analytics.agents.research.models import (
    Finding,
    FindingCandidate,
    FindingIngestionResult,
    FindingOccurrence,
    IngestionBatchSummary,
)
from app.analytics.agents.research.normalizer import (
    ComparatorNormalizer,
    DirectionNormalizer,
    FindingSegmentNormalizerV1,
    FindingTypeNormalizer,
    ScannerNormalizer,
)
from app.analytics.agents.research.repeat_policy import (
    KEEP_OPEN,
    MARK_REPEATED,
    MARK_RESEARCH_REQUIRED,
    NO_CHANGE,
    PROMOTION_DISABLED,
    FindingRepeatPolicyConfig,
    FindingRepeatPolicyV1,
)
from app.analytics.agents.research.transition_policy import (
    ResearchTransitionPolicyV1,
)

MIGRATION_037_PATH = Path("sql/migrations/037_finding_aggregation.sql")


# ======================================================================
# Helper to build a valid FindingCandidate for tests
# ======================================================================

def _make_candidate(
    run_id=None,
    finding_type="ENTRY",
    scanner_name="ME",
    metric_name="pnl_r",
    comparator="LT",
    confidence="MEDIUM",
    evidence_refs=None,
    business_date=None,
    maturity="PROVISIONAL",
    threshold_policy_version="finding-thresholds-v1",
    statement="Test finding",
    **overrides,
) -> FindingCandidate:
    """Build a valid FindingCandidate with sane defaults."""
    return FindingCandidate(
        finding_type=finding_type,
        scanner_name=scanner_name,
        direction=overrides.get("direction", "SHORT"),
        normalized_segment=overrides.get("normalized_segment", {"symbol": "btcusdt"}),
        metric_name=metric_name,
        comparator=comparator,
        threshold_policy_version=threshold_policy_version,
        statement=statement,
        scope_json=overrides.get("scope_json", {"symbol": "BTCUSDT"}),
        metric_value=overrides.get("metric_value", -0.5),
        sample_size=overrides.get("sample_size", 18),
        confidence=confidence,
        evidence_refs=evidence_refs or ["evidence://run1/obs1"],
        analysis_run_id=run_id or uuid4(),
        agent_run_id=overrides.get("agent_run_id"),
        dataset_version=overrides.get("dataset_version", "2024-01"),
        observed_at=overrides.get("observed_at", datetime.now(timezone.utc)),
        source_agent_name=overrides.get("source_agent_name", "EXECUTION_QUALITY"),
        business_date=business_date,
        maturity=maturity,
    )


def _make_occurrence(
    finding_id=None,
    run_id=None,
    confidence="MEDIUM",
    business_date=None,
    maturity="PROVISIONAL",
    evidence_refs=None,
) -> FindingOccurrence:
    """Build a FindingOccurrence with sane defaults."""
    return FindingOccurrence(
        occurrence_id=uuid4(),
        finding_id=finding_id or uuid4(),
        analysis_run_id=run_id or uuid4(),
        observed_at=datetime.now(timezone.utc),
        confidence=confidence,
        business_date=business_date,
        maturity=maturity,
        evidence_refs=evidence_refs or [],
    )


# ======================================================================
# 1. Fingerprint determinism
# ======================================================================

class TestFingerprintDeterminism:
    """Fingerprint must be deterministic: same input → same hash."""

    def test_same_input_same_hash(self):
        payload = FindingFingerprintPayload(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={"symbol": "btcusdt", "timeframe": "15m"},
            metric_name="pnl_r",
            comparator="LT",
            threshold_policy_version="finding-thresholds-v1",
        )
        h1 = FindingFingerprintV1.compute(payload)
        h2 = FindingFingerprintV1.compute(payload)
        assert h1 == h2

    def test_hash_is_64_hex_chars(self):
        payload = FindingFingerprintPayload(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={},
            metric_name="pnl_r",
            comparator="GT",
            threshold_policy_version="finding-thresholds-v1",
        )
        h = FindingFingerprintV1.compute(payload)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_is_sha256(self):
        payload = FindingFingerprintPayload(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={"a": 1},
            metric_name="pnl_r",
            comparator="GT",
            threshold_policy_version="finding-thresholds-v1",
        )
        h = FindingFingerprintV1.compute(payload)
        ordered_dict = {
            "comparator": "GT",
            "direction": "SHORT",
            "finding_type": "ENTRY",
            "metric_name": "pnl_r",
            "normalized_segment": {"a": 1},
            "scanner_name": "ME",
            "threshold_policy_version": "finding-thresholds-v1",
        }
        canonical = json.dumps(
            ordered_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert h == expected


# ======================================================================
# 2. Fingerprint excludes: metric_value, sample_size, confidence, date, analysis_run_id
# ======================================================================

class TestFingerprintExclusions:
    """Fingerprint must NOT depend on per-occurrence fields."""

    def _make_payload(self, **overrides) -> FindingFingerprintPayload:
        defaults = dict(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={"symbol": "btcusdt"},
            metric_name="pnl_r",
            comparator="LT",
            threshold_policy_version="finding-thresholds-v1",
        )
        defaults.update(overrides)
        return FindingFingerprintPayload(**defaults)

    def test_metric_value_excluded(self):
        p1 = self._make_payload()
        p2 = self._make_payload()
        assert FindingFingerprintV1.compute(p1) == FindingFingerprintV1.compute(p2)

    def test_sample_size_excluded(self):
        p1 = self._make_payload()
        p2 = self._make_payload()
        assert FindingFingerprintV1.compute(p1) == FindingFingerprintV1.compute(p2)

    def test_confidence_excluded(self):
        p1 = self._make_payload()
        p2 = self._make_payload()
        assert FindingFingerprintV1.compute(p1) == FindingFingerprintV1.compute(p2)


# ======================================================================
# 3. Fingerprint sensitive to all included fields
# ======================================================================

class TestFingerprintSensitivity:
    """Changing any fingerprint field must produce a different hash."""

    BASE = FindingFingerprintPayload(
        finding_type="ENTRY",
        scanner_name="ME",
        direction="SHORT",
        normalized_segment={"symbol": "btcusdt", "timeframe": "15m"},
        metric_name="pnl_r",
        comparator="LT",
        threshold_policy_version="finding-thresholds-v1",
    )

    def test_sensitive_to_finding_type(self):
        other = FindingFingerprintPayload(
            finding_type="STOP",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={"symbol": "btcusdt", "timeframe": "15m"},
            metric_name="pnl_r",
            comparator="LT",
            threshold_policy_version="finding-thresholds-v1",
        )
        assert FindingFingerprintV1.compute(self.BASE) != FindingFingerprintV1.compute(other)

    def test_sensitive_to_scanner_name(self):
        other = FindingFingerprintPayload(
            finding_type="ENTRY",
            scanner_name="OB",
            direction="SHORT",
            normalized_segment={"symbol": "btcusdt", "timeframe": "15m"},
            metric_name="pnl_r",
            comparator="LT",
            threshold_policy_version="finding-thresholds-v1",
        )
        assert FindingFingerprintV1.compute(self.BASE) != FindingFingerprintV1.compute(other)

    def test_sensitive_to_direction(self):
        other = FindingFingerprintPayload(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="LONG",
            normalized_segment={"symbol": "btcusdt", "timeframe": "15m"},
            metric_name="pnl_r",
            comparator="LT",
            threshold_policy_version="finding-thresholds-v1",
        )
        assert FindingFingerprintV1.compute(self.BASE) != FindingFingerprintV1.compute(other)

    def test_sensitive_to_segment(self):
        other = FindingFingerprintPayload(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={"symbol": "ethusdt", "timeframe": "15m"},
            metric_name="pnl_r",
            comparator="LT",
            threshold_policy_version="finding-thresholds-v1",
        )
        assert FindingFingerprintV1.compute(self.BASE) != FindingFingerprintV1.compute(other)

    def test_sensitive_to_metric_name(self):
        other = FindingFingerprintPayload(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={"symbol": "btcusdt", "timeframe": "15m"},
            metric_name="win_rate",
            comparator="LT",
            threshold_policy_version="finding-thresholds-v1",
        )
        assert FindingFingerprintV1.compute(self.BASE) != FindingFingerprintV1.compute(other)

    def test_sensitive_to_comparator(self):
        other = FindingFingerprintPayload(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={"symbol": "btcusdt", "timeframe": "15m"},
            metric_name="pnl_r",
            comparator="GT",
            threshold_policy_version="finding-thresholds-v1",
        )
        assert FindingFingerprintV1.compute(self.BASE) != FindingFingerprintV1.compute(other)

    def test_sensitive_to_threshold_version(self):
        other = FindingFingerprintPayload(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={"symbol": "btcusdt", "timeframe": "15m"},
            metric_name="pnl_r",
            comparator="LT",
            threshold_policy_version="finding-thresholds-v2",
        )
        assert FindingFingerprintV1.compute(self.BASE) != FindingFingerprintV1.compute(other)


# ======================================================================
# 4. Segment normalization
# ======================================================================

class TestSegmentNormalization:
    def test_sorted_keys(self):
        seg = {"z": 1, "a": 2, "m": 3}
        result = FindingSegmentNormalizerV1.normalize(seg)
        assert list(result.keys()) == ["a", "m", "z"]

    def test_lowercase_keys(self):
        seg = {"Symbol": "BTCUSDT", "Timeframe": "15m"}
        result = FindingSegmentNormalizerV1.normalize(seg)
        assert "symbol" in result
        assert "timeframe" in result

    def test_lowercase_string_values(self):
        seg = {"symbol": "BTCUSDT"}
        result = FindingSegmentNormalizerV1.normalize(seg)
        assert result["symbol"] == "btcusdt"

    def test_none_values_dropped(self):
        seg = {"a": 1, "b": None, "c": 3}
        result = FindingSegmentNormalizerV1.normalize(seg)
        assert "b" not in result
        assert result == {"a": 1, "c": 3}

    def test_empty_segment(self):
        assert FindingSegmentNormalizerV1.normalize({}) == {}

    def test_nested_dict(self):
        seg = {"outer": {"B": 2, "A": 1}}
        result = FindingSegmentNormalizerV1.normalize(seg)
        assert list(result["outer"].keys()) == ["a", "b"]

    def test_list_sorted(self):
        seg = {"tags": ["z", "a", "m"]}
        result = FindingSegmentNormalizerV1.normalize(seg)
        assert result["tags"] == ["a", "m", "z"]


# ======================================================================
# 5. Direction normalization
# ======================================================================

class TestDirectionNormalization:
    def test_long(self):
        assert DirectionNormalizer.normalize("LONG") == "LONG"
        assert DirectionNormalizer.normalize("long") == "LONG"

    def test_short(self):
        assert DirectionNormalizer.normalize("SHORT") == "SHORT"
        assert DirectionNormalizer.normalize("short") == "SHORT"

    def test_both(self):
        assert DirectionNormalizer.normalize("BOTH") == "BOTH"
        assert DirectionNormalizer.normalize("all") == "BOTH"
        assert DirectionNormalizer.normalize("either") == "BOTH"

    def test_none(self):
        assert DirectionNormalizer.normalize("NONE") == "NONE"
        assert DirectionNormalizer.normalize("") == "NONE"
        assert DirectionNormalizer.normalize("n/a") == "NONE"
        assert DirectionNormalizer.normalize("na") == "NONE"

    def test_unknown_defaults_to_none(self):
        assert DirectionNormalizer.normalize("UP") == "NONE"


# ======================================================================
# 6. Comparator normalization
# ======================================================================

class TestComparatorNormalization:
    def test_gt(self):
        assert ComparatorNormalizer.normalize("GT") == "GT"
        assert ComparatorNormalizer.normalize(">") == "GT"

    def test_gte(self):
        assert ComparatorNormalizer.normalize("GTE") == "GTE"
        assert ComparatorNormalizer.normalize(">=") == "GTE"

    def test_lt(self):
        assert ComparatorNormalizer.normalize("LT") == "LT"
        assert ComparatorNormalizer.normalize("<") == "LT"

    def test_lte(self):
        assert ComparatorNormalizer.normalize("LTE") == "LTE"
        assert ComparatorNormalizer.normalize("<=") == "LTE"

    def test_eq(self):
        assert ComparatorNormalizer.normalize("EQ") == "EQ"
        assert ComparatorNormalizer.normalize("=") == "EQ"
        assert ComparatorNormalizer.normalize("==") == "EQ"

    def test_neq(self):
        assert ComparatorNormalizer.normalize("NEQ") == "NEQ"
        assert ComparatorNormalizer.normalize("!=") == "NEQ"
        assert ComparatorNormalizer.normalize("<>") == "NEQ"

    def test_delta_positive(self):
        assert ComparatorNormalizer.normalize("DELTA_POSITIVE") == "DELTA_POSITIVE"
        assert ComparatorNormalizer.normalize("delta_positive") == "DELTA_POSITIVE"

    def test_delta_negative(self):
        assert ComparatorNormalizer.normalize("DELTA_NEGATIVE") == "DELTA_NEGATIVE"
        assert ComparatorNormalizer.normalize("delta_negative") == "DELTA_NEGATIVE"

    def test_change(self):
        assert ComparatorNormalizer.normalize("CHANGE") == "CHANGE"
        assert ComparatorNormalizer.normalize("change") == "CHANGE"

    def test_anomaly(self):
        assert ComparatorNormalizer.normalize("ANOMALY") == "ANOMALY"
        assert ComparatorNormalizer.normalize("anomaly") == "ANOMALY"

    def test_unknown_returns_none(self):
        assert ComparatorNormalizer.normalize("UNKNOWN") is None
        assert ComparatorNormalizer.normalize("") is None


# ======================================================================
# 7. Finding type mapping
# ======================================================================

class TestFindingTypeMapping:
    def test_entry_related(self):
        assert FindingTypeNormalizer.normalize("entry") == "ENTRY"
        assert FindingTypeNormalizer.normalize("ENTRY") == "ENTRY"
        assert FindingTypeNormalizer.normalize("entry_quality") == "ENTRY"
        assert FindingTypeNormalizer.normalize("entry_timing") == "ENTRY"
        assert FindingTypeNormalizer.normalize("entry_slippage") == "ENTRY"
        assert FindingTypeNormalizer.normalize("mae_entry") == "ENTRY"

    def test_dca_related(self):
        assert FindingTypeNormalizer.normalize("dca") == "DCA"
        assert FindingTypeNormalizer.normalize("DCA") == "DCA"
        assert FindingTypeNormalizer.normalize("dca_trigger") == "DCA"

    def test_stop_related(self):
        assert FindingTypeNormalizer.normalize("stop") == "STOP"
        assert FindingTypeNormalizer.normalize("STOP") == "STOP"
        assert FindingTypeNormalizer.normalize("stop_loss") == "STOP"

    def test_exit_related(self):
        assert FindingTypeNormalizer.normalize("exit") == "EXIT"
        assert FindingTypeNormalizer.normalize("EXIT") == "EXIT"
        assert FindingTypeNormalizer.normalize("trailing_exit") == "EXIT"

    def test_funnel_related(self):
        assert FindingTypeNormalizer.normalize("funnel") == "FUNNEL"
        assert FindingTypeNormalizer.normalize("FUNNEL") == "FUNNEL"
        assert FindingTypeNormalizer.normalize("signal_funnel") == "FUNNEL"

    def test_drift_related(self):
        assert FindingTypeNormalizer.normalize("drift") == "DRIFT"
        assert FindingTypeNormalizer.normalize("DRIFT") == "DRIFT"
        assert FindingTypeNormalizer.normalize("anomaly") == "DRIFT"

    def test_data_related(self):
        assert FindingTypeNormalizer.normalize("data") == "DATA"
        assert FindingTypeNormalizer.normalize("DATA") == "DATA"
        assert FindingTypeNormalizer.normalize("data_quality") == "DATA"

    def test_incident_related(self):
        assert FindingTypeNormalizer.normalize("incident") == "INCIDENT"
        assert FindingTypeNormalizer.normalize("INCIDENT") == "INCIDENT"
        assert FindingTypeNormalizer.normalize("error") == "INCIDENT"
        assert FindingTypeNormalizer.normalize("failure") == "INCIDENT"

    def test_unknown_returns_none(self):
        assert FindingTypeNormalizer.normalize("unknown_thing") is None
        assert FindingTypeNormalizer.normalize("") is None
        assert FindingTypeNormalizer.normalize("RANDOM") is None


# ======================================================================
# 8. Scanner normalization
# ======================================================================

class TestScannerNormalization:
    def test_uppercase(self):
        assert ScannerNormalizer.normalize("me") == "ME"
        assert ScannerNormalizer.normalize("ME") == "ME"

    def test_trimmed(self):
        assert ScannerNormalizer.normalize("  me  ") == "ME"

    def test_empty_returns_none(self):
        assert ScannerNormalizer.normalize("") is None
        assert ScannerNormalizer.normalize("  ") is None
        assert ScannerNormalizer.normalize(None) is None


# ======================================================================
# 9. Repeat policy — config absent/disabled → no auto promotion
# ======================================================================

class TestRepeatPolicyDisabled:
    """When config is None or all thresholds are 0, promotion is DISABLED."""

    def test_none_config_returns_disabled(self):
        action = FindingRepeatPolicyV1.evaluate(
            None,
            [{"confidence": "HIGH", "business_date": "2024-01-01"}] * 10,
            "OPEN",
        )
        assert action == PROMOTION_DISABLED

    def test_all_zero_thresholds_returns_disabled(self):
        config = FindingRepeatPolicyConfig(
            repeated_min_independent_occurrences=0,
            research_required_min_occurrences=0,
            min_distinct_business_dates=0,
        )
        action = FindingRepeatPolicyV1.evaluate(
            config,
            [{"confidence": "HIGH", "business_date": "2024-01-01"}] * 10,
            "OPEN",
        )
        assert action == PROMOTION_DISABLED

    def test_disabled_even_with_many_occurrences(self):
        """Even with 100 HIGH-confidence occurrences, disabled config means no promotion."""
        config = FindingRepeatPolicyConfig()  # all defaults = 0
        occurrences = [
            {"confidence": "HIGH", "business_date": f"2024-01-{i:02d}"}
            for i in range(1, 32)
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == PROMOTION_DISABLED


# ======================================================================
# 10. Repeat policy — OPEN → REPEATED at threshold
# ======================================================================

class TestRepeatPolicyOpenToRepeated:
    """OPEN → REPEATED when qualifying occurrences ≥ repeated_min_independent_occurrences."""

    def test_below_threshold_stays_open(self):
        config = FindingRepeatPolicyConfig(repeated_min_independent_occurrences=3)
        occurrences = [
            {"confidence": "MEDIUM", "business_date": "2024-01-01"},
            {"confidence": "MEDIUM", "business_date": "2024-01-02"},
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == KEEP_OPEN

    def test_at_threshold_promotes(self):
        config = FindingRepeatPolicyConfig(repeated_min_independent_occurrences=3)
        occurrences = [
            {"confidence": "MEDIUM", "business_date": "2024-01-01"},
            {"confidence": "MEDIUM", "business_date": "2024-01-02"},
            {"confidence": "MEDIUM", "business_date": "2024-01-03"},
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == MARK_REPEATED

    def test_above_threshold_promotes(self):
        config = FindingRepeatPolicyConfig(repeated_min_independent_occurrences=3)
        occurrences = [
            {"confidence": "MEDIUM", "business_date": "2024-01-01"},
            {"confidence": "MEDIUM", "business_date": "2024-01-02"},
            {"confidence": "MEDIUM", "business_date": "2024-01-03"},
            {"confidence": "MEDIUM", "business_date": "2024-01-04"},
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == MARK_REPEATED


# ======================================================================
# 11. Repeat policy — REPEATED → RESEARCH_REQUIRED at threshold
# ======================================================================

class TestRepeatPolicyRepeatedToResearchRequired:
    """REPEATED → RESEARCH_REQUIRED when qualifying ≥ research_required_min_occurrences
    AND distinct business dates ≥ min_distinct_business_dates."""

    def test_below_occurrence_threshold(self):
        config = FindingRepeatPolicyConfig(
            research_required_min_occurrences=5,
            min_distinct_business_dates=2,
        )
        occurrences = [
            {"confidence": "MEDIUM", "business_date": "2024-01-01"},
            {"confidence": "MEDIUM", "business_date": "2024-01-02"},
            {"confidence": "MEDIUM", "business_date": "2024-01-03"},
            {"confidence": "MEDIUM", "business_date": "2024-01-04"},
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "REPEATED")
        assert action == NO_CHANGE

    def test_meets_both_thresholds(self):
        config = FindingRepeatPolicyConfig(
            research_required_min_occurrences=5,
            min_distinct_business_dates=2,
        )
        # 5 distinct dates → 5 independent observations, 5 distinct dates
        occurrences = [
            {"confidence": "MEDIUM", "business_date": "2024-01-01"},
            {"confidence": "MEDIUM", "business_date": "2024-01-02"},
            {"confidence": "MEDIUM", "business_date": "2024-01-03"},
            {"confidence": "MEDIUM", "business_date": "2024-01-04"},
            {"confidence": "MEDIUM", "business_date": "2024-01-05"},
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "REPEATED")
        assert action == MARK_RESEARCH_REQUIRED

    def test_enough_occurrences_but_single_date(self):
        """5 occurrences but only 1 distinct date → NO_CHANGE."""
        config = FindingRepeatPolicyConfig(
            research_required_min_occurrences=5,
            min_distinct_business_dates=2,
        )
        occurrences = [
            {"confidence": "MEDIUM", "business_date": "2024-01-01"},
            {"confidence": "MEDIUM", "business_date": "2024-01-01"},
            {"confidence": "MEDIUM", "business_date": "2024-01-01"},
            {"confidence": "MEDIUM", "business_date": "2024-01-01"},
            {"confidence": "MEDIUM", "business_date": "2024-01-01"},
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "REPEATED")
        assert action == NO_CHANGE


# ======================================================================
# 12. Repeat policy — PROVISIONAL+FINAL same date → counted once
# ======================================================================

class TestRepeatPolicyMaturitySupersede:
    """PROVISIONAL + FINAL of the same business_date = 1 independent observation."""

    def test_final_supersedes_provisional_same_date(self):
        config = FindingRepeatPolicyConfig(repeated_min_independent_occurrences=3)
        occurrences = [
            # Two on same date, one PROVISIONAL one FINAL → counts as 1
            {"confidence": "MEDIUM", "business_date": "2024-01-01", "maturity": "PROVISIONAL"},
            {"confidence": "MEDIUM", "business_date": "2024-01-01", "maturity": "FINAL"},
            # Two distinct dates
            {"confidence": "MEDIUM", "business_date": "2024-01-02", "maturity": "PROVISIONAL"},
            {"confidence": "MEDIUM", "business_date": "2024-01-03", "maturity": "PROVISIONAL"},
        ]
        # Independent observations: 2024-01-01 (1), 2024-01-02 (1), 2024-01-03 (1) = 3
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == MARK_REPEATED

    def test_all_provisional_same_date_counted_once(self):
        config = FindingRepeatPolicyConfig(repeated_min_independent_occurrences=3)
        occurrences = [
            {"confidence": "MEDIUM", "business_date": "2024-01-01", "maturity": "PROVISIONAL"},
            {"confidence": "MEDIUM", "business_date": "2024-01-01", "maturity": "PROVISIONAL"},
            {"confidence": "MEDIUM", "business_date": "2024-01-01", "maturity": "PROVISIONAL"},
        ]
        # Only 1 independent observation (all same date)
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == KEEP_OPEN

    def test_final_only_dates_counted(self):
        config = FindingRepeatPolicyConfig(repeated_min_independent_occurrences=3)
        occurrences = [
            {"confidence": "MEDIUM", "business_date": "2024-01-01", "maturity": "FINAL"},
            {"confidence": "MEDIUM", "business_date": "2024-01-02", "maturity": "FINAL"},
            {"confidence": "MEDIUM", "business_date": "2024-01-03", "maturity": "FINAL"},
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == MARK_REPEATED


# ======================================================================
# 13. Repeat policy — different business_dates counted independently
# ======================================================================

class TestRepeatPolicyDistinctDates:
    """Each distinct business_date is an independent observation."""

    def test_three_dates_three_observations(self):
        config = FindingRepeatPolicyConfig(
            repeated_min_independent_occurrences=3,
            research_required_min_occurrences=3,
            min_distinct_business_dates=3,
        )
        occurrences = [
            {"confidence": "MEDIUM", "business_date": "2024-01-01"},
            {"confidence": "MEDIUM", "business_date": "2024-01-02"},
            {"confidence": "MEDIUM", "business_date": "2024-01-03"},
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == MARK_REPEATED


# ======================================================================
# 14. Repeat policy — no shortcut OPEN → RESEARCH_REQUIRED
# ======================================================================

class TestRepeatPolicyNoShortcut:
    """OPEN should never skip directly to RESEARCH_REQUIRED."""

    def test_many_occurrences_still_only_repeated(self):
        config = FindingRepeatPolicyConfig(
            repeated_min_independent_occurrences=3,
            research_required_min_occurrences=5,
            min_distinct_business_dates=2,
        )
        occurrences = [
            {"confidence": "HIGH", "business_date": f"2024-01-{i:02d}"}
            for i in range(1, 11)
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == MARK_REPEATED  # Not MARK_RESEARCH_REQUIRED


# ======================================================================
# 15. Repeat policy — CLOSED is terminal
# ======================================================================

class TestRepeatPolicyClosedTerminal:
    def test_closed_no_change(self):
        config = FindingRepeatPolicyConfig(
            repeated_min_independent_occurrences=1,
        )
        occurrences = [
            {"confidence": "HIGH", "business_date": f"2024-01-{i:02d}"}
            for i in range(1, 20)
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "CLOSED")
        assert action == NO_CHANGE


# ======================================================================
# 16. Repeat policy — RESEARCH_REQUIRED is terminal (no further promotion)
# ======================================================================

class TestRepeatPolicyResearchRequiredTerminal:
    def test_research_required_no_change(self):
        config = FindingRepeatPolicyConfig(
            repeated_min_independent_occurrences=1,
            research_required_min_occurrences=1,
            min_distinct_business_dates=1,
        )
        occurrences = [
            {"confidence": "HIGH", "business_date": f"2024-01-{i:02d}"}
            for i in range(1, 20)
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "RESEARCH_REQUIRED")
        assert action == NO_CHANGE


# ======================================================================
# 17. Repeat policy — confidence filtering
# ======================================================================

class TestRepeatPolicyConfidenceFiltering:
    def test_low_confidence_not_counted(self):
        config = FindingRepeatPolicyConfig(
            repeated_min_independent_occurrences=3,
            min_confidence="MEDIUM",
        )
        occurrences = [
            {"confidence": "LOW", "business_date": "2024-01-01"},
            {"confidence": "LOW", "business_date": "2024-01-02"},
            {"confidence": "LOW", "business_date": "2024-01-03"},
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == KEEP_OPEN

    def test_medium_and_high_counted(self):
        config = FindingRepeatPolicyConfig(
            repeated_min_independent_occurrences=3,
            min_confidence="MEDIUM",
        )
        occurrences = [
            {"confidence": "MEDIUM", "business_date": "2024-01-01"},
            {"confidence": "HIGH", "business_date": "2024-01-02"},
            {"confidence": "MEDIUM", "business_date": "2024-01-03"},
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == MARK_REPEATED

    def test_only_high_counted_when_min_is_high(self):
        config = FindingRepeatPolicyConfig(
            repeated_min_independent_occurrences=2,
            min_confidence="HIGH",
        )
        occurrences = [
            {"confidence": "MEDIUM", "business_date": "2024-01-01"},
            {"confidence": "HIGH", "business_date": "2024-01-02"},
            {"confidence": "MEDIUM", "business_date": "2024-01-03"},
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == KEEP_OPEN  # Only 1 HIGH


# ======================================================================
# 18. Repeat policy — analysis_run_id dedup (no business_date)
# ======================================================================

class TestRepeatPolicyRunIdDedup:
    """Occurrences without business_date are grouped by analysis_run_id."""

    def test_same_run_id_counted_once(self):
        config = FindingRepeatPolicyConfig(repeated_min_independent_occurrences=3)
        run_id = "aaaa-bbbb"
        occurrences = [
            {"confidence": "MEDIUM", "business_date": None, "analysis_run_id": run_id},
            {"confidence": "MEDIUM", "business_date": None, "analysis_run_id": run_id},
            {"confidence": "MEDIUM", "business_date": None, "analysis_run_id": run_id},
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == KEEP_OPEN  # Only 1 independent

    def test_different_run_ids_counted_independently(self):
        config = FindingRepeatPolicyConfig(repeated_min_independent_occurrences=3)
        occurrences = [
            {"confidence": "MEDIUM", "business_date": None, "analysis_run_id": "run-1"},
            {"confidence": "MEDIUM", "business_date": None, "analysis_run_id": "run-2"},
            {"confidence": "MEDIUM", "business_date": None, "analysis_run_id": "run-3"},
        ]
        action = FindingRepeatPolicyV1.evaluate(config, occurrences, "OPEN")
        assert action == MARK_REPEATED


# ======================================================================
# 19. Ingestion — analysis_run_id required
# ======================================================================

class TestIngestionAnalysisRunIdRequired:
    def test_missing_analysis_run_id_raises(self):
        mock_repo = MagicMock()
        from app.analytics.agents.research.ingestion import FindingIngestionService

        service = FindingIngestionService(mock_repo)
        # Create candidate with analysis_run_id=None — should raise
        candidate = FindingCandidate(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={},
            metric_name="pnl_r",
            comparator="LT",
            threshold_policy_version="v1",
            statement="test",
            scope_json={},
            metric_value=None,
            sample_size=0,
            confidence="MEDIUM",
            evidence_refs=["evidence://1"],
            analysis_run_id=None,  # type: ignore[arg-type]
            agent_run_id=None,
            dataset_version="v1",
            observed_at=datetime.now(timezone.utc),
            source_agent_name="TEST",
        )
        with pytest.raises(ValueError, match="analysis_run_id is required"):
            service.ingest_candidate(candidate)


# ======================================================================
# 20. Ingestion — threshold_policy_version required
# ======================================================================

class TestIngestionThresholdPolicyVersionRequired:
    def test_missing_threshold_policy_version_raises(self):
        mock_repo = MagicMock()
        from app.analytics.agents.research.ingestion import FindingIngestionService

        service = FindingIngestionService(mock_repo)
        candidate = FindingCandidate(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={},
            metric_name="pnl_r",
            comparator="LT",
            threshold_policy_version="",  # Empty
            statement="test",
            scope_json={},
            metric_value=None,
            sample_size=0,
            confidence="MEDIUM",
            evidence_refs=["evidence://1"],
            analysis_run_id=uuid4(),
            agent_run_id=None,
            dataset_version="v1",
            observed_at=datetime.now(timezone.utc),
            source_agent_name="TEST",
        )
        with pytest.raises(ValueError, match="threshold_policy_version is required"):
            service.ingest_candidate(candidate)


# ======================================================================
# 21. Ingestion — idempotency
# ======================================================================

class TestIngestionIdempotency:
    """Ingesting the same candidate twice with the same analysis_run_id
    should not create a duplicate occurrence."""

    def test_second_ingest_is_idempotent(self):
        mock_repo = MagicMock()
        finding_id = uuid4()
        run_id = uuid4()
        existing_finding = Finding(
            finding_id=finding_id,
            finding_type="ENTRY",
            status="OPEN",
            fingerprint="abc123",
        )
        # First call: no existing finding
        mock_repo.get_finding_by_fingerprint.return_value = None
        mock_repo.create_finding_with_fingerprint.return_value = existing_finding
        occ = FindingOccurrence(
            occurrence_id=uuid4(),
            finding_id=finding_id,
            analysis_run_id=run_id,
        )
        mock_repo.create_occurrence_if_absent.return_value = (occ, True)
        # No existing occurrences for first call
        mock_repo.list_occurrences_for_finding.return_value = []

        from app.analytics.agents.research.ingestion import FindingIngestionService

        service = FindingIngestionService(mock_repo)
        candidate = _make_candidate(run_id=run_id)
        result1 = service.ingest_candidate(candidate)

        assert result1.created_finding is True
        assert result1.created_occurrence is True
        assert result1.idempotent_replay is False

        # Second call: same fingerprint → existing finding found
        mock_repo.get_finding_by_fingerprint.return_value = existing_finding
        mock_repo.create_occurrence_if_absent.return_value = (occ, False)
        mock_repo.list_occurrences_for_finding.return_value = [
            _make_occurrence(finding_id=finding_id, run_id=run_id),
        ]

        result2 = service.ingest_candidate(candidate)
        assert result2.created_finding is False
        assert result2.idempotent_replay is True


# ======================================================================
# 22. Ingestion — ON CONFLICT occurrence → created=False
# ======================================================================

class TestIngestionOnConflictOccurrence:
    """When occurrence already exists (ON CONFLICT DO NOTHING), created=False."""

    def test_conflict_returns_created_false(self):
        mock_repo = MagicMock()
        finding_id = uuid4()
        existing_finding = Finding(
            finding_id=finding_id,
            finding_type="ENTRY",
            status="OPEN",
            fingerprint="abc",
        )
        mock_repo.get_finding_by_fingerprint.return_value = existing_finding
        occ = FindingOccurrence(
            occurrence_id=uuid4(),
            finding_id=finding_id,
        )
        mock_repo.create_occurrence_if_absent.return_value = (occ, False)
        mock_repo.list_occurrences_for_finding.return_value = [
            _make_occurrence(finding_id=finding_id),
        ]

        from app.analytics.agents.research.ingestion import FindingIngestionService

        service = FindingIngestionService(mock_repo)
        candidate = _make_candidate()
        result = service.ingest_candidate(candidate)

        assert result.created_occurrence is False
        assert result.idempotent_replay is True


# ======================================================================
# 23. Concurrent same fingerprint → 1 finding
# ======================================================================

class TestConcurrentSameFingerprint:
    """Two candidates with the same fingerprint should create only 1 finding."""

    def test_same_fingerprint_single_finding(self):
        mock_repo = MagicMock()
        finding_id = uuid4()
        existing_finding = Finding(
            finding_id=finding_id,
            finding_type="ENTRY",
            status="OPEN",
            fingerprint="same_hash",
        )

        from app.analytics.agents.research.ingestion import FindingIngestionService

        service = FindingIngestionService(mock_repo)
        candidate1 = _make_candidate(statement="First")
        candidate2 = _make_candidate(statement="Second")

        # First candidate creates the finding
        mock_repo.get_finding_by_fingerprint.return_value = None
        mock_repo.create_finding_with_fingerprint.return_value = existing_finding
        mock_repo.create_occurrence_if_absent.return_value = (
            FindingOccurrence(occurrence_id=uuid4(), finding_id=finding_id),
            True,
        )
        mock_repo.list_occurrences_for_finding.return_value = [
            _make_occurrence(finding_id=finding_id, business_date="2024-01-01"),
        ]
        result1 = service.ingest_candidate(candidate1)
        assert result1.created_finding is True

        # Second candidate links to existing finding
        mock_repo.get_finding_by_fingerprint.return_value = existing_finding
        mock_repo.create_occurrence_if_absent.return_value = (
            FindingOccurrence(occurrence_id=uuid4(), finding_id=finding_id),
            True,
        )
        mock_repo.list_occurrences_for_finding.return_value = [
            _make_occurrence(finding_id=finding_id, business_date="2024-01-01"),
            _make_occurrence(finding_id=finding_id, business_date="2024-01-02"),
        ]
        result2 = service.ingest_candidate(candidate2)
        assert result2.created_finding is False
        assert result2.finding_id == finding_id

        # Only one create_finding_with_fingerprint call
        assert mock_repo.create_finding_with_fingerprint.call_count == 1

    def test_concurrent_unique_violation_recover(self):
        """When create_finding_with_fingerprint raises UNIQUE violation,
        ingestion re-reads the existing finding."""
        mock_repo = MagicMock()
        finding_id = uuid4()
        existing_finding = Finding(
            finding_id=finding_id,
            finding_type="ENTRY",
            status="OPEN",
            fingerprint="same_hash",
        )

        from app.analytics.agents.research.ingestion import FindingIngestionService

        service = FindingIngestionService(mock_repo)
        candidate = _make_candidate()

        # First attempt: fingerprint lookup = None, insert raises
        mock_repo.get_finding_by_fingerprint.side_effect = [
            None,  # first lookup
            existing_finding,  # re-read after conflict
        ]
        mock_repo.create_finding_with_fingerprint.side_effect = Exception(
            "duplicate key value violates unique constraint"
        )
        mock_repo.create_occurrence_if_absent.return_value = (
            FindingOccurrence(occurrence_id=uuid4(), finding_id=finding_id),
            True,
        )
        mock_repo.list_occurrences_for_finding.return_value = [
            _make_occurrence(finding_id=finding_id),
        ]

        result = service.ingest_candidate(candidate)
        assert result.created_finding is False
        assert result.finding_id == finding_id


# ======================================================================
# 24. first_seen / last_seen correct via aggregate refresh
# ======================================================================

class TestAggregateRefresh:
    """After ingestion, refresh_finding_aggregate is called."""

    def test_aggregate_refresh_called(self):
        mock_repo = MagicMock()
        finding_id = uuid4()
        existing_finding = Finding(
            finding_id=finding_id,
            finding_type="ENTRY",
            status="OPEN",
            fingerprint="abc",
        )
        mock_repo.get_finding_by_fingerprint.return_value = existing_finding
        mock_repo.create_occurrence_if_absent.return_value = (
            FindingOccurrence(occurrence_id=uuid4(), finding_id=finding_id),
            True,
        )
        mock_repo.list_occurrences_for_finding.return_value = [
            _make_occurrence(finding_id=finding_id, business_date="2024-01-01"),
        ]

        from app.analytics.agents.research.ingestion import FindingIngestionService

        service = FindingIngestionService(mock_repo)
        candidate = _make_candidate()
        service.ingest_candidate(candidate)
        mock_repo.refresh_finding_aggregate.assert_called_once_with(finding_id)


# ======================================================================
# 25. Transaction rollback — no partial state
# ======================================================================

class TestIngestionTransactionRollback:
    """If occurrence creation fails, the entire operation should be rolled back."""

    def test_occurrence_failure_rolls_back(self):
        mock_repo = MagicMock()
        finding_id = uuid4()
        existing_finding = Finding(
            finding_id=finding_id,
            finding_type="ENTRY",
            status="OPEN",
            fingerprint="abc",
        )
        mock_repo.get_finding_by_fingerprint.return_value = existing_finding
        # Occurrence creation fails
        mock_repo.create_occurrence_if_absent.side_effect = RuntimeError("DB connection lost")

        from app.analytics.agents.research.ingestion import FindingIngestionService

        service = FindingIngestionService(mock_repo)
        candidate = _make_candidate()

        with pytest.raises(RuntimeError, match="DB connection lost"):
            service.ingest_candidate(candidate)

        # refresh should NOT have been called
        mock_repo.refresh_finding_aggregate.assert_not_called()


# ======================================================================
# 26. Batch summary counts
# ======================================================================

class TestBatchSummary:
    def test_batch_summary_counts(self):
        mock_repo = MagicMock()
        finding_id = uuid4()
        existing_finding = Finding(
            finding_id=finding_id,
            finding_type="ENTRY",
            status="OPEN",
            fingerprint="abc",
        )

        from app.analytics.agents.research.ingestion import FindingIngestionService

        service = FindingIngestionService(mock_repo)
        candidates = [
            _make_candidate(statement=f"Candidate {i}")
            for i in range(3)
        ]

        mock_repo.get_finding_by_fingerprint.return_value = existing_finding
        mock_repo.create_occurrence_if_absent.return_value = (
            FindingOccurrence(occurrence_id=uuid4(), finding_id=finding_id),
            True,
        )
        mock_repo.list_occurrences_for_finding.return_value = [
            _make_occurrence(finding_id=finding_id, business_date="2024-01-01"),
        ]

        summary = service.ingest_batch(candidates)
        assert summary.candidates_seen == 3
        assert summary.linked_findings == 3
        assert summary.errors == 0

    def test_batch_skips_malformed_continues_rest(self):
        """Malformed candidates (ValueError) are skipped; rest of batch continues."""
        mock_repo = MagicMock()
        finding_id = uuid4()
        existing_finding = Finding(
            finding_id=finding_id,
            finding_type="ENTRY",
            status="OPEN",
            fingerprint="abc",
        )

        from app.analytics.agents.research.ingestion import FindingIngestionService

        service = FindingIngestionService(mock_repo)

        good_candidate = _make_candidate(statement="Good")
        bad_candidate = _make_candidate(statement="Bad")
        bad_candidate.analysis_run_id = None  # type: ignore[assignment]

        mock_repo.get_finding_by_fingerprint.return_value = existing_finding
        mock_repo.create_occurrence_if_absent.return_value = (
            FindingOccurrence(occurrence_id=uuid4(), finding_id=finding_id),
            True,
        )
        mock_repo.list_occurrences_for_finding.return_value = [
            _make_occurrence(finding_id=finding_id, business_date="2024-01-01"),
        ]

        summary = service.ingest_batch([bad_candidate, good_candidate])
        assert summary.candidates_seen == 2
        assert summary.skipped_invalid == 1
        assert summary.linked_findings == 1

    def test_batch_db_failure_propagates(self):
        """DB/system failure propagates and stops the batch."""
        mock_repo = MagicMock()

        from app.analytics.agents.research.ingestion import FindingIngestionService

        service = FindingIngestionService(mock_repo)

        # First candidate fails with a DB error (not ValueError)
        mock_repo.get_finding_by_fingerprint.side_effect = RuntimeError("DB down")
        candidates = [_make_candidate()]

        with pytest.raises(RuntimeError, match="DB down"):
            service.ingest_batch(candidates)


# ======================================================================
# 27. Stage3 adapter — missing schema_version → rejected
# ======================================================================

class TestStage3AdapterSchemaValidation:
    def test_missing_schema_version_rejected(self):
        from app.analytics.agents.research.adapter import Stage3FindingCandidateAdapter

        adapter = Stage3FindingCandidateAdapter()
        result = adapter.extract_candidates(
            analysis_run_id=uuid4(),
            agent_name="TEST_AGENT",
            result_json={
                # No schema_version
                "observations": [
                    {
                        "finding_type": "ENTRY",
                        "scanner_name": "ME",
                        "metric_name": "pnl_r",
                        "comparator": "LT",
                        "evidence_refs": ["evidence://1"],
                    }
                ],
            },
        )
        assert result == []

    def test_unknown_schema_version_skipped(self):
        from app.analytics.agents.research.adapter import Stage3FindingCandidateAdapter

        adapter = Stage3FindingCandidateAdapter()
        result = adapter.extract_candidates(
            analysis_run_id=uuid4(),
            agent_name="TEST_AGENT",
            result_json={
                "schema_version": "unknown-version-v99",
                "observations": [
                    {
                        "finding_type": "ENTRY",
                        "scanner_name": "ME",
                        "metric_name": "pnl_r",
                        "comparator": "LT",
                        "evidence_refs": ["evidence://1"],
                    }
                ],
            },
        )
        assert result == []

    def test_known_schema_version_accepted(self):
        from app.analytics.agents.research.adapter import Stage3FindingCandidateAdapter

        adapter = Stage3FindingCandidateAdapter()
        result = adapter.extract_candidates(
            analysis_run_id=uuid4(),
            agent_name="TEST_AGENT",
            result_json={
                "schema_version": "agent-result-v1",
                "dataset_version": "2024-01",
                "observations": [
                    {
                        "finding_type": "ENTRY",
                        "scanner_name": "ME",
                        "metric_name": "pnl_r",
                        "comparator": "LT",
                        "direction": "SHORT",
                        "evidence_refs": ["evidence://1"],
                        "statement": "Test finding",
                    }
                ],
            },
        )
        assert len(result) == 1
        assert result[0].finding_type == "ENTRY"

    def test_all_known_versions_accepted(self):
        from app.analytics.agents.research.adapter import (
            Stage3FindingCandidateAdapter,
            SUPPORTED_SCHEMA_VERSIONS,
        )

        adapter = Stage3FindingCandidateAdapter()
        for version in SUPPORTED_SCHEMA_VERSIONS:
            result = adapter.extract_candidates(
                analysis_run_id=uuid4(),
                agent_name="TEST_AGENT",
                result_json={
                    "schema_version": version,
                    "observations": [
                        {
                            "finding_type": "ENTRY",
                            "scanner_name": "ME",
                            "metric_name": "pnl_r",
                            "comparator": "LT",
                            "evidence_refs": ["evidence://1"],
                        }
                    ],
                },
            )
            assert len(result) == 1, f"Schema version {version} should be accepted"


# ======================================================================
# 28. Stage3 adapter — fake evidence_ref → rejected
# ======================================================================

class TestStage3AdapterEvidenceValidation:
    def test_no_evidence_refs_skipped(self):
        from app.analytics.agents.research.adapter import Stage3FindingCandidateAdapter

        adapter = Stage3FindingCandidateAdapter()
        result = adapter.extract_candidates(
            analysis_run_id=uuid4(),
            agent_name="TEST_AGENT",
            result_json={
                "schema_version": "agent-result-v1",
                "observations": [
                    {
                        "finding_type": "ENTRY",
                        "scanner_name": "ME",
                        "metric_name": "pnl_r",
                        "comparator": "LT",
                        "evidence_refs": [],
                    }
                ],
            },
        )
        assert result == []

    def test_missing_evidence_refs_skipped(self):
        from app.analytics.agents.research.adapter import Stage3FindingCandidateAdapter

        adapter = Stage3FindingCandidateAdapter()
        result = adapter.extract_candidates(
            analysis_run_id=uuid4(),
            agent_name="TEST_AGENT",
            result_json={
                "schema_version": "agent-result-v1",
                "observations": [
                    {
                        "finding_type": "ENTRY",
                        "scanner_name": "ME",
                        "metric_name": "pnl_r",
                        "comparator": "LT",
                    }
                ],
            },
        )
        assert result == []

    def test_fake_evidence_ref_rejected(self):
        """When validated_evidence is provided, refs not in it are rejected."""
        from app.analytics.agents.research.adapter import Stage3FindingCandidateAdapter

        adapter = Stage3FindingCandidateAdapter()
        result = adapter.extract_candidates(
            analysis_run_id=uuid4(),
            agent_name="TEST_AGENT",
            result_json={
                "schema_version": "agent-result-v1",
                "validated_evidence": ["evidence://real/obs1", "evidence://real/obs2"],
                "observations": [
                    {
                        "finding_type": "ENTRY",
                        "scanner_name": "ME",
                        "metric_name": "pnl_r",
                        "comparator": "LT",
                        "evidence_refs": ["evidence://fake/obs99"],
                    }
                ],
            },
        )
        assert result == []

    def test_valid_evidence_ref_accepted(self):
        from app.analytics.agents.research.adapter import Stage3FindingCandidateAdapter

        adapter = Stage3FindingCandidateAdapter()
        result = adapter.extract_candidates(
            analysis_run_id=uuid4(),
            agent_name="TEST_AGENT",
            result_json={
                "schema_version": "agent-result-v1",
                "validated_evidence": ["evidence://real/obs1", "evidence://real/obs2"],
                "observations": [
                    {
                        "finding_type": "ENTRY",
                        "scanner_name": "ME",
                        "metric_name": "pnl_r",
                        "comparator": "LT",
                        "evidence_refs": ["evidence://real/obs1"],
                    }
                ],
            },
        )
        assert len(result) == 1


# ======================================================================
# 29. Stage3 adapter — item cannot override analysis_run_id
# ======================================================================

class TestStage3AdapterAnalysisRunIdLocked:
    def test_item_cannot_override_analysis_run_id(self):
        from app.analytics.agents.research.adapter import Stage3FindingCandidateAdapter

        adapter = Stage3FindingCandidateAdapter()
        outer_run_id = uuid4()
        result = adapter.extract_candidates(
            analysis_run_id=outer_run_id,
            agent_name="TEST_AGENT",
            result_json={
                "schema_version": "agent-result-v1",
                "observations": [
                    {
                        "finding_type": "ENTRY",
                        "scanner_name": "ME",
                        "metric_name": "pnl_r",
                        "comparator": "LT",
                        "evidence_refs": ["evidence://1"],
                        "analysis_run_id": str(uuid4()),  # Different ID!
                    }
                ],
            },
        )
        assert result == []  # Rejected because item tried to override

    def test_item_matching_analysis_run_id_accepted(self):
        from app.analytics.agents.research.adapter import Stage3FindingCandidateAdapter

        adapter = Stage3FindingCandidateAdapter()
        outer_run_id = uuid4()
        result = adapter.extract_candidates(
            analysis_run_id=outer_run_id,
            agent_name="TEST_AGENT",
            result_json={
                "schema_version": "agent-result-v1",
                "observations": [
                    {
                        "finding_type": "ENTRY",
                        "scanner_name": "ME",
                        "metric_name": "pnl_r",
                        "comparator": "LT",
                        "evidence_refs": ["evidence://1"],
                        "analysis_run_id": str(outer_run_id),  # Same ID
                    }
                ],
            },
        )
        assert len(result) == 1
        assert result[0].analysis_run_id == outer_run_id


# ======================================================================
# 30. Stage3 adapter — missing metric_name → skip
# ======================================================================

class TestStage3AdapterMetricValidation:
    def test_empty_metric_name_skipped(self):
        from app.analytics.agents.research.adapter import Stage3FindingCandidateAdapter

        adapter = Stage3FindingCandidateAdapter()
        result = adapter.extract_candidates(
            analysis_run_id=uuid4(),
            agent_name="TEST_AGENT",
            result_json={
                "schema_version": "agent-result-v1",
                "observations": [
                    {
                        "finding_type": "ENTRY",
                        "scanner_name": "ME",
                        "metric_name": "",
                        "comparator": "LT",
                        "evidence_refs": ["evidence://1"],
                    }
                ],
            },
        )
        assert result == []


# ======================================================================
# 31. Stage3 adapter — anomalies extracted
# ======================================================================

class TestStage3AdapterAnomalies:
    def test_anomalies_extracted(self):
        from app.analytics.agents.research.adapter import Stage3FindingCandidateAdapter

        adapter = Stage3FindingCandidateAdapter()
        result = adapter.extract_candidates(
            analysis_run_id=uuid4(),
            agent_name="TEST_AGENT",
            result_json={
                "schema_version": "agent-result-v1",
                "anomalies": [
                    {
                        "finding_type": "DRIFT",
                        "scanner_name": "ME",
                        "metric_name": "score_mean",
                        "comparator": "ANOMALY",
                        "direction": "NONE",
                        "evidence_refs": ["evidence://anom1"],
                        "statement": "Score drift detected",
                    }
                ],
            },
        )
        assert len(result) == 1
        assert result[0].finding_type == "DRIFT"


# ======================================================================
# 32. No financial metric calculation in ingestion
# ======================================================================

class TestNoFinancialCalculation:
    """Ingestion must not compute financial metrics."""

    def test_ingestion_has_no_calculation_code(self):
        import inspect
        from app.analytics.agents.research import ingestion as ing_mod

        source = inspect.getsource(ing_mod)
        forbidden_identifiers = ["compute_pnl", "compute_risk", "sharpe", "sortino", "expectancy"]
        for term in forbidden_identifiers:
            assert term.lower() not in source.lower(), (
                f"Ingestion module contains forbidden identifier: {term}"
            )


# ======================================================================
# 33. No config/paper writes in ingestion
# ======================================================================

class TestNoConfigPaperWrites:
    """Ingestion must not write to config or paper tables."""

    def test_ingestion_no_config_reference(self):
        import inspect
        from app.analytics.agents.research import ingestion as ing_mod

        source = inspect.getsource(ing_mod)
        forbidden = ["paper_trade", "paper_state", "scanner_config", "config_snapshot"]
        for term in forbidden:
            assert term.lower() not in source.lower(), (
                f"Ingestion module references forbidden table: {term}"
            )


# ======================================================================
# 34. Migration 037 exists and is correct
# ======================================================================

class TestMigration037:
    def test_migration_037_exists(self):
        assert MIGRATION_037_PATH.exists()

    def test_migration_037_adds_agent_run_id(self):
        content = MIGRATION_037_PATH.read_text()
        assert "agent_run_id" in content

    def test_migration_037_adds_business_date(self):
        content = MIGRATION_037_PATH.read_text()
        assert "business_date" in content

    def test_migration_037_adds_maturity(self):
        content = MIGRATION_037_PATH.read_text()
        assert "maturity" in content

    def test_migration_037_adds_source_agent_name(self):
        content = MIGRATION_037_PATH.read_text()
        assert "source_agent_name" in content

    def test_migration_037_adds_fingerprint_unique_index(self):
        content = MIGRATION_037_PATH.read_text()
        assert "idx_finding_fingerprint_unique" in content

    def test_migration_037_is_idempotent(self):
        content = MIGRATION_037_PATH.read_text()
        assert "IF NOT EXISTS" in content

    def test_migration_037_only_writes_research_schema(self):
        content = MIGRATION_037_PATH.read_text()
        assert "CREATE TABLE IF NOT EXISTS analytics." not in content
        assert "CREATE TABLE IF NOT EXISTS paper." not in content


# ======================================================================
# 35. Models — FindingCandidate
# ======================================================================

class TestFindingCandidateModel:
    def test_creation(self):
        candidate = FindingCandidate(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={"symbol": "btcusdt"},
            metric_name="pnl_r",
            comparator="LT",
            threshold_policy_version="finding-thresholds-v1",
            statement="Test",
            scope_json={},
            metric_value=None,
            sample_size=0,
            confidence="LOW",
            evidence_refs=[],
            analysis_run_id=uuid4(),
            agent_run_id=None,
            dataset_version="v1",
            observed_at=datetime.now(timezone.utc),
            source_agent_name="TEST",
        )
        assert candidate.finding_type == "ENTRY"
        assert candidate.maturity == "PROVISIONAL"
        assert candidate.analysis_run_id is not None

    def test_analysis_run_id_required(self):
        """analysis_run_id is required — cannot be None at type level."""
        candidate = FindingCandidate(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={},
            metric_name="pnl_r",
            comparator="LT",
            threshold_policy_version="v1",
            statement="",
            scope_json={},
            metric_value=None,
            sample_size=0,
            confidence="LOW",
            evidence_refs=[],
            analysis_run_id=uuid4(),
            agent_run_id=None,
            dataset_version="v1",
            observed_at=datetime.now(timezone.utc),
            source_agent_name="TEST",
        )
        assert isinstance(candidate.analysis_run_id, type(uuid4()))

    def test_custom_maturity(self):
        candidate = FindingCandidate(
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            normalized_segment={},
            metric_name="pnl_r",
            comparator="LT",
            threshold_policy_version="v1",
            statement="",
            scope_json={},
            metric_value=None,
            sample_size=0,
            confidence="LOW",
            evidence_refs=[],
            analysis_run_id=uuid4(),
            agent_run_id=None,
            dataset_version="v1",
            observed_at=datetime.now(timezone.utc),
            source_agent_name="TEST",
            maturity="CONFIRMED",
        )
        assert candidate.maturity == "CONFIRMED"


# ======================================================================
# 36. Models — FindingIngestionResult
# ======================================================================

class TestFindingIngestionResultModel:
    def test_defaults(self):
        result = FindingIngestionResult(
            finding_id=uuid4(),
            fingerprint="abc123",
        )
        assert result.created_finding is False
        assert result.created_occurrence is False
        assert result.idempotent_replay is False
        assert result.old_status is None
        assert result.new_status is None
        assert result.policy_action == "NO_CHANGE"


# ======================================================================
# 37. Models — IngestionBatchSummary
# ======================================================================

class TestIngestionBatchSummaryModel:
    def test_defaults(self):
        summary = IngestionBatchSummary()
        assert summary.candidates_seen == 0
        assert summary.created_findings == 0
        assert summary.linked_findings == 0
        assert summary.created_occurrences == 0
        assert summary.idempotent_replays == 0
        assert summary.promoted_repeated == 0
        assert summary.promoted_research_required == 0
        assert summary.skipped_invalid == 0
        assert summary.errors == 0


# ======================================================================
# 38. Repository methods existence check
# ======================================================================

class TestRepositoryMethodsExist:
    def _repo_methods(self) -> set[str]:
        from app.analytics.agents.research import repository as repo_mod
        return set(dir(repo_mod.ResearchRepository))

    def test_get_finding_by_fingerprint(self):
        assert "get_finding_by_fingerprint" in self._repo_methods()

    def test_create_finding_with_fingerprint(self):
        assert "create_finding_with_fingerprint" in self._repo_methods()

    def test_upsert_fingerprint(self):
        assert "upsert_fingerprint" in self._repo_methods()

    def test_create_occurrence_if_absent(self):
        assert "create_occurrence_if_absent" in self._repo_methods()

    def test_list_occurrences_for_finding(self):
        assert "list_occurrences_for_finding" in self._repo_methods()

    def test_refresh_finding_aggregate(self):
        assert "refresh_finding_aggregate" in self._repo_methods()


# ======================================================================
# 39. FindingOccurrence model has PR2 fields
# ======================================================================

class TestFindingOccurrencePR2Fields:
    def test_agent_run_id_field(self):
        occ = FindingOccurrence(
            finding_id=uuid4(),
            agent_run_id=uuid4(),
        )
        assert occ.agent_run_id is not None

    def test_business_date_field(self):
        occ = FindingOccurrence(
            finding_id=uuid4(),
            business_date="2024-01-15",
        )
        assert occ.business_date == "2024-01-15"

    def test_maturity_field(self):
        occ = FindingOccurrence(
            finding_id=uuid4(),
            maturity="CONFIRMED",
        )
        assert occ.maturity == "CONFIRMED"

    def test_source_agent_name_field(self):
        occ = FindingOccurrence(
            finding_id=uuid4(),
            source_agent_name="EXECUTION_QUALITY",
        )
        assert occ.source_agent_name == "EXECUTION_QUALITY"

    def test_defaults(self):
        occ = FindingOccurrence(finding_id=uuid4())
        assert occ.maturity == "PROVISIONAL"
        assert occ.source_agent_name == ""
        assert occ.agent_run_id is None
        assert occ.business_date is None


# ======================================================================
# 40. Transition policy integration
# ======================================================================

class TestTransitionPolicyIntegration:
    """Verify that ingestion respects transition policy."""

    def test_cannot_skip_from_open_to_research_required(self):
        with pytest.raises(ValueError, match="Illegal transition"):
            ResearchTransitionPolicyV1.validate("finding", "OPEN", "RESEARCH_REQUIRED")

    def test_valid_open_to_repeated(self):
        ResearchTransitionPolicyV1.validate("finding", "OPEN", "REPEATED")

    def test_valid_repeated_to_research_required(self):
        ResearchTransitionPolicyV1.validate("finding", "REPEATED", "RESEARCH_REQUIRED")


# ======================================================================
# 41. Ingestion handles policy transition failures gracefully
# ======================================================================

class TestIngestionPolicyTransitionFailure:
    def test_failed_transition_returns_old_status(self):
        mock_repo = MagicMock()
        finding_id = uuid4()
        existing_finding = Finding(
            finding_id=finding_id,
            finding_type="ENTRY",
            status="CLOSED",
            fingerprint="abc",
        )
        mock_repo.get_finding_by_fingerprint.return_value = existing_finding
        mock_repo.create_occurrence_if_absent.return_value = (
            FindingOccurrence(occurrence_id=uuid4(), finding_id=finding_id),
            True,
        )
        mock_repo.list_occurrences_for_finding.return_value = [
            _make_occurrence(finding_id=finding_id, confidence="HIGH", business_date="2024-01-01"),
        ] * 10
        mock_repo.update_finding_status.side_effect = ValueError(
            "No transitions allowed from 'CLOSED'"
        )

        from app.analytics.agents.research.ingestion import FindingIngestionService

        service = FindingIngestionService(mock_repo)
        candidate = _make_candidate(
            threshold_policy_version="v1",
            confidence="HIGH",
        )
        result = service.ingest_candidate(candidate)
        assert result.new_status == "CLOSED"
        assert result.old_status == "CLOSED"


# ======================================================================
# 42. Finding model fingerprint field
# ======================================================================

class TestFindingModelFingerprint:
    def test_fingerprint_populated_by_pr2(self):
        f = Finding(
            finding_type="ENTRY",
            title="Test",
            fingerprint="abc123def456",
        )
        assert f.fingerprint == "abc123def456"

    def test_fingerprint_none_by_default(self):
        f = Finding()
        assert f.fingerprint is None


# ======================================================================
# 43. Repeat policy — edge case: empty occurrences
# ======================================================================

class TestRepeatPolicyEmptyOccurrences:
    def test_empty_occurrences_keeps_open(self):
        config = FindingRepeatPolicyConfig(repeated_min_independent_occurrences=3)
        action = FindingRepeatPolicyV1.evaluate(config, [], "OPEN")
        assert action == KEEP_OPEN

    def test_empty_occurrences_no_change_on_repeated(self):
        config = FindingRepeatPolicyConfig(
            research_required_min_occurrences=5,
            min_distinct_business_dates=2,
        )
        action = FindingRepeatPolicyV1.evaluate(config, [], "REPEATED")
        assert action == NO_CHANGE


# ======================================================================
# 44. Ingestion — no config → promotion disabled
# ======================================================================

class TestIngestionNoConfigPromotionDisabled:
    def test_no_config_returns_promotion_disabled(self):
        mock_repo = MagicMock()
        finding_id = uuid4()
        existing_finding = Finding(
            finding_id=finding_id,
            finding_type="ENTRY",
            status="OPEN",
            fingerprint="abc",
        )
        mock_repo.get_finding_by_fingerprint.return_value = existing_finding
        mock_repo.create_occurrence_if_absent.return_value = (
            FindingOccurrence(occurrence_id=uuid4(), finding_id=finding_id),
            True,
        )
        mock_repo.list_occurrences_for_finding.return_value = [
            _make_occurrence(finding_id=finding_id, confidence="HIGH", business_date=f"2024-01-{i:02d}")
            for i in range(1, 11)
        ]

        from app.analytics.agents.research.ingestion import FindingIngestionService

        service = FindingIngestionService(mock_repo, policy_config=None)
        candidate = _make_candidate(confidence="HIGH")
        result = service.ingest_candidate(candidate)
        assert result.policy_action == PROMOTION_DISABLED
        # Status should remain OPEN (no promotion)
        assert result.new_status == "OPEN"
