"""Issue #324 / H1-E — frozen protocol identity of the prospective observer.

``H1E_PROSPECTIVE_OBSERVER_V1`` — the RESEARCH_ONLY, Phase A (offline)
prospective 4H/1D classification observer over the independent #305
pre-scoring source ``research_me_upstream_coverage.source_event``.

Hard scope guarantees (Issue #324 spec):

* Phase A is OFFLINE ONLY — no deploy, no VPS migration, no live Bybit
  calls, no activation, no backfill, no OOS activation;
* the #305 source and its freeze are NEVER altered; the frozen #305 facts
  below are carried as constants for boundary checks only;
* the H1-E ``freeze_ts`` is a SEPARATE activation, assigned only after
  deployment/readiness and a separate owner GO.  Phase A MUST NOT mint,
  reset or anticipate a real activation freeze
  (:data:`H1E_FREEZE_NOT_MINTED`);
* the historical 2026-11-25 SOXSUSDT event is a QA example only and is
  excluded from every future H1-E prospective denominator.

Changing ANY line of :data:`PROTOCOL_DESCRIPTOR` changes
:func:`protocol_fingerprint` and therefore requires a new ledger identity
namespace — the same freeze/fingerprint contract as #305 and H1-D.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

# ─── protocol identity ───────────────────────────────────────────────────────

EXPERIMENT_ID = "ME_ENTRY_QUALITY_H1E_PROSPECTIVE_OBSERVER_V1"
H1E_PROTOCOL_VERSION = "1.0.0"

#: Ledger identity namespace — deliberately distinct from the #305
#: ``ME_UPSTREAM_SOURCE_V1`` namespace and from every V3/OOS-2 namespace.
IDENTITY_NAMESPACE = "ME_ENTRY_QUALITY_H1E_V1"

#: The ONLY source this observer reads: the independent #305 pre-scoring
#: cohort.  Post-filter ``dds.scanner_setup`` and the V3 event ledger are
#: deliberately out of scope.
SOURCE_SCHEMA = "research_me_upstream_coverage"
SOURCE_TABLE = "research_me_upstream_coverage.source_event"

#: The PROPOSED (not applied) H1-E ledger schema — separate namespace from
#: #305, production and existing OOS schemas.
LEDGER_SCHEMA = "research_me_entry_quality_h1e"

#: Phase status of this code drop.  Phase B/C/D/E require separate owner GO.
PHASE_A_STATUS = "PHASE_A_OFFLINE_IMPLEMENTATION"

PROTOCOL_DESCRIPTOR = (
    "ME_ENTRY_QUALITY_H1E_V1|1.0.0|"
    "source=research_me_upstream_coverage.source_event|"
    "htf=h1c_run_h1_event+h1d_240_D_60x60|"
    "asof=signal_time_us|inclusive_close_boundary|"
    "prospective_only=signal_time>h1e_freeze_ts|no_backfill|"
    "research_only=no_deploy|no_live_bybit|no_oos_activation"
)


def protocol_fingerprint() -> str:
    """Deterministic SHA-256 of the frozen protocol descriptor above."""
    payload = json.dumps(
        {"descriptor": PROTOCOL_DESCRIPTOR},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ─── frozen #305 facts (read-only constants — NEVER altered) ────────────────

#: The #305 activation freeze.  Immutable; H1-E never writes it.
SOURCE305_EXPERIMENT_ID = "ME_INVERSE_SHORT_V3_UPSTREAM_SOURCE_CAPTURE_V1"
SOURCE305_FREEZE_TS_TEXT = "2026-11-24T13:31:53.365952+00:00"

#: #305 explicitly UNVERIFIED capture window (preserved verbatim).  Any
#: event inside it is excluded from every H1-E prospective denominator and
#: rejected with an explicit reason code.
SOURCE305_UNVERIFIED_WINDOW_TEXT: tuple[str, str] = (
    "2026-11-24T13:31:53.365952+00:00",
    "2026-11-25T07:14:41.230516+00:00",
)

#: The historical SOXSUSDT QA example (2026-11-25): a QA fixture only.
SOXSUSDT_QA_SYMBOL = "SOXSUSDT"
SOXSUSDT_QA_EXAMPLE_DATE = "2026-11-25"

#: H1-E freeze placeholder: Phase A has NO approved freeze and must never
#: mint one.  A synthetic fixture freeze is allowed for offline tests only.
H1E_FREEZE_NOT_MINTED = "H1E_FREEZE_NOT_MINTED"


def _parse_utc_us(text: str) -> int:
    """Exact UTC epoch-microsecond parse of an ISO-8601 literal (pure)."""
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp literal is not UTC-anchored: {text!r}")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return int((parsed - epoch).total_seconds() * 1_000_000)


SOURCE305_FREEZE_TS_US = _parse_utc_us(SOURCE305_FREEZE_TS_TEXT)

#: Inclusive ``[start, end]`` µs bounds of the #305 UNVERIFIED window.
SOURCE305_UNVERIFIED_WINDOW_US: tuple[int, int] = (
    _parse_utc_us(SOURCE305_UNVERIFIED_WINDOW_TEXT[0]),
    _parse_utc_us(SOURCE305_UNVERIFIED_WINDOW_TEXT[1]),
)


def excluded_window_reason(signal_time_us: int, symbol: str = "") -> str:
    """Explicit exclusion reason for a signal instant (``""`` = not excluded).

    Two preserved exclusions, both fail-closed:

    * the #305 UNVERIFIED window
      (:data:`SOURCE305_UNVERIFIED_WINDOW_US`, inclusive bounds);
    * the historical SOXSUSDT QA example — any SOXSUSDT event on
      2026-11-25 is a QA fixture and never a prospective observation.
    """
    if not isinstance(signal_time_us, int) or isinstance(signal_time_us, bool):
        return "H1E_REJECT_EXCLUDED_WINDOW_INVALID_TIME"
    start, end = SOURCE305_UNVERIFIED_WINDOW_US
    if start <= signal_time_us <= end:
        return "H1E_REJECT_UNVERIFIED_WINDOW"
    if symbol == SOXSUSDT_QA_SYMBOL:
        day_start = _parse_utc_us(f"{SOXSUSDT_QA_EXAMPLE_DATE}T00:00:00+00:00")
        day_end = day_start + 86_400 * 1_000_000
        if day_start <= signal_time_us < day_end:
            return "H1E_REJECT_SOXSUSDT_QA_EXAMPLE"
    return ""
