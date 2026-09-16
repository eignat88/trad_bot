"""Tests for Stage 3 Evidence Catalog."""
import pytest
from app.analytics.agents.evidence import (
    EvidenceCatalog, build_metric_id, build_case_id, build_quality_id,
)


# ---------------------------------------------------------------------------
# Evidence ID builders
# ---------------------------------------------------------------------------

class TestEvidenceIdBuilders:
    def test_build_metric_id(self):
        eid = build_metric_id("ME", "SHORT", "24h", "pnl_r")
        assert eid == "metric:scanner:ME:SHORT:24h:pnl_r"

    def test_build_case_id(self):
        eid = build_case_id("trade", "12345")
        assert eid == "case:trade:12345"

    def test_build_quality_id(self):
        eid = build_quality_id("symbol_coverage")
        assert eid == "quality:symbol_coverage"

    def test_metric_id_deterministic(self):
        """Same inputs → same ID (stable and deterministic)."""
        id1 = build_metric_id("ME", "LONG", "7d", "win_rate")
        id2 = build_metric_id("ME", "LONG", "7d", "win_rate")
        assert id1 == id2


# ---------------------------------------------------------------------------
# EvidenceCatalog
# ---------------------------------------------------------------------------

class TestEvidenceCatalog:
    def test_validate_known_id(self):
        cat = EvidenceCatalog(["metric:scanner:ME:SHORT:24h:pnl_r"])
        assert cat.validate("metric:scanner:ME:SHORT:24h:pnl_r") is True

    def test_validate_unknown_id(self):
        cat = EvidenceCatalog(["metric:scanner:ME:SHORT:24h:pnl_r"])
        assert cat.validate("metric:scanner:ME:LONG:24h:pnl_r") is False

    def test_validate_refs_all_valid(self):
        cat = EvidenceCatalog(["metric:a", "metric:b"])
        invalid = cat.validate_refs(["metric:a", "metric:b"])
        assert invalid == []

    def test_validate_refs_some_invalid(self):
        cat = EvidenceCatalog(["metric:a"])
        invalid = cat.validate_refs(["metric:a", "metric:unknown"])
        assert invalid == ["metric:unknown"]

    def test_validate_refs_all_invalid(self):
        cat = EvidenceCatalog(["metric:a"])
        invalid = cat.validate_refs(["metric:x", "metric:y"])
        assert sorted(invalid) == ["metric:x", "metric:y"]

    def test_empty_catalog_rejects_all(self):
        cat = EvidenceCatalog([])
        assert cat.validate("metric:anything") is False

    def test_catalog_from_ids(self):
        ids = ["metric:a", "case:trade:1", "quality:coverage"]
        cat = EvidenceCatalog(ids)
        for eid in ids:
            assert cat.validate(eid) is True
