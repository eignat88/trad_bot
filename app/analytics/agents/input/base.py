"""Base protocol for specialist input assemblers."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol
from uuid import UUID


@dataclass
class SpecialistInput:
    """Assembled input for a specialist agent."""
    agent_name: str
    run_id: UUID
    dataset_version: str
    analysis_window_from: str
    analysis_window_to: str
    maturity: str
    quality_status: str
    limitations: list[str]
    sample_sizes: dict[str, int]
    metrics: dict[str, Any]
    segments: list[dict[str, Any]]
    cases: list[dict[str, Any]]
    evidence_ids: list[str]
    evidence_catalog: list[dict[str, Any]]


class SpecialistInputAssembler(Protocol):
    """Protocol for specialist input assemblers."""
    
    def assemble(
        self,
        *,
        run_id: UUID,
        dataset_version: str,
        maturity: str,
        quality_status: str,
        limitations: list[str],
        analysis_window_from: str,
        analysis_window_to: str,
    ) -> SpecialistInput: ...
