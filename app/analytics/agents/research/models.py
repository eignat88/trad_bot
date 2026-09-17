"""Research foundation domain models for Stage 4."""
from __future__ import annotations
import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4


class FindingStatus(enum.Enum):
    OPEN = "OPEN"
    CONFIRMED = "CONFIRMED"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    SUPERSEDED = "SUPERSEDED"


class FindingSeverity(enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class HypothesisStatus(enum.Enum):
    PROPOSED = "PROPOSED"
    UNDER_TEST = "UNDER_TEST"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class ExperimentStatus(enum.Enum):
    PROPOSED = "PROPOSED"
    FROZEN = "FROZEN"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class EntityType(enum.Enum):
    FINDING = "finding"
    HYPOTHESIS = "hypothesis"
    EXPERIMENT = "experiment"


@dataclass
class Finding:
    finding_id: UUID = field(default_factory=uuid4)
    finding_code: str = ""
    title: str = ""
    severity: str = "LOW"
    status: str = "OPEN"
    source_run_id: Optional[UUID] = None
    agent_name: str = ""
    scope: dict = field(default_factory=dict)
    sample_size: int = 0
    confidence: str = "LOW"
    evidence_refs: list[str] = field(default_factory=list)
    metric_refs: list[str] = field(default_factory=list)
    statement: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Hypothesis:
    hypothesis_id: UUID = field(default_factory=uuid4)
    finding_id: Optional[UUID] = None
    hypothesis_code: str = ""
    statement: str = ""
    falsifiable_experiment: str = ""
    required_data: list[str] = field(default_factory=list)
    validation_criterion: str = ""
    confidence: str = "LOW"
    status: str = "PROPOSED"
    source_run_id: Optional[UUID] = None
    agent_name: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Experiment:
    experiment_id: UUID = field(default_factory=uuid4)
    hypothesis_id: Optional[UUID] = None
    title: str = ""
    description: str = ""
    required_data: list[str] = field(default_factory=list)
    validation_criterion: str = ""
    status: str = "PROPOSED"
    source_run_id: Optional[UUID] = None
    agent_name: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TransitionRecord:
    transition_id: Optional[int] = None
    entity_type: str = ""
    entity_id: Optional[UUID] = None
    from_status: Optional[str] = None
    to_status: str = ""
    actor: str = "system"
    reason: str = ""
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
