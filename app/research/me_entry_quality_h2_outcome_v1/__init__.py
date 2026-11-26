"""Issue #298 / #312 — H2 delayed-entry & paired OUTCOME model (D-1..D-12).

``me_entry_quality_h2_outcome_v1`` is a SELF-CONTAINED, PURE research
package: no database, no network, no filesystem, no clock, no SQL, no
systemd, no production wiring of any kind.  It is the executable form of
``docs/research/312_h2_entry_outcome_policy_v1.md`` §3 — a PREPARED model
whose every open decision stays an explicit owner decision.

What it does:

* :class:`~app.research.me_entry_quality_h2_outcome_v1.policy.OutcomePolicy`
  — all twelve D-1..D-12 decisions as named parameters, each with the
  three states ``PENDING_OWNER_DECISION`` (default) /
  ``PRELIMINARY_CANDIDATE_NOT_FROZEN`` / ``FROZEN`` (only via an explicit
  approved fingerprint);
* an executable delayed-entry model (D-1/D-2/D-4): entry only at an
  admissible price class STRICTLY AFTER the confirmation instant;
  ``ENTRY_EXPIRED`` at the declared deadline; ``MISSED_ENTRY`` for
  gap-through / lock-limit / no-liquidity — every event stays in the
  denominator accounting (D-8);
* a paired outcome model (D-5..D-11): baseline at the ORIGINAL signal
  instant with the ORIGINAL price basis vs the delayed confirmation entry,
  under the SAME exit / cost / geometry conventions, with MAE/MFE
  conventions explicitly labeled and missed-winner accounting;
* honest refusal: WITHOUT a fully specified policy AND an approved
  (frozen) fingerprint the model REFUSES to emit any yield / Net E[R] /
  win-rate / MAE-MFE aggregate — the result status is
  ``NOT_READY_PENDING_DECISION``, never a number (the ``UNDETERMINED_NO_
  EXECUTION_MODEL`` precedent of the merged H2 engine).

Hard invariants (tested):

* the confirmation-candle close is NEVER executable — it is carried only
  as ``CONFIRMATION_CANDLE_CLOSE_REFERENCE_ONLY_NOT_EXECUTABLE``;
* closed-candle-only path evaluation, ASOF strict at microsecond
  precision, no look-ahead, no repainting, prefix-stable, deterministic;
* idempotent per (``source_event_key``, ``variant_id``,
  ``policy_fingerprint``).

Status (unchanged by this package): the H2 track is
``IMPLEMENTATION_MERGED / ACTIVATION_HOLD / OUTCOME_MODEL_BLOCKED_PENDING_
DECISION`` (issue #298).  This package enables NO accumulation, NO
activation and NO production readiness claim, and no metric computed here
may be published.
"""
from __future__ import annotations

from app.research.me_entry_quality_h2_outcome_v1.aggregate import (
    AcceptanceRuleOrderError,
    OutcomeConflictError,
    OutcomeSet,
)
from app.research.me_entry_quality_h2_outcome_v1.costs import (
    CostModel,
    CostModelError,
)
from app.research.me_entry_quality_h2_outcome_v1.entry_model import (
    EntryResolution,
    resolve_entry,
)
from app.research.me_entry_quality_h2_outcome_v1.exit_model import (
    Geometry,
    GeometryError,
    build_geometry,
    simulate_leg,
)
from app.research.me_entry_quality_h2_outcome_v1.paired import evaluate_pair
from app.research.me_entry_quality_h2_outcome_v1.policy import (
    BaselineSpec,
    Decision,
    DenominatorSpec,
    EntryDeadline,
    EntrySpec,
    ExitRule,
    FillPolicy,
    FreezeRecord,
    MaeMfeSpec,
    MissedWinnerRule,
    OutcomePolicy,
    OutcomePolicyError,
    SlTpGeometryPolicy,
    StatisticalAcceptanceRule,
)
from app.research.me_entry_quality_h2_outcome_v1.types import (
    AggregateOutcome,
    CandleBar,
    ConfirmationInfo,
    EventPairOutcome,
    LegOutcome,
    SourceEventInput,
    TradePrint,
)

H2_OUTCOME_PACKAGE_VERSION = "H2_OUTCOME_MODEL_1.0.0"

__all__ = [
    "H2_OUTCOME_PACKAGE_VERSION",
    # policy / decisions
    "OutcomePolicy",
    "OutcomePolicyError",
    "Decision",
    "EntrySpec",
    "FillPolicy",
    "CostModel",
    "CostModelError",
    "EntryDeadline",
    "SlTpGeometryPolicy",
    "ExitRule",
    "BaselineSpec",
    "DenominatorSpec",
    "MaeMfeSpec",
    "MissedWinnerRule",
    "StatisticalAcceptanceRule",
    "FreezeRecord",
    # inputs
    "CandleBar",
    "TradePrint",
    "ConfirmationInfo",
    "SourceEventInput",
    # evaluation
    "EntryResolution",
    "resolve_entry",
    "Geometry",
    "GeometryError",
    "build_geometry",
    "simulate_leg",
    "evaluate_pair",
    # results / aggregates
    "LegOutcome",
    "EventPairOutcome",
    "AggregateOutcome",
    "OutcomeSet",
    "OutcomeConflictError",
    "AcceptanceRuleOrderError",
]
