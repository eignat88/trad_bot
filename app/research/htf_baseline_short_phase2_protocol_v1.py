"""Issue #176 Phase-2 frozen-protocol candidate (Blocker B1).

Machine-readable, deterministic specification for the prospective observe-only
studies:

    HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1
    HTF_BASELINE_SHORT_CAUSAL_EPISODE_STATEFUL_OBSERVER_V1

Status: PROTOCOL_NOT_FROZEN. This module is a *candidate* protocol. It becomes
frozen only through explicit operator approval, after which the SHA-256
fingerprint below is recorded immutably together with the approving commit.

Every field that requires an operator decision is marked
``OPERATOR_APPROVAL_REQUIRED`` and carries a PROPOSED (unapproved) default.
No value in this file was derived from retrospective PnL optimization; such
optimization is explicitly prohibited by the protocol itself.

Nothing in this module mutates state, touches the network, or activates an
experiment.
"""
from __future__ import annotations

from hashlib import sha256
import json

PROTOCOL_SCHEMA = "HTF_PHASE2_PROTOCOL_V1"
PROTOCOL_STATUS = "PROTOCOL_NOT_FROZEN"
OPERATOR_APPROVAL_REQUIRED = "OPERATOR_APPROVAL_REQUIRED"

#: Fixed identity of the prospective experiment. Must never be reused from the
#: Point-B experiments (HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE,
#: HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE) and must never share their
#: freeze boundary.
EXPERIMENT_ID = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"
STUDY_FAMILY = "HTF_BASELINE_SHORT_CAUSAL_EPISODE_STATEFUL_OBSERVER_V1"
DIRECTION = "SHORT"

#: Historical research base commit this protocol was drafted against.
BASE_COMMIT = "4ef60d1e6bb7d2fbbd7fe2b4e3de0591995dc5ba"

