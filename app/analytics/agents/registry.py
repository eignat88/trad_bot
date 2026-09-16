"""Specialist agent registry for Stage 3.

Defines the mapping between agent names, input assemblers, prompt builders,
and default model configuration. The registry is the single source of truth
for how each specialist is assembled and invoked.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol


class SpecialistInputAssembler(Protocol):
    """Protocol for specialist input assemblers."""
    def assemble(self, **kwargs: Any) -> Any: ...


class PromptBuilder(Protocol):
    """Protocol for prompt builders."""
    def __call__(self, specialist_input: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SpecialistRegistration:
    """Registration entry for a specialist agent."""
    agent_name: str
    assembler_class: type
    prompt_builder: Callable[..., dict[str, Any]]
    default_model: Optional[str] = None
    enabled: bool = True
    description: str = ""


def _get_funnel_assembler_class() -> type:
    from app.analytics.agents.input.funnel import FunnelPerformanceInputAssembler
    return FunnelPerformanceInputAssembler


def _get_execution_assembler_class() -> type:
    from app.analytics.agents.input.execution import ExecutionQualityInputAssembler
    return ExecutionQualityInputAssembler


def _get_drift_assembler_class() -> type:
    from app.analytics.agents.input.drift import DriftAnomalyInputAssembler
    return DriftAnomalyInputAssembler


def _get_funnel_prompt(specialist_input: Any) -> dict[str, Any]:
    from app.analytics.agents.prompts.funnel_performance_v1 import build_funnel_prompt
    return build_funnel_prompt(specialist_input)


def _get_execution_prompt(specialist_input: Any) -> dict[str, Any]:
    from app.analytics.agents.prompts.execution_quality_v1 import build_execution_prompt
    return build_execution_prompt(specialist_input)


def _get_drift_prompt(specialist_input: Any) -> dict[str, Any]:
    from app.analytics.agents.prompts.drift_anomaly_v1 import build_drift_prompt
    return build_drift_prompt(specialist_input)


# ── Registry ───────────────────────────────────────────────────────────

SPECIALIST_REGISTRY: dict[str, SpecialistRegistration] = {
    "FUNNEL_AND_PERFORMANCE": SpecialistRegistration(
        agent_name="FUNNEL_AND_PERFORMANCE",
        assembler_class=_get_funnel_assembler_class,
        prompt_builder=_get_funnel_prompt,
        default_model=None,  # configured via agent_definition.model
        enabled=True,
        description="Analyzes signal funnel and trade performance across scanners/directions",
    ),
    "EXECUTION_QUALITY": SpecialistRegistration(
        agent_name="EXECUTION_QUALITY",
        assembler_class=_get_execution_assembler_class,
        prompt_builder=_get_execution_prompt,
        default_model=None,
        enabled=True,
        description="Analyzes entry/DCA/stop/exit execution quality",
    ),
    "DRIFT_AND_ANOMALY": SpecialistRegistration(
        agent_name="DRIFT_AND_ANOMALY",
        assembler_class=_get_drift_assembler_class,
        prompt_builder=_get_drift_prompt,
        default_model=None,
        enabled=True,
        description="Detects behavioral drift and anomalies relative to baseline",
    ),
}


def get_specialist_registration(agent_name: str) -> Optional[SpecialistRegistration]:
    """Look up a specialist by name."""
    return SPECIALIST_REGISTRY.get(agent_name)


def list_enabled_specialists() -> list[SpecialistRegistration]:
    """Return all enabled specialist registrations."""
    return [r for r in SPECIALIST_REGISTRY.values() if r.enabled]
