"""Deterministic cost accounting for Stage 3 LLM inversions.

Token usage is captured from ModelResponse by the executor.
Cost is computed using a versioned pricing table.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PricingEntry:
    """Pricing for a specific model."""
    model: str
    input_cost_per_1m: float   # USD per 1M input tokens
    output_cost_per_1m: float  # USD per 1M output tokens
    effective_from: str         # ISO date, e.g. "2024-01-01"


# Versioned pricing table — ordered by effective_from, most recent last
PRICING_TABLE: list[PricingEntry] = [
    PricingEntry(model="gpt-4o", input_cost_per_1m=2.50, output_cost_per_1m=10.00, effective_from="2024-05-13"),
    PricingEntry(model="gpt-4o-mini", input_cost_per_1m=0.15, output_cost_per_1m=0.60, effective_from="2024-07-18"),
    PricingEntry(model="claude-sonnet-4-20250514", input_cost_per_1m=3.00, output_cost_per_1m=15.00, effective_from="2025-05-14"),
]


def compute_cost(
    input_tokens: int,
    output_tokens: int,
    model: str,
    execution_date: Optional[str] = None,
) -> Optional[float]:
    """Compute estimated cost in USD for given token counts.
    
    Returns None if model pricing is unknown.
    execution_date: ISO date string (e.g. "2026-09-16"). Uses latest pricing if not provided.
    """
    entry = _find_pricing(model, execution_date)
    if entry is None:
        return None
    
    input_cost = (input_tokens / 1_000_000) * entry.input_cost_per_1m
    output_cost = (output_tokens / 1_000_000) * entry.output_cost_per_1m
    return round(input_cost + output_cost, 6)


def _find_pricing(model: str, execution_date: Optional[str] = None) -> Optional[PricingEntry]:
    """Find the applicable pricing entry for a model at a given date."""
    candidates = [p for p in PRICING_TABLE if p.model == model]
    if not candidates:
        return None
    
    if execution_date is None:
        # Use latest pricing
        return max(candidates, key=lambda p: p.effective_from)
    
    # Use pricing effective before or on execution_date
    applicable = [p for p in candidates if p.effective_from <= execution_date]
    if applicable:
        return max(applicable, key=lambda p: p.effective_from)

    # No applicable pricing — execution was before any known rate
    return None