PROTOCOL_V1: dict = {
    "schema": PROTOCOL_SCHEMA,
    "status": PROTOCOL_STATUS,
    "issue": 176,
    "predecessor_issue": 174,
    "base_commit": BASE_COMMIT,
    "experiment_id": EXPERIMENT_ID,
    "study_family": STUDY_FAMILY,
    "direction": DIRECTION,
    "study_type": "PROSPECTIVE_OBSERVE_ONLY",
    "hypothesis": (
        "After a causal closed-1H key resistance level forms and price first "
        "touches it, the FIRST strictly-causal SHORT reaction confirmation on "
        "closed 5m bars carries measurable forward behavior that differs from "
        "the unconditional baseline. Phase 2 measures occurrence and forward "
        "behavior; it does NOT establish an executable edge."
    ),
    "timeframes": {
        "level_timeframe": "1H",
        "decision_timeframe": "5M",
        "time_base": "EPOCH_MILLISECONDS_UTC",
        "close_time_convention": "close_time = open_time + duration; a bar is closed iff close_time <= decision_asof",
    },
    "causal_contract": {
        "closed_5m_required": True,
        "closed_1h_required": True,
        "forming_candle_consumption": "FORBIDDEN",
        "decision_time": "close of the latest closed 5m bar",
        "data_used_at_decision_T": "only facts knowable at or before T",
        "future_candles": "FORBIDDEN",
        "backdating": "FORBIDDEN",
        "5m_sequence": {
            "alignment_ms": 300000,
            "contiguity": "REQUIRED",
            "duplicates": "REJECT",
            "out_of_order": "REJECT",
            "conflicting_candles": "REJECT",
            "stale_prefix": "REJECT (last closed 5m must close at decision time)",
        },
        "1h_aggregation": {
            "rule": "an hour is usable iff it contains exactly 12 contiguous closed 5m bars and is closed at the decision time",
            "partial_or_missing_hours": "EXCLUDED_AND_REPORTED",
            "provider_1h_accepted": True,
            "provider_1h_must_be_closed": True,
        },
    },
    "level_definition": {
        "kind": "CAUSAL_RESISTANCE_FOR_SHORT",
        "swing_rule": "strict local maximum of closed-1H highs with SWING_LEFT=4 and SWING_RIGHT=4 closed bars on each side",
        "tied_plateau": "a maximal run of equal highs collapses to exactly one representative level minted at the plateau's first bar (P2-07)",
        "independent_same_price_swings": "SEPARATE_LEVELS (P2-11); an ineligible earlier candidate never suppresses a later eligible one",
        "formation_time": "close time of the plateau's first bar",
        "available_at": "close time of the right-confirmation bar (formation + 4 closed hours); a level is causally unavailable before available_at",
        "touch_count_basis": "closed 1H bars strictly earlier than the formation bar",
        "touch_tolerance": "max(price * 0.003, decision_atr * 0.25) — ATR-normalized at the DECISION bar (unified contract; fixes fallback-ATR defect)",
        "min_touches": {"value_source": "frozen constant KEY_LEVEL_MIN_TOUCHES (Point-B), imported read-only"},
        "reference_price": "last closed 5m close at the decision time (SHORT resistance must sit above reference)",
        "eligibility_at_decision": "available_at <= decision_time AND price > reference_price AND touch-count satisfied",
        "level_identity": "sha256(symbol|direction|formation_close_ms|available_ms|price:.12g) — content-anchored, independent of any rolling window",
    },
    "episode_identity": {
        "rule": "sha256(experiment_id|symbol|direction|level_id|first_touch_open_ms)",
        "forbidden_in_identity": [
            "mutable run counters", "checkpoint generation", "restart counts",
            "rolling-window index", "wall-clock time",
        ],
        "stable_across": ["restart", "warmup length", "rolling-window shift"],
    },
    "event_identity": {
        "unit": "FIRST_CONFIRM of one episode",
        "source_event_id": "episode_id + ':FIRST_CONFIRM'",
        "at_most_one_per_episode": True,
    },
    "lifecycle": {
        "states": ["CONTACT", "CONFIRMED", "UNRESOLVED"],
        "contact_run": "consecutive closed 5m bars touching the level; run end on first non-contact bar (structural, no timer)",
        "recontact": "a new touch after the run ended supersedes the pending episode to UNRESOLVED/RECONTACT_SUPERSEDED_PENDING and starts a new episode (P2-10)",
        "same_step_supersession": "a same-bar recontact supersedes the pending episode and REJECTS the same-bar confirmation (never emitted, never attributed) (P2-10)",
        "confirmation_on_touching_bar": "allowed only while the contact run is active (continuation) (P2-06/P2-09)",
        "pending_after_contact_end": "ALLOWED; confirmation may arrive later (FIRST_CONFIRM)",
        "no_timeout": True,
        "terminal_reasons": ["CAUSAL_REACTION_CONFIRMED", "RECONTACT_SUPERSEDED_PENDING", "SOURCE_WINDOW_EVICTED"],
        "terminal_immutability": "terminal episodes are never re-attributed; post-terminal confirmation raises",
        "level_retirement_policy": {
            "rule": "LEVEL_ELIGIBILITY_LOSS_IS_NON_TERMINAL",
            "detail": "eligibility loss never deletes pending and never retires a level; it is recorded diagnostically only",
            "status": OPERATOR_APPROVAL_REQUIRED,
            "alternative_requires_new_protocol_version": True,
        },
        "multi_level_same_bar": "each level advances independently; multiple same-bar emissions allowed; deterministic sorted level ordering",
        "atomicity": "each 5m step is all-or-nothing; a failed step leaves state unchanged",
    },
    "first_confirm": {
        "definition": "the first strictly-causal reaction end (frozen _find_reaction_end semantics) that completes exactly at the latest closed 5m bar of a pending episode",
        "known_at_ms": "close time of the confirming 5m bar",
        "never_backdated": True,
        "never_backfilled": True,
    },
    "freeze_boundary": {
        "freeze_sources_must_agree": ["protocol", "registry", "database"],
        "signal_time_rule": "eligible iff signal_time > freeze_ts (strict)",
        "first_touch_rule": "eligible iff first_touch_time > freeze_ts (strict)",
        "warmup_rule": "episodes originating in warmup are NOT eligible",
        "restart_rule": "episodes with first_touch_time <= freeze_ts are NOT eligible even if confirmed later",
        "mismatch_or_missing_freeze": "REJECT_FAIL_CLOSED",
        "historical_events_as_oos": "FORBIDDEN",
    },
    "geometry": {
        "proximity_ms": {"value": 300000, "status": OPERATOR_APPROVAL_REQUIRED},
        "price_bps": {"value": 20, "status": OPERATOR_APPROVAL_REQUIRED},
        "boundaries": "inclusive provisional baseline; boundary sensitivity (0/10/20/30/40 bps) must be approved before freeze",
        "ambiguity": "multiple prior anchors fail closed without merge (AMBIGUOUS_PRIOR_ANCHORS)",
    },
    "maturity_gates": {
        "minimum_confirmations": {"value": 30, "status": OPERATOR_APPROVAL_REQUIRED},
        "minimum_symbols": {"value": 5, "status": OPERATOR_APPROVAL_REQUIRED},
        "minimum_active_days": {"value": 14, "status": OPERATOR_APPROVAL_REQUIRED},
        "readout_before_maturity": "DIAGNOSTIC_ONLY",
    },
    "metrics": {
        "observability": [
            "emitted_first_confirmations", "duplicate_suppressions", "coverage_gaps",
            "checkpoint_writes", "heartbeat", "pending_episode_count",
        ],
        "readout": [
            "confirmation counts by symbol", "forward MFE/MAE-style path statistics "
            "(observation only)", "episode funnel (contacts -> pending -> confirm -> unresolved)",
        ],
        "execution_expectancy": "OUT_OF_SCOPE_ISSUE_170",
        "transaction_costs": "OUT_OF_SCOPE_UNLESS_AND_UNTIL_TRADE_EXPECTANCY_IS_EVALUATED",
    },
    "parameter_optimization": {
        "retrospective_pnl_tuning": "PROHIBITED",
        "parameter_changes_after_freeze": "REQUIRE_NEW_EXPERIMENT_VERSION_AND_NEW_FREEZE",
    },
    "automation": {
        "phase2_start": "PROHIBITED_WITHOUT_OPERATOR_APPROVAL",
        "observer_enabled_default": False,
        "activation_in_migration": "PROHIBITED",
    },
}


def canonical_protocol_json(protocol: dict | None = None) -> str:
    """Deterministic canonical serialization used for the fingerprint."""
    data = PROTOCOL_V1 if protocol is None else protocol
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def protocol_fingerprint(protocol: dict | None = None) -> str:
    """SHA-256 fingerprint of the canonical protocol JSON."""
    return sha256(canonical_protocol_json(protocol).encode("utf-8")).hexdigest()


def operator_decisions_pending(protocol: dict | None = None) -> list[str]:
    """Return dotted paths of fields still awaiting operator approval."""
    data = PROTOCOL_V1 if protocol is None else protocol
    pending: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            if node.get("status") == OPERATOR_APPROVAL_REQUIRED:
                pending.append(path)
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(data, "")
    return sorted(pending)
