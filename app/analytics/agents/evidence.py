"""Evidence catalog for analytical agents.

Every metric, case, and quality check that agents reference must have a
stable, deterministic, human-readable ID.  The builder functions produce
these IDs and the ``EvidenceCatalog`` validates that referenced IDs
actually exist in the current dataset manifest.
"""

from __future__ import annotations

from typing import List, Set


# ---------------------------------------------------------------------------
# ID builder helpers
# ---------------------------------------------------------------------------

_SEPARATOR = ":"


def build_metric_id(
    scanner: str,
    direction: str,
    window: str,
    metric_name: str,
) -> str:
    """Build a deterministic metric evidence ID.

    Example
    -------
    >>> build_metric_id("ME", "SHORT", "24h", "pnl_r")
    'metric:scanner:ME:SHORT:24h:pnl_r'
    """
    parts = ["metric", "scanner", scanner, direction, window, metric_name]
    return _SEPARATOR.join(parts)


def build_case_id(entity_type: str, entity_id: str) -> str:
    """Build a deterministic case evidence ID.

    Example
    -------
    >>> build_case_id("trade", "12345")
    'case:trade:12345'
    """
    parts = ["case", entity_type, str(entity_id)]
    return _SEPARATOR.join(parts)


def build_quality_id(check_name: str) -> str:
    """Build a deterministic quality-check evidence ID.

    Example
    -------
    >>> build_quality_id("symbol_coverage")
    'quality:symbol_coverage'
    """
    parts = ["quality", check_name]
    return _SEPARATOR.join(parts)


# ---------------------------------------------------------------------------
# Evidence catalog
# ---------------------------------------------------------------------------


class EvidenceCatalog:
    """Registry of valid evidence IDs for a single analysis run.

    Parameters
    ----------
    ids:
        The full set of valid evidence IDs (metric, case, quality, …).

    Example
    -------
    >>> cat = EvidenceCatalog(["metric:btc:LONG:24h:pnl_r", "quality:coverage"])
    >>> cat.validate("metric:btc:LONG:24h:pnl_r")
    True
    >>> cat.validate_refs(["metric:btc:LONG:24h:pnl_r", "metric:NOPE:bad"])
    ['metric:NOPE:bad']
    """

    def __init__(self, ids: List[str]) -> None:
        self._valid: Set[str] = set(ids)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        """Number of registered evidence IDs."""
        return len(self._valid)

    def validate(self, evidence_id: str) -> bool:
        """Return ``True`` if *evidence_id* exists in the catalog."""
        return evidence_id in self._valid

    def validate_refs(self, refs: List[str]) -> List[str]:
        """Return a list of *invalid* evidence references.

        An empty list means **all** references are valid.
        """
        return [ref for ref in refs if ref not in self._valid]
