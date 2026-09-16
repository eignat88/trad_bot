"""Stage 3 Agent Foundation — data models.

Immutable by convention: AgentInputManifest and AgentResult must not be mutated
after creation. The repository enforces this at the DB level via triggers.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AgentType(enum.Enum):
    """Type of agent: specialist (single domain) or chief (orchestrator)."""
    SPECIALIST = "SPECIALIST"
    CHIEF = "CHIEF"


class AgentRunStatus(enum.Enum):
    """Lifecycle status of an individual agent run."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ValidationStatus(enum.Enum):
    """Validation outcome for agent output artifacts."""
    PENDING = "PENDING"
    VALID = "VALID"
    INVALID = "INVALID"
    REPAIR_ATTEMPTED = "REPAIR_ATTEMPTED"


class ActionClass(enum.Enum):
    """Recommended action class emitted by the chief agent."""
    NO_ACTION = "NO_ACTION"
    MONITOR = "MONITOR"
    INVESTIGATE = "INVESTIGATE"
    BACKTEST = "BACKTEST"
    DATA_FIX = "DATA_FIX"
    PRODUCTION_INCIDENT = "PRODUCTION_INCIDENT"


class ReportStatus(enum.Enum):
    """Publication lifecycle of the daily trading report."""
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    PUBLISHED = "PUBLISHED"
    SUPERSEDED = "SUPERSEDED"


class ConfidenceLevel(enum.Enum):
    """Confidence tier used inside findings and recommendations."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class DataQualityStatus(enum.Enum):
    """Data quality gate status passed into agent manifests."""
    PASS = "PASS"
    DEGRADED = "DEGRADED"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class AgentDefinition:
    """Registered agent metadata — one row per agent type.

    Immutable configuration reference that maps agent names to their
    contract/prompt versions and activation state.
    """
    agent_name: str = ""
    agent_type: AgentType = AgentType.SPECIALIST
    contract_version: str = "v1"
    prompt_version: str = "v1"
    model: Optional[str] = None  # LLM model identifier (separate from agent_name)
    enabled: bool = True
    description: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        """Validate required fields."""
        if not self.agent_name:
            raise ValueError("agent_name is required")


@dataclass
class AgentRun:
    """One execution attempt of an agent within an analysis run.

    Tracks timing, token usage, and error diagnostics.  The ``attempt``
    counter increments on automatic retries so that each ``AgentRun``
    row is unique.
    """
    agent_run_id: UUID = field(default_factory=uuid4)
    analysis_run_id: UUID = field(default_factory=uuid4)
    agent_name: str = ""
    attempt: int = 1
    model: Optional[str] = None
    status: AgentRunStatus = AgentRunStatus.PENDING
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    latency_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    error_class: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        """Validate required fields and reject secret leakage."""
        if not self.agent_name:
            raise ValueError("agent_name is required")

        if self.error_message:
            lower = self.error_message.lower()
            if 'password' in lower or 'secret' in lower or 'token' in lower:
                raise ValueError("error_message must not contain secrets")


@dataclass
class AgentInputManifest:
    """Deterministic input snapshot delivered to an agent before execution.

    Immutable by convention — once created the manifest must not be mutated.
    The ``input_hash`` provides an integrity fingerprint so downstream
    consumers can verify that the agent received the expected data.
    """
    agent_run_id: UUID = field(default_factory=uuid4)
    dataset_version: str = ""
    input_hash: str = ""
    schema_version: str = "v1"
    analysis_window_from: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    analysis_window_to: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    maturity: str = "PROVISIONAL"
    data_quality_status: str = "PASS"
    limitations: list[str] = field(default_factory=list)
    sample_sizes: dict[str, int] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    segments: list[dict[str, Any]] = field(default_factory=list)
    cases: list[dict[str, Any]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    manifest_json: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AgentResult:
    """Structured output produced by an agent after execution.

    Immutable by convention — the ``result_hash`` is computed from
    ``result_json`` and must remain consistent.
    """
    agent_run_id: UUID = field(default_factory=uuid4)
    schema_version: str = "v1"
    result_json: dict[str, Any] = field(default_factory=dict)
    result_hash: str = ""
    validation_status: ValidationStatus = ValidationStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DailyTradingReport:
    """End-of-day trading report assembled by the chief agent.

    Aggregates findings, hypotheses, and proposed experiments from all
    specialist agents.  The ``partial`` flag indicates the report was
    assembled with missing agent outputs (``missing_agents`` lists who
    did not complete).
    """
    report_id: UUID = field(default_factory=uuid4)
    analysis_run_id: UUID = field(default_factory=uuid4)
    report_version: int = 1
    maturity: str = "PROVISIONAL"
    executive_summary: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    proposed_experiments: list[dict[str, Any]] = field(default_factory=list)
    action_class: ActionClass = ActionClass.NO_ACTION
    limitations: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    partial: bool = False
    missing_agents: list[str] = field(default_factory=list)
    status: ReportStatus = ReportStatus.DRAFT
    agent_run_ids: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
