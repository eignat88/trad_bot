"""Issue #324 / H1-E.1 — ``H1E_SOURCE_READINESS_V1`` readiness contract.

Pure, read-only readiness report: no database, no network, no clock.  The
caller supplies EVIDENCE values (measurements taken elsewhere — a
read-only query, a limiter probe, a documented decision); this module only
decides, deterministically, what each evidence set proves.

Statuses per check:

* ``PASS``   — positive, auditable evidence for the check;
* ``HOLD``   — evidence exists but is insufficient / a hold is in force;
* ``BLOCK``  — a known violation; the readiness gate must stay closed;
* ``UNKNOWN`` — the check is UNSUPPORTED by the supplied evidence
  (``None`` / absent).  ``UNKNOWN`` NEVER resolves to ``PASS``; it feeds
  the ``HOLD`` side of the overall verdict (spec: "Unsupported checks must
  remain ``UNKNOWN/HOLD``, not become PASS").

Overall verdict: any ``BLOCK`` → ``BLOCK``; else any ``HOLD``/``UNKNOWN``
→ ``HOLD``; else ``PASS``.  Because the H1-E activation freeze cannot be
approved in Phase A, a Phase A report can never be ``PASS`` — that is the
intended fail-closed shape.

Checks cover the spec list: #305 source coverage + #296 lineage
obligations, source fingerprint, known blind intervals, captured event
identity, HTF source / shared limiter availability, and the H1-E freeze
approval itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

H1E_SOURCE_READINESS_VERSION = "H1E_SOURCE_READINESS_V1"

STATUS_PASS = "PASS"
STATUS_HOLD = "HOLD"
STATUS_BLOCK = "BLOCK"
STATUS_UNKNOWN = "UNKNOWN"

VERDICT_PASS = "PASS"
VERDICT_HOLD = "HOLD"
VERDICT_BLOCK = "BLOCK"

# ─── check ids (stable) ─────────────────────────────────────────────────────

CHECK_SRC305_SCHEMA_CONTRACT = "SRC305_SCHEMA_CONTRACT"
CHECK_SRC305_FREEZE_IMMUTABLE = "SRC305_FREEZE_IMMUTABLE"
CHECK_SRC305_COVERAGE_PROOF = "SRC305_COVERAGE_PROOF"
CHECK_SRC296_LINEAGE_OBLIGATIONS = "SRC296_LINEAGE_OBLIGATIONS"
CHECK_SOURCE_FINGERPRINT = "SOURCE_FINGERPRINT"
CHECK_BLIND_INTERVALS = "BLIND_INTERVALS"
CHECK_CAPTURED_EVENT_IDENTITY = "CAPTURED_EVENT_IDENTITY"
CHECK_HTF_SOURCE_AVAILABILITY = "HTF_SOURCE_AVAILABILITY"
CHECK_LIMITER_AVAILABILITY = "LIMITER_AVAILABILITY"
CHECK_H1E_FREEZE_APPROVAL = "H1E_FREEZE_APPROVAL"

#: ``True`` / ``False`` / ``None`` (= unsupported → ``UNKNOWN``).
_TRISTATE_CHECKS: tuple[str, ...] = (
    CHECK_SRC305_SCHEMA_CONTRACT,
    CHECK_SRC305_FREEZE_IMMUTABLE,
    CHECK_SRC296_LINEAGE_OBLIGATIONS,
    CHECK_SOURCE_FINGERPRINT,
    CHECK_HTF_SOURCE_AVAILABILITY,
    CHECK_LIMITER_AVAILABILITY,
)

_CHECK_TITLES: dict[str, str] = {
    CHECK_SRC305_SCHEMA_CONTRACT: (
        "#305 research_me_upstream_coverage.source_event schema contract "
        "matches the repository migration 094"
    ),
    CHECK_SRC305_FREEZE_IMMUTABLE: (
        "#305 freeze_ts immutable (migration 094 trigger) and unaltered"
    ),
    CHECK_SRC305_COVERAGE_PROOF: (
        "#305 source capture coverage (independent completeness of the "
        "pre-scoring capture; #296 UPSTREAM_SOURCE_COVERAGE_UNPROVEN rule)"
    ),
    CHECK_SRC296_LINEAGE_OBLIGATIONS: (
        "#296 lineage obligations: complete lineage, deterministic "
        "reason codes, preserved identity, no rebuild from mutable fields"
    ),
    CHECK_SOURCE_FINGERPRINT: (
        "source pipeline fingerprint present and stable across sampled "
        "events"
    ),
    CHECK_BLIND_INTERVALS: (
        "known blind intervals / UNVERIFIED windows documented and "
        "excluded from prospective denominators"
    ),
    CHECK_CAPTURED_EVENT_IDENTITY: (
        "captured event identity: source_event_key hashes from "
        "source_identity; lineage_hash matches canonical lineage JSON"
    ),
    CHECK_HTF_SOURCE_AVAILABILITY: (
        "HTF 4H (240) / 1D (D) candle source availability at classification "
        "time"
    ),
    CHECK_LIMITER_AVAILABILITY: (
        "shared Bybit limiter / retry budget / circuit breaker availability "
        "for HTF fetches"
    ),
    CHECK_H1E_FREEZE_APPROVAL: (
        "H1-E freeze_ts separately approved by the owner (Phase C GO) — "
        "Phase A MUST NOT mint one"
    ),
}


@dataclass(frozen=True)
class ReadinessCheck:
    """One readiness check with its auditable evidence."""

    check_id: str
    title: str
    status: str
    evidence: Any
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "title": self.title,
            "status": self.status,
            "evidence": self.evidence,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReadinessReport:
    """Deterministic readiness verdict over the supplied evidence."""

    version: str
    checks: tuple[ReadinessCheck, ...]
    verdict: str
    phase_a_hold: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "verdict": self.verdict,
            "phase_a_hold": self.phase_a_hold,
            "checks": [check.to_dict() for check in self.checks],
        }


def _tri_state_check(
    check_id: str, evidence: Any, *, false_detail: str, true_detail: str,
) -> ReadinessCheck:
    title = _CHECK_TITLES[check_id]
    if evidence is None:
        return ReadinessCheck(check_id, title, STATUS_UNKNOWN, None, "unsupported")
    if evidence is True:
        return ReadinessCheck(check_id, title, STATUS_PASS, True, true_detail)
    if evidence is False:
        return ReadinessCheck(check_id, title, STATUS_BLOCK, False, false_detail)
    return ReadinessCheck(
        check_id, title, STATUS_UNKNOWN, evidence, "unsupported evidence value",
    )


def build_readiness_report(evidence: Mapping[str, Any]) -> ReadinessReport:
    """Build the readiness report from caller-supplied evidence (pure).

    Recognised evidence keys (all optional; absent/``None`` = unsupported):

    * ``source_schema_contract_ok``        — ``bool | None``
    * ``source_freeze_immutable_ok``       — ``bool | None``
    * ``source_coverage_proof``            — ``bool | None`` (independent
      upstream completeness proof; #296 explicitly keeps this unproven)
    * ``source296_lineage_obligations_ok`` — ``bool | None``
    * ``source_fingerprint_stable_ok``     — ``bool | None``
    * ``blind_intervals``                  — mapping/sequence of documented
      blind intervals (empty = none documented — a HOLD, never a PASS
      without an explicit ``blind_intervals_documented=True``)
    * ``blind_intervals_documented``       — ``bool | None``
    * ``sampled_identity_check``           — ``"PASS"`` / ``"FAIL"`` /
      ``None`` (sampled source_event_key / lineage_hash re-derivation)
    * ``htf_source_available_ok``          — ``bool | None``
    * ``limiter_available_ok``             — ``bool | None``
    * ``h1e_freeze_approved``              — ``bool | None`` (Phase A: always
      not approved → ``HOLD``)
    """
    checks: list[ReadinessCheck] = []

    checks.append(_tri_state_check(
        CHECK_SRC305_SCHEMA_CONTRACT,
        evidence.get("source_schema_contract_ok"),
        false_detail="schema contract mismatch against migration 094",
        true_detail="schema contract verified against migration 094",
    ))
    checks.append(_tri_state_check(
        CHECK_SRC305_FREEZE_IMMUTABLE,
        evidence.get("source_freeze_immutable_ok"),
        false_detail="#305 freeze altered or rewritable",
        true_detail="#305 freeze immutable and unaltered",
    ))

    # #305 coverage proof — #296 keeps upstream completeness UNPROVEN; a
    # truthful "False" is a HOLD (documented gap), never a silent PASS.
    coverage_proof = evidence.get("source_coverage_proof")
    if coverage_proof is True:
        checks.append(ReadinessCheck(
            CHECK_SRC305_COVERAGE_PROOF, _CHECK_TITLES[CHECK_SRC305_COVERAGE_PROOF],
            STATUS_PASS, True, "independent upstream completeness proven"))
    elif coverage_proof is False:
        checks.append(ReadinessCheck(
            CHECK_SRC305_COVERAGE_PROOF, _CHECK_TITLES[CHECK_SRC305_COVERAGE_PROOF],
            STATUS_HOLD, False,
            "#296 UPSTREAM_SOURCE_COVERAGE_UNPROVEN — explicit blocker-listed"))
    else:
        checks.append(ReadinessCheck(
            CHECK_SRC305_COVERAGE_PROOF, _CHECK_TITLES[CHECK_SRC305_COVERAGE_PROOF],
            STATUS_UNKNOWN, coverage_proof, "unsupported"))

    checks.append(_tri_state_check(
        CHECK_SRC296_LINEAGE_OBLIGATIONS,
        evidence.get("source296_lineage_obligations_ok"),
        false_detail="lineage obligation violated",
        true_detail="lineage obligations verified (identity preserved, "
                    "deterministic reason codes, verbatim lineage)",
    ))
    checks.append(_tri_state_check(
        CHECK_SOURCE_FINGERPRINT,
        evidence.get("source_fingerprint_stable_ok"),
        false_detail="source fingerprint missing or unstable",
        true_detail="source fingerprint present and stable",
    ))

    # Blind intervals — must be explicitly documented; absent evidence is
    # UNKNOWN, an undocumented set is a HOLD.
    blind_documented = evidence.get("blind_intervals_documented")
    blind_intervals = evidence.get("blind_intervals")
    if blind_documented is False:
        blind_status, blind_detail = STATUS_HOLD, "blind intervals not documented"
    elif blind_documented is True:
        blind_status = STATUS_PASS
        blind_detail = "known blind intervals documented and excluded"
    else:
        blind_status, blind_detail = STATUS_UNKNOWN, "unsupported"
    checks.append(ReadinessCheck(
        CHECK_BLIND_INTERVALS, _CHECK_TITLES[CHECK_BLIND_INTERVALS],
        blind_status, blind_intervals, blind_detail))

    identity_check = evidence.get("sampled_identity_check")
    if identity_check == "PASS":
        checks.append(ReadinessCheck(
            CHECK_CAPTURED_EVENT_IDENTITY, _CHECK_TITLES[CHECK_CAPTURED_EVENT_IDENTITY],
            STATUS_PASS, identity_check, "sampled identities re-derived exactly"))
    elif identity_check == "FAIL":
        checks.append(ReadinessCheck(
            CHECK_CAPTURED_EVENT_IDENTITY, _CHECK_TITLES[CHECK_CAPTURED_EVENT_IDENTITY],
            STATUS_BLOCK, identity_check, "identity re-derivation mismatch"))
    else:
        checks.append(ReadinessCheck(
            CHECK_CAPTURED_EVENT_IDENTITY, _CHECK_TITLES[CHECK_CAPTURED_EVENT_IDENTITY],
            STATUS_UNKNOWN, identity_check, "unsupported"))

    checks.append(_tri_state_check(
        CHECK_HTF_SOURCE_AVAILABILITY,
        evidence.get("htf_source_available_ok"),
        false_detail="HTF 240/D source unavailable",
        true_detail="HTF 240/D source available",
    ))
    checks.append(_tri_state_check(
        CHECK_LIMITER_AVAILABILITY,
        evidence.get("limiter_available_ok"),
        false_detail="shared limiter / retry budget / breaker unavailable",
        true_detail="shared limiter / retry budget / breaker available",
    ))

    freeze_approved = evidence.get("h1e_freeze_approved")
    if freeze_approved is True:
        freeze_check = ReadinessCheck(
            CHECK_H1E_FREEZE_APPROVAL, _CHECK_TITLES[CHECK_H1E_FREEZE_APPROVAL],
            STATUS_HOLD, True,
            "freeze approval recorded — still requires the Phase C GO "
            "before any activation")
    elif freeze_approved is False:
        freeze_check = ReadinessCheck(
            CHECK_H1E_FREEZE_APPROVAL, _CHECK_TITLES[CHECK_H1E_FREEZE_APPROVAL],
            STATUS_HOLD, False, "H1E_FREEZE_NOT_MINTED (Phase A)")
    else:
        freeze_check = ReadinessCheck(
            CHECK_H1E_FREEZE_APPROVAL, _CHECK_TITLES[CHECK_H1E_FREEZE_APPROVAL],
            STATUS_UNKNOWN, freeze_approved, "unsupported")
    checks.append(freeze_check)

    statuses = [check.status for check in checks]
    if STATUS_BLOCK in statuses:
        verdict = VERDICT_BLOCK
    elif STATUS_HOLD in statuses or STATUS_UNKNOWN in statuses:
        verdict = VERDICT_HOLD
    else:
        verdict = VERDICT_PASS

    return ReadinessReport(
        version=H1E_SOURCE_READINESS_VERSION,
        checks=tuple(checks),
        verdict=verdict,
        phase_a_hold=True,
    )
