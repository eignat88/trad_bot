"""Normalizers for research finding ingestion.

Each normalizer converts a raw, unstructured input value into a canonical
form suitable for fingerprinting and storage.  Normalizers are pure
functions — no side effects, fully deterministic, trivially testable.
"""
from __future__ import annotations

import re
from typing import Optional


# ======================================================================
# Segment normalizer
# ======================================================================

class FindingSegmentNormalizerV1:
    """Produce a deterministic dict from an arbitrary input segment.

    Rules:
        - All keys are lowercased and stripped
        - All string values are lowercased and stripped
        - Nested dicts are recursively normalised
        - Lists are sorted (elements stringified for sort stability)
        - Keys are sorted alphabetically
        - None values are dropped
    """

    @classmethod
    def normalize(cls, segment: dict) -> dict:
        if not segment:
            return {}
        return dict(sorted(cls._normalize_dict(segment).items()))

    @classmethod
    def _normalize_dict(cls, d: dict) -> dict:
        result: dict = {}
        for k, v in d.items():
            nk = str(k).strip().lower()
            if v is None:
                continue
            if isinstance(v, dict):
                result[nk] = dict(sorted(cls._normalize_dict(v).items()))
            elif isinstance(v, list):
                result[nk] = cls._normalize_list(v)
            elif isinstance(v, str):
                result[nk] = v.strip().lower()
            else:
                result[nk] = v
        return dict(sorted(result.items()))

    @classmethod
    def _normalize_list(cls, lst: list) -> list:
        normalized = []
        for item in lst:
            if isinstance(item, dict):
                normalized.append(cls._normalize_dict(item))
            elif isinstance(item, str):
                normalized.append(item.strip().lower())
            else:
                normalized.append(item)
        try:
            return sorted(normalized, key=lambda x: str(x))
        except TypeError:
            return normalized


# ======================================================================
# Finding type normalizer
# ======================================================================

class FindingTypeNormalizer:
    """Map observation codes / agent output types → canonical FindingType string.

    Returns None for unknown/unmappable codes (rejected by ingestion).
    """

    _MAPPING: dict[str, str] = {
        # Entry-related
        "entry": "ENTRY",
        "ENTRY": "ENTRY",
        "entry_quality": "ENTRY",
        "entry_timing": "ENTRY",
        "entry_slippage": "ENTRY",
        "entry_delay": "ENTRY",
        "mae_entry": "ENTRY",
        "entry_mae": "ENTRY",
        # DCA-related
        "dca": "DCA",
        "DCA": "DCA",
        "dca_trigger": "DCA",
        "dca_quality": "DCA",
        # Stop-related
        "stop": "STOP",
        "STOP": "STOP",
        "stop_loss": "STOP",
        "stop_quality": "STOP",
        "stop_triggered": "STOP",
        "stop_drift": "STOP",
        # Exit-related
        "exit": "EXIT",
        "EXIT": "EXIT",
        "exit_quality": "EXIT",
        "exit_timing": "EXIT",
        "exit_management": "EXIT",
        "trailing_exit": "EXIT",
        # Funnel / performance
        "funnel": "FUNNEL",
        "FUNNEL": "FUNNEL",
        "performance": "FUNNEL",
        "signal_funnel": "FUNNEL",
        "funnel_analysis": "FUNNEL",
        "scanner_funnel": "FUNNEL",
        # Drift / anomaly
        "drift": "DRIFT",
        "DRIFT": "DRIFT",
        "anomaly": "DRIFT",
        "anomaly_detection": "DRIFT",
        "drift_detection": "DRIFT",
        "regime_drift": "DRIFT",
        # Data quality
        "data": "DATA",
        "DATA": "DATA",
        "data_quality": "DATA",
        "missing_data": "DATA",
        "data_integrity": "DATA",
        # Incident
        "incident": "INCIDENT",
        "INCIDENT": "INCIDENT",
        "system_incident": "INCIDENT",
        "error": "INCIDENT",
        "failure": "INCIDENT",
    }

    @classmethod
    def normalize(cls, raw: str) -> Optional[str]:
        """Return canonical FindingType string, or None if unmappable."""
        if not raw:
            return None
        key = raw.strip().upper().replace(" ", "_")
        # Try exact upper match first
        result = cls._MAPPING.get(key)
        if result:
            return result
        # Try raw (lowercase)
        result = cls._MAPPING.get(raw.strip().lower().replace(" ", "_"))
        return result


# ======================================================================
# Comparator normalizer
# ======================================================================

class ComparatorNormalizer:
    """Validate and normalise comparator strings.

    Returns None for unknown comparators (rejected by ingestion).
    """

    _VALID: set[str] = {
        "GT", "GTE", "LT", "LTE", "EQ", "NEQ",
        "DELTA_POSITIVE", "DELTA_NEGATIVE", "CHANGE", "ANOMALY",
    }

    _ALIASES: dict[str, str] = {
        ">": "GT",
        ">=": "GTE",
        "<": "LT",
        "<=": "LTE",
        "=": "EQ",
        "==": "EQ",
        "!=": "NEQ",
        "<>": "NEQ",
        "delta_positive": "DELTA_POSITIVE",
        "delta_negative": "DELTA_NEGATIVE",
        "change": "CHANGE",
        "anomaly": "ANOMALY",
    }

    @classmethod
    def normalize(cls, raw: str) -> Optional[str]:
        """Return canonical comparator string, or None if unknown."""
        if not raw:
            return None
        stripped = raw.strip()
        # Check alias first
        alias = cls._ALIASES.get(stripped)
        if alias:
            return alias
        # Check upper-cased
        upper = stripped.upper()
        if upper in cls._VALID:
            return upper
        return None


# ======================================================================
# Direction normalizer
# ======================================================================

class DirectionNormalizer:
    """Normalise trading direction to LONG / SHORT / BOTH / NONE."""

    _VALID: set[str] = {"LONG", "SHORT", "BOTH", "NONE"}

    _ALIASES: dict[str, str] = {
        "long": "LONG",
        "short": "SHORT",
        "both": "BOTH",
        "none": "NONE",
        "all": "BOTH",
        "either": "BOTH",
        "na": "NONE",
        "n/a": "NONE",
        "": "NONE",
    }

    @classmethod
    def normalize(cls, raw: str) -> str:
        """Return canonical direction. Defaults to NONE for unknown."""
        if not raw:
            return "NONE"
        stripped = raw.strip()
        lower = stripped.lower()
        alias = cls._ALIASES.get(lower)
        if alias:
            return alias
        upper = stripped.upper()
        if upper in cls._VALID:
            return upper
        return "NONE"


# ======================================================================
# Scanner normalizer
# ======================================================================

class ScannerNormalizer:
    """Normalise scanner name: trimmed, uppercase.

    Returns None for empty/blank scanner names (rejected by ingestion).
    """

    @classmethod
    def normalize(cls, raw: str) -> Optional[str]:
        """Return normalised scanner name, or None if blank."""
        if not raw:
            return None
        stripped = raw.strip()
        if not stripped:
            return None
        return stripped.upper()
