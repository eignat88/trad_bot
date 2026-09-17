"""Deterministic fingerprinting for research findings.

FindingFingerprintV1 produces a SHA-256 content hash from a compact,
sorted-key JSON representation of the fingerprint payload.  The fingerprint
is the primary dedup key: findings with identical fingerprints are merged
into the same research.finding row.

Fingerprint exclusions (intentionally NOT part of the hash):
    - metric_value   (varies per occurrence)
    - sample_size    (varies per occurrence)
    - confidence     (varies per occurrence)
    - business_date  (varies per occurrence)
    - analysis_run_id (varies per occurrence)
    - agent_run_id   (varies per occurrence)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class FindingFingerprintPayload:
    """Immutable payload that uniquely identifies the *type* of a finding.

    All fields are required and non-nullable — a missing field should be
    represented as an explicit empty string or sentinel value.
    """

    finding_type: str                # ENTRY, DCA, STOP, EXIT, FUNNEL, DRIFT, DATA, INCIDENT
    scanner_name: str                # exact, trimmed, uppercase
    direction: str                   # LONG, SHORT, BOTH, NONE
    normalized_segment: dict         # sorted keys, normalised
    metric_name: str                 # validated metric reference
    comparator: str                  # GT, GTE, LT, LTE, EQ, NEQ, DELTA_POSITIVE, DELTA_NEGATIVE, CHANGE, ANOMALY
    threshold_policy_version: str    # e.g. "finding-thresholds-v1"


class FindingFingerprintV1:
    """Version 1 fingerprint computation — deterministic SHA-256."""

    VERSION = "v1"

    @staticmethod
    def compute(payload: FindingFingerprintPayload) -> str:
        """Return a 64-character lowercase hex SHA-256 hash.

        Canonical form:
            - Dataclass fields in declaration order
            - Compact JSON (no spaces after , or :)
            - Sorted keys for nested dicts
            - UTF-8 encoding
            - ASCII-only (ensure_ascii=True)
        """
        # Use dataclass fields in declaration order for deterministic key order
        ordered_dict: dict[str, object] = {}
        for f in fields(payload):
            value = getattr(payload, f.name)
            # Normalize dicts to sorted-key representation
            if isinstance(value, dict):
                value = dict(sorted(value.items()))
            ordered_dict[f.name] = value

        canonical = json.dumps(
            ordered_dict,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
