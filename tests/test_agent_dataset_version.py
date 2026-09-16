"""Tests for Stage 3 deterministic dataset_version computation."""
import pytest
from datetime import datetime, timezone

from app.analytics.agents.dataset_version import (
    compute_dataset_version,
    build_publication_manifest,
)


class TestComputeDatasetVersion:
    def test_deterministic_same_inputs(self):
        """#11: same manifest → same hash."""
        dt = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
        v1 = compute_dataset_version("run-1", dt, dt, dt, "FINAL")
        v2 = compute_dataset_version("run-1", dt, dt, dt, "FINAL")
        assert v1 == v2

    def test_field_order_irrelevant(self):
        """#11: field order change → same hash (sorted keys)."""
        dt = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
        v1 = compute_dataset_version("run-1", dt, dt, dt, "FINAL", canonical_schema_version="2.0")
        v2 = compute_dataset_version("run-1", dt, dt, dt, "FINAL", canonical_schema_version="2.0")
        assert v1 == v2

    def test_material_field_change_different_hash(self):
        """#11: material field change → different hash."""
        dt = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
        v1 = compute_dataset_version("run-1", dt, dt, dt, "FINAL")
        v2 = compute_dataset_version("run-2", dt, dt, dt, "FINAL")
        assert v1 != v2

    def test_provisional_different_from_final(self):
        """#10: PROVISIONAL/FINAL → different hash."""
        dt = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
        v1 = compute_dataset_version("run-1", dt, dt, dt, "PROVISIONAL")
        v2 = compute_dataset_version("run-1", dt, dt, dt, "FINAL")
        assert v1 != v2

    def test_hex_64_chars(self):
        """Output is 64-char hex string."""
        dt = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
        v = compute_dataset_version("run-1", dt, dt, dt, "FINAL")
        assert len(v) == 64
        assert all(c in "0123456789abcdef" for c in v)

    def test_naive_datetime_treated_as_utc(self):
        """Naive datetime assumed UTC."""
        dt_naive = datetime(2026, 9, 15, 6, 0)
        dt_aware = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
        v1 = compute_dataset_version("run-1", dt_naive, dt_naive, dt_naive, "FINAL")
        v2 = compute_dataset_version("run-1", dt_aware, dt_aware, dt_aware, "FINAL")
        assert v1 == v2

    def test_different_window_different_hash(self):
        """Different analysis window → different hash."""
        dt1 = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
        dt2 = datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc)
        v1 = compute_dataset_version("run-1", dt1, dt2, dt2, "FINAL")
        v2 = compute_dataset_version("run-1", dt1, dt2, datetime(2026, 9, 16, 7, 0, tzinfo=timezone.utc), "FINAL")
        assert v1 != v2


class TestBuildPublicationManifest:
    def test_all_fields_included(self):
        dt = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
        m = build_publication_manifest(
            analysis_run_id="run-1",
            analysis_window_from=dt, analysis_window_to=dt,
            observation_cutoff=dt, maturity="FINAL",
        )
        assert m["analysis_run_id"] == "run-1"
        assert m["maturity"] == "FINAL"
        assert m["canonical_schema_version"] == "1.0"

    def test_canonical_build_identity_included(self):
        dt = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
        m = build_publication_manifest(
            analysis_run_id="run-1",
            analysis_window_from=dt, analysis_window_to=dt,
            observation_cutoff=dt, maturity="FINAL",
            canonical_build_identity="abc123",
        )
        assert m["canonical_build_identity"] == "abc123"

    def test_no_volatile_fields(self):
        dt = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
        m = build_publication_manifest(
            analysis_run_id="run-1",
            analysis_window_from=dt, analysis_window_to=dt,
            observation_cutoff=dt, maturity="FINAL",
        )
        assert "created_at" not in m
        assert "updated_at" not in m
        assert "agent_run_id" not in m
