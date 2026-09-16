"""Deterministic dataset_version computation for Stage 3 publication.

The ``dataset_version`` is a content-addressed hash derived from the
publication manifest.  It must be **deterministic** — given the same
logical inputs the version hash is always the same — and it must be
**stable** — volatile fields like ``created_at`` or random UUIDs must
NOT influence the hash.

Manifest fields that INFLUENCE the version:
    - analysis_run_id
    - analysis_window_from / analysis_window_to (ISO timestamps)
    - observation_cutoff (ISO timestamp)
    - maturity
    - canonical_schema_version
    - quality_policy_version
    - canonical_build_identity

Manifest fields that DO NOT influence the version (excluded):
    - created_at, updated_at
    - random UUID / agent_run_id
    - any volatile timestamp
    - agent_run_id (per-manifest, not per-dataset)

Usage::

    from app.analytics.agents.dataset_version import (
        compute_dataset_version,
        build_publication_manifest,
    )

    manifest = build_publication_manifest(
        analysis_run_id="...",
        analysis_window_from=dt_from,
        analysis_window_to=dt_to,
        observation_cutoff=cutoff,
        maturity="FINAL",
        canonical_build_identity="abc123",
    )
    version = compute_dataset_version(**manifest)
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Canonical JSON helpers ───────────────────────────────────────────

def _iso(dt: Any) -> str:
    """Normalise a datetime to a canonical ISO-8601 string.

    Handles datetime objects and string representations from pg8000.
    """
    if isinstance(dt, str):
        return dt  # Already a string (e.g. from pg8000), pass through
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(dt)


def _canonical_json(obj: Any) -> str:
    """Serialise *obj* to canonical JSON (sorted keys, no whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_hex(data: str) -> str:
    """Return the hex-encoded SHA-256 of *data*."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


# ── Manifest builder ────────────────────────────────────────────────

def build_publication_manifest(
    *,
    analysis_run_id: str,
    analysis_window_from: datetime,
    analysis_window_to: datetime,
    observation_cutoff: datetime,
    maturity: str,
    canonical_schema_version: str = "1.0",
    quality_policy_version: str = "v1",
    canonical_build_identity: str = "",
) -> dict[str, Any]:
    """Construct the publication manifest dict.

    This is the **single source of truth** for what goes into the
    ``dataset_version`` hash.  All timestamps are normalised to UTC
    ISO-8601 before hashing.

    Parameters
    ----------
    analysis_run_id:
        String UUID of the analysis run.
    analysis_window_from:
        Start of the analysis window.
    analysis_window_to:
        End of the analysis window.
    observation_cutoff:
        Latest data timestamp allowed for inclusion.
    maturity:
        PROVISIONAL or FINAL.
    canonical_schema_version:
        Schema version of the canonical build output (default ``"1.0"``).
    quality_policy_version:
        Quality policy version string (default ``"v1"``).
    canonical_build_identity:
        A string identifying what was built — e.g. a hash of build
        metadata or a concatenation of build-stage run IDs.  Empty
        string means "no identity provided" (still valid for hashing,
        but callers should populate this for full reproducibility).

    Returns
    -------
    dict[str, Any]
        Manifest dict ready to be passed to ``compute_dataset_version``.
    """
    return {
        "analysis_run_id": str(analysis_run_id),
        "analysis_window_from": _iso(analysis_window_from),
        "analysis_window_to": _iso(analysis_window_to),
        "observation_cutoff": _iso(observation_cutoff),
        "maturity": str(maturity),
        "canonical_schema_version": str(canonical_schema_version),
        "quality_policy_version": str(quality_policy_version),
        "canonical_build_identity": str(canonical_build_identity),
    }


# ── Version computation ─────────────────────────────────────────────

def compute_dataset_version(
    analysis_run_id: str,
    analysis_window_from: datetime,
    analysis_window_to: datetime,
    observation_cutoff: datetime,
    maturity: str,
    canonical_schema_version: str = "1.0",
    quality_policy_version: str = "v1",
    canonical_build_identity: str = "",
) -> str:
    """Compute deterministic dataset_version from publication manifest fields.

    Uses SHA-256 of canonical JSON (sorted keys, normalised timestamps).

    **Includes**: analysis_run_id, window bounds, observation_cutoff,
    maturity, schema version, quality policy version, build identity.

    **Excludes**: created_at, updated_at, random UUID, agent_run_id,
    volatile timestamps.

    Parameters
    ----------
    analysis_run_id:
        String UUID of the analysis run.
    analysis_window_from:
        Start of the analysis window (datetime).
    analysis_window_to:
        End of the analysis window (datetime).
    observation_cutoff:
        Latest data timestamp allowed for inclusion (datetime).
    maturity:
        PROVISIONAL or FINAL.
    canonical_schema_version:
        Schema version string (default ``"1.0"``).
    quality_policy_version:
        Quality policy version (default ``"v1"``).
    canonical_build_identity:
        Identity string for the build (e.g. hash or run-ID chain).

    Returns
    -------
    str
        Hex-encoded SHA-256 digest, e.g. ``"a1b2c3d4..."``.
    """
    manifest = build_publication_manifest(
        analysis_run_id=analysis_run_id,
        analysis_window_from=analysis_window_from,
        analysis_window_to=analysis_window_to,
        observation_cutoff=observation_cutoff,
        maturity=maturity,
        canonical_schema_version=canonical_schema_version,
        quality_policy_version=quality_policy_version,
        canonical_build_identity=canonical_build_identity,
    )

    canonical = _canonical_json(manifest)
    version = _sha256_hex(canonical)

    logger.debug(
        "dataset_version computed: %s (run_id=%s, maturity=%s)",
        version,
        analysis_run_id,
        maturity,
    )

    return version
