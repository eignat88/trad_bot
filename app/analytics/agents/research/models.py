"""Research foundation domain models for Stage 4 — migration 035 schema.

12 tables in ``research`` schema, DOC4-aligned enums and dataclasses.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4


# ======================================================================
# Enums
# ======================================================================

class FindingStatus(enum.Enum):
    OPEN = "OPEN"
    REPEATED = "REPEATED"
    RESEARCH_REQUIRED = "RESEARCH_REQUIRED"
    CLOSED = "CLOSED"


class FindingType(enum.Enum):
    ENTRY = "ENTRY"
    DCA = "DCA"
    STOP = "STOP"
    EXIT = "EXIT"
    FUNNEL = "FUNNEL"
    DRIFT = "DRIFT"
    DATA = "DATA"
    INCIDENT = "INCIDENT"


class FindingSeverity(enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class HypothesisStatus(enum.Enum):
    DRAFT = "DRAFT"
    RESEARCH_REQUIRED = "RESEARCH_REQUIRED"
    EXPERIMENT_DESIGNED = "EXPERIMENT_DESIGNED"
    BACKTESTING = "BACKTESTING"
    OOS_VALIDATION = "OOS_VALIDATION"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ExperimentStatus(enum.Enum):
    PROPOSED = "PROPOSED"
    FROZEN = "FROZEN"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ExperimentRunStatus(enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ValidationSplit(enum.Enum):
    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"
    OOS = "OOS"
    ROBUSTNESS = "ROBUSTNESS"


class ValidationVerdict(enum.Enum):
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ChangeCandidateStatus(enum.Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    IMPLEMENTED = "IMPLEMENTED"


class MonitoringVerdict(enum.Enum):
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class EntityType(enum.Enum):
    finding = "finding"
    hypothesis = "hypothesis"
    experiment = "experiment"
    experiment_run = "experiment_run"
    validation_result = "validation_result"
    change_candidate = "change_candidate"
    production_change = "production_change"
    monitoring_result = "monitoring_result"


# ======================================================================
# Dataclasses
# ======================================================================

@dataclass
class Finding:
    """research.finding — validated observations from Stage 3 agents."""
    finding_id: UUID = field(default_factory=uuid4)
    finding_type: str = "ENTRY"
    title: str = ""
    fingerprint: Optional[str] = None  # nullable in PR1, populated by PR2
    scope_json: dict = field(default_factory=dict)
    first_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    occurrence_count: int = 1
    status: str = "OPEN"
    confidence: str = "LOW"
    evidence_summary: list = field(default_factory=list)
    source_run_id: Optional[UUID] = None
    agent_name: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class FindingOccurrence:
    """research.finding_occurrence — each observed instance of a finding."""
    occurrence_id: UUID = field(default_factory=uuid4)
    finding_id: UUID = field(default_factory=uuid4)
    analysis_run_id: Optional[UUID] = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metric_value: Optional[float] = None
    sample_size: int = 0
    confidence: Optional[str] = None
    evidence_refs: list = field(default_factory=list)
    dataset_version: Optional[str] = None
    details_json: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Hypothesis:
    """research.hypothesis — linked to findings via junction table."""
    hypothesis_id: UUID = field(default_factory=uuid4)
    statement: str = ""
    falsification_criterion: str = ""
    population_json: dict = field(default_factory=dict)
    intervention_json: dict = field(default_factory=dict)
    baseline_json: dict = field(default_factory=dict)
    primary_metric: str = ""
    guardrails_json: dict = field(default_factory=dict)
    minimum_sample: int = 0
    status: str = "DRAFT"
    version: int = 1
    source_run_id: Optional[UUID] = None
    agent_name: str = ""
    evidence_refs: list = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class HypothesisFinding:
    """research.hypothesis_finding — many-to-many junction."""
    hypothesis_id: UUID = field(default_factory=uuid4)
    finding_id: UUID = field(default_factory=uuid4)


@dataclass
class Experiment:
    """research.experiment — proposed and tracked experiments."""
    experiment_id: UUID = field(default_factory=uuid4)
    hypothesis_id: Optional[UUID] = None
    title: str = ""
    description: str = ""
    protocol_version: str = "1.0"
    protocol_json: dict = field(default_factory=dict)
    frozen_at: Optional[datetime] = None
    status: str = "PROPOSED"
    source_run_id: Optional[UUID] = None
    agent_name: str = ""
    evidence_refs: list = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ExperimentRun:
    """research.experiment_run — individual execution of an experiment."""
    experiment_run_id: UUID = field(default_factory=uuid4)
    experiment_id: UUID = field(default_factory=uuid4)
    dataset_version: str = ""
    trad_bot_commit_sha: Optional[str] = None
    backtest_commit_sha: Optional[str] = None
    status: str = "PENDING"
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    artifact_location: Optional[str] = None
    reproducibility_command: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ValidationResult:
    """research.validation_result — per-split validation outcome."""
    validation_result_id: UUID = field(default_factory=uuid4)
    experiment_run_id: UUID = field(default_factory=uuid4)
    split: str = "TRAIN"
    segment_type: Optional[str] = None
    segment_value: Optional[str] = None
    sample_size: int = 0
    primary_metric_value: Optional[float] = None
    metrics_json: dict = field(default_factory=dict)
    verdict: str = "INCONCLUSIVE"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ChangeCandidate:
    """research.change_candidate — proposed production changes."""
    candidate_id: UUID = field(default_factory=uuid4)
    hypothesis_id: Optional[UUID] = None
    experiment_id: Optional[UUID] = None
    validation_result_id: Optional[UUID] = None
    risk_assessment_json: dict = field(default_factory=dict)
    status: str = "PROPOSED"
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ProductionChange:
    """research.production_change — records of deployed changes."""
    change_id: UUID = field(default_factory=uuid4)
    candidate_id: Optional[UUID] = None
    branch: Optional[str] = None
    pr_number: Optional[int] = None
    commit_sha: Optional[str] = None
    deployment_at: Optional[datetime] = None
    config_snapshot_json: dict = field(default_factory=dict)
    affected_scanners_json: list = field(default_factory=list)
    deployed_by: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MonitoringResult:
    """research.monitoring_result — post-deployment monitoring outcomes."""
    monitoring_result_id: UUID = field(default_factory=uuid4)
    change_id: Optional[UUID] = None
    window: str = ""
    observed_from: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    observed_to: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sample_size: int = 0
    primary_metric_actual: Optional[float] = None
    expected_metric: Optional[float] = None
    guardrails_json: dict = field(default_factory=dict)
    verdict: str = "PENDING"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Fingerprint:
    """research.fingerprint — finding fingerprints for deduplication (PR1 schema, PR2 populates)."""
    fingerprint_id: UUID = field(default_factory=uuid4)
    finding_id: UUID = field(default_factory=uuid4)
    fingerprint_hash: str = ""
    finding_type: str = ""
    scanner_name: Optional[str] = None
    direction: Optional[str] = None
    normalized_segment: Optional[str] = None
    metric_name: Optional[str] = None
    comparator: Optional[str] = None
    threshold_policy_version: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TransitionRecord:
    """research.transition_history — append-only audit trail."""
    transition_id: Optional[int] = None
    entity_type: str = ""
    entity_id: Optional[UUID] = None
    from_status: Optional[str] = None
    to_status: str = ""
    actor: str = "system"
    reason: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
