"""Issue #312 — H2_RSI_ONLY Prospective Runtime V1 (research only).

Independent, prospective-only research runtime that feeds the original
#305 upstream source cohort (``research_me_upstream_coverage.source_event``,
READ-ONLY) through the merged H2 RSI-confirmation engine
(:mod:`app.research.me_entry_quality_h2_v1`, PR #311 — reused unchanged) on
fully CLOSED 5m candles, and persists per-variant delayed-entry outcomes into
the dedicated research-only schema
``research_me_entry_quality_h2_prospective`` (migration 095).

Hard guarantees:

* RESEARCH-ONLY — no trading signals, no orders, no scoring, no gates, no
  paper/live, no service control; no change to ``scanner_runner.py``, to any
  production detector or to #305 (strict SELECT-only on its schema);
* DISABLED BY DEFAULT — with the activation flag off the runner performs no
  database connection, no Bybit request and no write of any kind;
* CAUSAL — only candles fully closed at the actual observation instant are
  used; the pure PR #311 engine enforces ASOF isolation and microsecond
  signal-time precision; no historical backfill into the prospective cohort;
* NO FICTIONAL EXECUTION — RSI confirmation is never a trade: entry price
  stays ``UNDETERMINED_NO_EXECUTION_MODEL`` and no Net E[R] / return metric is
  ever published (the execution model is unapproved — see
  ``docs/research/312_h2_entry_outcome_policy_v1.md``);
* FAIL-CLOSED candles — a dedicated, bounded research API budget; without a
  confirmed budget the candle provider stays OFF and the runtime reports
  ``BLOCKED_CANDLE_SOURCE``; missing data never becomes ``EXPIRED`` or
  ``CONFIRMED_DELAYED``.

ACTIVATION HOLD: deploy, merge and activation require a separate explicit GO.
"""
from __future__ import annotations

from .contracts import (
    CANDIDATE_OBSERVATION_WINDOWS,
    H2_PROSPECTIVE_ALGORITHM_VERSION,
    H2_PROSPECTIVE_PIPELINE_VERSION,
    H2_PROSPECTIVE_PROTOCOL_VERSION,
    H2_PROSPECTIVE_RSI_CALC_VERSION,
    H2_PROSPECTIVE_RUNTIME_VERSION,
    LIFECYCLE_STATES,
    TERMINAL_STATES,
    candidate_policies,
)

__all__ = [
    "CANDIDATE_OBSERVATION_WINDOWS",
    "H2_PROSPECTIVE_ALGORITHM_VERSION",
    "H2_PROSPECTIVE_PIPELINE_VERSION",
    "H2_PROSPECTIVE_PROTOCOL_VERSION",
    "H2_PROSPECTIVE_RSI_CALC_VERSION",
    "H2_PROSPECTIVE_RUNTIME_VERSION",
    "LIFECYCLE_STATES",
    "TERMINAL_STATES",
    "candidate_policies",
]
