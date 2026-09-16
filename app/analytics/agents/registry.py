"""Specialist agent registry for Stage 3.

Defines the mapping between agent names, input assemblers, prompt builders,
and default model configuration. The registry is the single source of truth
for how each specialist is assembled and invoked.

Registry uses **actual class/function references** — not getter-factory
wrappers — so callers use dataclass attribute access directly.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

# ── Direct imports (no lazy getter wrappers) ──────────────────────────────
from app.analytics.agents.input.funnel import FunnelPerformanceInputAssembler
from app.analytics.agents.input.execution import ExecutionQualityInputAssembler
from app.analytics.agents.input.drift import DriftAnomalyInputAssembler
from app.analytics.agents.prompts.funnel_performance_v1 import build_funnel_prompt
from app.analytics.agents.prompts.execution_quality_v1 import build_execution_prompt
from app.analytics.agents.prompts.drift_anomaly_v1 import build_drift_prompt


class SpecialistInputAssembler(Protocol):
    """Protocol for specialist input assemblers."""
    def assemble(self, **kwargs: Any) -> Any: ...


class PromptBuilder(Protocol):
    """Protocol for prompt builders."""
    def __call__(self, specialist_input: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SpecialistRegistration:
    """Registration entry for a specialist agent.

    ``assembler_class`` is the actual class (not a factory/function).
    ``prompt_builder`` is the actual callable (not a wrapper).
    """
    agent_name: str
    assembler_class: type
    prompt_builder: Callable[..., dict[str, Any]]
    default_model: Optional[str] = None
    enabled: bool = True
    description: str = ""


# ── Registry ───────────────────────────────────────────────────────────

SPECIALIST_REGISTRY: dict[str, SpecialistRegistration] = {
    "FUNNEL_AND_PERFORMANCE": SpecialistRegistration(
        agent_name="FUNNEL_AND_PERFORMANCE",
        assembler_class=FunnelPerformanceInputAssembler,
        prompt_builder=build_funnel_prompt,
        default_model=None,  # configured via agent_definition.model
        enabled=True,
        description="Analyzes signal funnel and trade performance across scanners/directions",
    ),
    "EXECUTION_QUALITY": SpecialistRegistration(
        agent_name="EXECUTION_QUALITY",
        assembler_class=ExecutionQualityInputAssembler,
        prompt_builder=build_execution_prompt,
        default_model=None,
        enabled=True,
        description="Analyzes entry/DCA/stop/exit execution quality",
    ),
    "DRIFT_AND_ANOMALY": SpecialistRegistration(
        agent_name="DRIFT_AND_ANOMALY",
        assembler_class=DriftAnomalyInputAssembler,
        prompt_builder=build_drift_prompt,
        default_model=None,
        enabled=True,
        description="Detects behavioral drift and anomalies relative to baseline",
    ),
}
