"""Issue #312 / FR-05 — H2 prospective result store (research only).

Persistence for the H2 prospective runtime in the DEDICATED research-only
schema ``research_me_entry_quality_h2_prospective`` (migration 095).  Two
implementations behind ONE interface:

``InMemoryProspectiveStore``
    Process-local store for unit tests and dry-runs.  Enforces exactly the
    same invariants as the PostgreSQL store.

``PgProspectiveStore``
    PostgreSQL implementation over a DEDICATED connection (never the scanner
    or paper-runner transaction), writing ONLY inside its own schema and
    reading ``research_me_upstream_coverage.source_event`` SELECT-only.

Isolation invariants:

* #305 tables are NEVER written (the source reader is SELECT-only);
* nothing outside ``research_me_entry_quality_h2_prospective.*`` is written;
* ingest is idempotent on ``source_event_key``: the first-seen snapshot is
  preserved VERBATIM and a redelivery never overwrites it;
* a redelivery whose preserved identity / immutable source hash differs is an
  explicit ``EVENT_KEY_LINEAGE_CONFLICT`` recorded in ``result_conflict`` —
  never silently swallowed;
* terminal results are UNIQUE on
  (``source_event_key``, ``variant_id``, ``policy_fingerprint``) and immutable:
  an identical re-write is a no-op, a differing one is an explicit immutable-
  data conflict (``IMMUTABLE_RESULT_CONFLICT``) and never replaces the stored
  outcome;
* terminal outcomes are never recreated by a redelivery (FR-04 / criterion 2).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.research.me_entry_quality_h2_prospective_v1.contracts import (
    INGEST_ACCEPTED,
    INGEST_REJECTED_INVALID,
    INGEST_REJECTED_LINEAGE,
    REASON_EVENT_KEY_LINEAGE_CONFLICT,
    REASON_INVALID_SOURCE_PRICE,
    REASON_OK,
    STATE_PENDING_CONFIRMATION,
    TERMINAL_STATES,
    exact_price_str,
    immutable_result_hash,
    parse_source_price,
)
from app.research.me_entry_quality_h2_prospective_v1.lifecycle import (
    LifecycleDecision,
)

logger = logging.getLogger(__name__)

SCHEMA = "research_me_entry_quality_h2_prospective"
INGEST_TABLE = f"{SCHEMA}.ingest_event"
STATE_TABLE = f"{SCHEMA}.variant_state"
RESULT_TABLE = f"{SCHEMA}.variant_result"
CONFLICT_TABLE = f"{SCHEMA}.result_conflict"
EVALUATION_TABLE = f"{SCHEMA}.evaluation_log"
HEALTH_TABLE = f"{SCHEMA}.runtime_health"
WATERMARK_TABLE = f"{SCHEMA}.reader_watermark"
BUDGET_TABLE = f"{SCHEMA}.api_budget_state"
OVERLAP_TABLE = f"{SCHEMA}.overlap_cursor"


def default_overlap_state() -> dict[str, Any]:
    """Fresh reconciliation-pass state (review P1-overlap)."""
    return {
        "cursor_captured_at_us": 0,
        "cursor_key": "",
        "pass_floor_us": 0,
        "pass_ceiling_us": 0,
        "pass_active": False,
        "passes_completed": 0,
    }

CONFLICT_EVENT_KEY_LINEAGE = "EVENT_KEY_LINEAGE_CONFLICT"
CONFLICT_IMMUTABLE_RESULT = "IMMUTABLE_RESULT_CONFLICT"


@dataclass(frozen=True)
class IngestOutcome:
    """Idempotent ingest result for ONE #305 delivery."""

    status: str
    reason: str = REASON_OK
    detail: str = ""
    conflict: bool = False
    #: True when this exact delivery was already ingested (first-seen
    #: snapshot preserved — the FR-01 idempotent redelivery contract).
    duplicate: bool = False


@dataclass(frozen=True)
class VariantState:
    """Persisted lifecycle state for ONE (event, variant, policy)."""

    source_event_key: str
    variant_id: str
    policy_fingerprint: str
    state: str
    policy: dict[str, Any]
    symbol: str
    signal_time_us: int
    evaluation_attempts: int = 0
    coverage_attempts: int = 0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@dataclass(frozen=True)
class ApplyOutcome:
    """Result of persisting ONE evaluation attempt."""

    state: str
    result_written: bool = False
    conflict: bool = False


class H2ProspectiveStore:
    """Interface of the H2 prospective persistence layer."""

    broken: bool = False

    # ── ingest (#305 deliveries, read-only source) ─────────────────────────
    def ingest_event(self, delivery: Any) -> IngestOutcome:
        raise NotImplementedError

    def get_source_row(self, source_event_key: str) -> Mapping[str, Any] | None:
        """The first-seen VERBATIM source row payload (never overwritten)."""
        raise NotImplementedError

    def ingest_event_count(self) -> int:
        """Number of distinct ingested ``source_event_key`` rows."""
        raise NotImplementedError

    def get_cursor(self) -> tuple[int, str]:
        """Capture-order delivery cursor ``(captured_at_us, source_event_key)``.

        Review P1-watermark: the cursor tracks the #305 CAPTURE/COMMIT order
        (``captured_at``), never ``signal_time`` — late-committed events with
        earlier signal times stay inside the reconciliation scope (bounded by
        the documented overlap guarantee, see FR-05 diagnostics).
        """
        raise NotImplementedError

    def set_cursor(self, captured_at_us: int, source_event_key: str) -> None:
        raise NotImplementedError

    # ── overlap reconciliation state (review P1-overlap) ────────────────────
    def get_overlap_state(self) -> dict[str, Any]:
        """Durable reconciliation-pass state (survives restart).

        Keys: ``cursor_captured_at_us``, ``cursor_key``, ``pass_floor_us``,
        ``pass_ceiling_us``, ``pass_active``, ``passes_completed``.
        """
        raise NotImplementedError

    def save_overlap_state(self, state: Mapping[str, Any]) -> None:
        raise NotImplementedError

    # ── research API budget state (review P1-budget) ────────────────────────
    def load_api_budget_state(self) -> dict[str, Any] | None:
        """Persisted research API budget state (survives restarts)."""
        raise NotImplementedError

    def save_api_budget_state(self, state: Mapping[str, Any]) -> None:
        raise NotImplementedError

    # ── lifecycle ──────────────────────────────────────────────────────────
    def ensure_variant_state(
        self,
        *,
        source_event_key: str,
        variant_id: str,
        policy_fingerprint: str,
        policy: Mapping[str, Any],
        symbol: str,
        signal_time_us: int,
    ) -> str:
        raise NotImplementedError

    def get_variant_state(
        self, *, source_event_key: str, variant_id: str, policy_fingerprint: str
    ) -> VariantState | None:
        raise NotImplementedError

    def list_variant_states(
        self, *, pending_only: bool = True, limit: int = 100
    ) -> list[VariantState]:
        raise NotImplementedError

    def apply_evaluation(
        self,
        *,
        source_event_key: str,
        variant_id: str,
        policy_fingerprint: str,
        result_dict: Mapping[str, Any],
        decision: LifecycleDecision,
        evaluation_asof_ms: int,
        window_elapsed: bool,
        candle_source: Mapping[str, Any] | None = None,
    ) -> ApplyOutcome:
        raise NotImplementedError

    # ── telemetry / integrity ──────────────────────────────────────────────
    def record_conflict(
        self,
        *,
        conflict_kind: str,
        source_event_key: str,
        variant_id: str = "",
        policy_fingerprint: str = "",
        stored_hash: str = "",
        delivered_hash: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError

    def record_health(
        self, status: str, counters: Mapping[str, Any], details: Mapping[str, Any] | None = None
    ) -> None:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - trivial
        return None


# ---------------------------------------------------------------------------
# In-memory implementation (unit tests / dry-run)
# ---------------------------------------------------------------------------

class InMemoryProspectiveStore(H2ProspectiveStore):
    """Process-local store with the same invariants as the PG store."""

    def __init__(self) -> None:
        self._ingest: dict[str, dict[str, Any]] = {}
        self._states: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._results: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._conflicts: list[dict[str, Any]] = []
        self._evaluations: list[dict[str, Any]] = []
        self._health: list[dict[str, Any]] = []
        self._cursor: tuple[int, str] = (0, "")
        self._api_budget_state: dict[str, Any] | None = None
        self._overlap_state: dict[str, Any] = default_overlap_state()
        self.writes = 0

    # ── ingest ─────────────────────────────────────────────────────────────
    def ingest_event(self, delivery: Any) -> IngestOutcome:
        key = delivery.source_event_key
        signature = {
            "source_identity": delivery.source_identity,
            "lineage_hash": delivery.lineage_hash,
            "signal_time_us": delivery.signal_time_us,
            "immutable_hash": delivery.immutable_hash,
        }
        existing = self._ingest.get(key)
        if existing is not None:
            if existing["signature"] == signature:
                return IngestOutcome(
                    status=existing["status"],
                    reason=existing["reason"],
                    detail="idempotent redelivery; first-seen snapshot preserved",
                    duplicate=True,
                )
            self.record_conflict(
                conflict_kind=CONFLICT_EVENT_KEY_LINEAGE,
                source_event_key=key,
                stored_hash=existing["signature"].get("immutable_hash", ""),
                delivered_hash=delivery.immutable_hash,
                details={"signature_stored": existing["signature"], **signature},
            )
            return IngestOutcome(
                status=INGEST_REJECTED_LINEAGE,
                reason=REASON_EVENT_KEY_LINEAGE_CONFLICT,
                detail="redelivered identity differs from the first-seen snapshot",
                conflict=True,
            )
        self._ingest[key] = {
            "status": delivery.status,
            "reason": delivery.reason,
            "signature": signature,
            "row": dict(delivery.row) if delivery.row is not None else None,
        }
        self.writes += 1
        return IngestOutcome(status=delivery.status, reason=delivery.reason)

    def get_source_row(self, source_event_key: str) -> Mapping[str, Any] | None:
        entry = self._ingest.get(source_event_key)
        if entry is None or entry.get("row") is None:
            return None
        row = dict(entry["row"])
        row["source_reference_price"] = parse_source_price(
            row.get("source_reference_price")
        )
        return row

    def ingest_event_count(self) -> int:
        return len(self._ingest)

    def get_cursor(self) -> tuple[int, str]:
        return self._cursor

    def set_cursor(self, captured_at_us: int, source_event_key: str) -> None:
        self._cursor = (int(captured_at_us), str(source_event_key))

    def get_overlap_state(self) -> dict[str, Any]:
        return dict(self._overlap_state)

    def save_overlap_state(self, state: Mapping[str, Any]) -> None:
        # MERGE over the current state: a partial update must never regress
        # fields it did not mention (review FR-04 checkpoint safety)
        merged = dict(self._overlap_state)
        merged.update(
            {k: v for k, v in state.items() if k in default_overlap_state()}
        )
        merged["cursor_captured_at_us"] = int(merged["cursor_captured_at_us"])
        merged["pass_floor_us"] = int(merged["pass_floor_us"])
        merged["pass_ceiling_us"] = int(merged["pass_ceiling_us"])
        merged["passes_completed"] = int(merged["passes_completed"])
        merged["pass_active"] = bool(merged["pass_active"])
        merged["cursor_key"] = str(merged["cursor_key"])
        self._overlap_state = merged
        self.writes += 1

    def load_api_budget_state(self) -> dict[str, Any] | None:
        return (
            None
            if self._api_budget_state is None
            else dict(self._api_budget_state)
        )

    def save_api_budget_state(self, state: Mapping[str, Any]) -> None:
        self._api_budget_state = dict(state)
        self.writes += 1

    # ── lifecycle ──────────────────────────────────────────────────────────
    def ensure_variant_state(
        self,
        *,
        source_event_key: str,
        variant_id: str,
        policy_fingerprint: str,
        policy: Mapping[str, Any],
        symbol: str,
        signal_time_us: int,
    ) -> str:
        state_key = (source_event_key, variant_id, policy_fingerprint)
        existing = self._states.get(state_key)
        if existing is not None:
            return existing["state"]
        self._states[state_key] = {
            "source_event_key": source_event_key,
            "variant_id": variant_id,
            "policy_fingerprint": policy_fingerprint,
            "policy": dict(policy),
            "state": STATE_PENDING_CONFIRMATION,
            "symbol": symbol,
            "signal_time_us": int(signal_time_us),
            "evaluation_attempts": 0,
            "coverage_attempts": 0,
        }
        self.writes += 1
        return STATE_PENDING_CONFIRMATION

    def get_variant_state(
        self, *, source_event_key: str, variant_id: str, policy_fingerprint: str
    ) -> VariantState | None:
        row = self._states.get((source_event_key, variant_id, policy_fingerprint))
        return None if row is None else VariantState(**row)

    def list_variant_states(
        self, *, pending_only: bool = True, limit: int = 100
    ) -> list[VariantState]:
        out: list[VariantState] = []
        for row in self._states.values():
            state = VariantState(**row)
            if pending_only and state.terminal:
                continue
            out.append(state)
            if len(out) >= limit:
                break
        return out

    def apply_evaluation(
        self,
        *,
        source_event_key: str,
        variant_id: str,
        policy_fingerprint: str,
        result_dict: Mapping[str, Any],
        decision: LifecycleDecision,
        evaluation_asof_ms: int,
        window_elapsed: bool,
        candle_source: Mapping[str, Any] | None = None,
    ) -> ApplyOutcome:
        state_key = (source_event_key, variant_id, policy_fingerprint)
        state_row = self._states.get(state_key)
        if state_row is None:
            state_row = {
                "source_event_key": source_event_key,
                "variant_id": variant_id,
                "policy_fingerprint": policy_fingerprint,
                "policy": {},
                "state": STATE_PENDING_CONFIRMATION,
                "symbol": str(result_dict.get("symbol") or ""),
                "signal_time_us": int(result_dict.get("signal_time_us") or 0),
                "evaluation_attempts": 0,
                "coverage_attempts": 0,
            }
            self._states[state_key] = state_row
        if state_row["state"] in TERMINAL_STATES:
            # Terminal states are locked: never re-decided, never recreated.
            # But a rewrite carrying DIFFERENT immutable data is an explicit
            # integrity conflict — never silently swallowed (FR-05).
            existing = self._results.get(state_key)
            if existing is not None:
                locked_hash = immutable_result_hash(dict(result_dict))
                if existing["result_payload_hash"] != locked_hash:
                    self.record_conflict(
                        conflict_kind=CONFLICT_IMMUTABLE_RESULT,
                        source_event_key=source_event_key,
                        variant_id=variant_id,
                        policy_fingerprint=policy_fingerprint,
                        stored_hash=existing["result_payload_hash"],
                        delivered_hash=locked_hash,
                    )
                    return ApplyOutcome(
                        state=state_row["state"], conflict=True
                    )
            return ApplyOutcome(state=state_row["state"])

        state_row["evaluation_attempts"] += 1
        if result_dict.get("status") == "INCOMPLETE_COVERAGE":
            state_row["coverage_attempts"] += 1

        self._evaluations.append({
            "source_event_key": source_event_key,
            "variant_id": variant_id,
            "policy_fingerprint": policy_fingerprint,
            "evaluation_asof_ms": int(evaluation_asof_ms),
            "pipeline_status": str(result_dict.get("status") or ""),
            "state_before": state_row["state"],
            "state_after": decision.new_state or state_row["state"],
            "reason": decision.reason,
            "detail": decision.detail,
            "window_elapsed": bool(window_elapsed),
            "candle_source": (
                None if candle_source is None else dict(candle_source)
            ),
        })
        self.writes += 1

        if not decision.changes:
            return ApplyOutcome(state=state_row["state"])

        state_row["state"] = decision.new_state
        self.writes += 1

        if decision.new_state not in TERMINAL_STATES:
            return ApplyOutcome(state=decision.new_state)

        # Terminal outcome: unique + immutable (idempotent, conflict-aware).
        payload = dict(result_dict)
        delivered_hash = immutable_result_hash(payload)
        existing = self._results.get(state_key)
        if existing is not None:
            if existing["result_payload_hash"] != delivered_hash:
                self.record_conflict(
                    conflict_kind=CONFLICT_IMMUTABLE_RESULT,
                    source_event_key=source_event_key,
                    variant_id=variant_id,
                    policy_fingerprint=policy_fingerprint,
                    stored_hash=existing["result_payload_hash"],
                    delivered_hash=delivered_hash,
                )
                return ApplyOutcome(
                    state=existing.get("status", decision.new_state),
                    result_written=False,
                    conflict=True,
                )
            return ApplyOutcome(state=state_row["state"], result_written=False)
        self._results[state_key] = {
            "source_event_key": source_event_key,
            "variant_id": variant_id,
            "policy_fingerprint": policy_fingerprint,
            "status": decision.new_state,
            "result_payload": payload,
            "result_payload_hash": delivered_hash,
        }
        self.writes += 1
        return ApplyOutcome(state=decision.new_state, result_written=True)

    # ── telemetry / integrity ──────────────────────────────────────────────
    def record_conflict(
        self,
        *,
        conflict_kind: str,
        source_event_key: str,
        variant_id: str = "",
        policy_fingerprint: str = "",
        stored_hash: str = "",
        delivered_hash: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._conflicts.append({
            "conflict_kind": conflict_kind,
            "source_event_key": source_event_key,
            "variant_id": variant_id,
            "policy_fingerprint": policy_fingerprint,
            "stored_hash": stored_hash,
            "delivered_hash": delivered_hash,
            "details": dict(details or {}),
        })
        self.writes += 1

    def record_health(
        self, status: str, counters: Mapping[str, Any], details: Mapping[str, Any] | None = None
    ) -> None:
        self._health.append({
            "status": status,
            "counters": dict(counters),
            "details": dict(details or {}),
        })
        self.writes += 1

    # -- test helpers ------------------------------------------------------
    @property
    def ingest_rows(self) -> dict[str, dict[str, Any]]:
        return dict(self._ingest)

    @property
    def results(self) -> dict[tuple[str, str, str], dict[str, Any]]:
        return dict(self._results)

    @property
    def conflicts(self) -> list[dict[str, Any]]:
        return list(self._conflicts)

    @property
    def evaluations(self) -> list[dict[str, Any]]:
        return list(self._evaluations)

    @property
    def health_rows(self) -> list[dict[str, Any]]:
        return list(self._health)


# ---------------------------------------------------------------------------
# PostgreSQL implementation (DB-API: pg8000 or psycopg2)
# ---------------------------------------------------------------------------

class PgProspectiveStore(H2ProspectiveStore):
    """PostgreSQL store over a dedicated research connection.

    Driver-agnostic DB-API semantics (``%s`` placeholders, JSON passed as a
    canonical string with explicit ``::jsonb`` casts).  Every public method is
    one transaction; a failed statement rolls back and marks the store broken
    so the caller reconnects instead of hammering a dead connection.
    """

    def __init__(self, connection: Any) -> None:
        self._conn = connection
        self.broken = False

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - defensive
            logger.debug("h2 prospective store close failed", exc_info=True)

    # -- helpers ----------------------------------------------------------
    def _rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception:  # pragma: no cover - defensive
            logger.debug("h2 prospective store rollback failed", exc_info=True)

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        try:
            cur = self._conn.cursor()
            cur.execute(sql, tuple(params))
            return cur
        except Exception:
            self.broken = True
            self._rollback()
            raise

    def _commit(self) -> None:
        self._conn.commit()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, allow_nan=False, default=str)

    # ── ingest ─────────────────────────────────────────────────────────────
    def ingest_event(self, delivery: Any) -> IngestOutcome:
        key = delivery.source_event_key
        signature = {
            "source_identity": delivery.source_identity,
            "lineage_hash": delivery.lineage_hash,
            "signal_time_us": delivery.signal_time_us,
            "immutable_hash": delivery.immutable_hash,
        }
        cur = self._execute(
            f"""
            SELECT ingest_status, reject_reason, source_identity,
                   lineage_hash, signal_time_us, immutable_source_hash
            FROM {INGEST_TABLE}
            WHERE source_event_key = %s
            """,
            (key,),
        )
        existing = cur.fetchone()
        if existing is not None:
            stored_signature = {
                "source_identity": existing[2],
                "lineage_hash": existing[3],
                "signal_time_us": int(existing[4]),
                "immutable_hash": existing[5] or "",
            }
            if stored_signature == signature:
                self._execute(
                    f"""
                    UPDATE {INGEST_TABLE}
                    SET redelivery_count = redelivery_count + 1
                    WHERE source_event_key = %s
                    """,
                    (key,),
                )
                self._commit()
                return IngestOutcome(
                    status=existing[0],
                    reason=existing[1] or REASON_OK,
                    detail="idempotent redelivery; first-seen snapshot preserved",
                    duplicate=True,
                )
            self.record_conflict(
                conflict_kind=CONFLICT_EVENT_KEY_LINEAGE,
                source_event_key=key,
                stored_hash=stored_signature.get("immutable_hash", ""),
                delivered_hash=delivery.immutable_hash,
                details={"signature_stored": stored_signature, **signature},
            )
            return IngestOutcome(
                status=INGEST_REJECTED_LINEAGE,
                reason=REASON_EVENT_KEY_LINEAGE_CONFLICT,
                detail="redelivered identity differs from the first-seen snapshot",
                conflict=True,
            )

        row = delivery.row or {}
        # Review P1-Decimal: the NUMERIC column keeps the EXACT decimal and
        # the JSON payload keeps its exact text form — never a float round
        # trip, never an opaque str on restore.
        try:
            price_decimal = parse_source_price(
                row.get("source_reference_price")
            )
        except ValueError as exc:
            return IngestOutcome(
                status=INGEST_REJECTED_INVALID,
                reason=REASON_INVALID_SOURCE_PRICE,
                detail=str(exc),
            )
        payload = dict(row)
        payload["source_reference_price"] = exact_price_str(price_decimal)
        self._execute(
            f"""
            INSERT INTO {INGEST_TABLE} (
                source_event_key, source_identity, symbol, signal_time,
                signal_time_us, captured_at, activation_freeze_ts,
                lineage_hash, immutable_source_hash, ingest_status,
                reject_reason, reject_detail, source_fingerprint,
                signal_candle_open_time, entry_timeframe,
                source_reference_price, source_payload
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb
            )
            ON CONFLICT (source_event_key) DO NOTHING
            """,
            (
                key,
                delivery.source_identity,
                str(row.get("symbol") or ""),
                row.get("signal_time"),
                int(delivery.signal_time_us),
                row.get("captured_at"),
                row.get("activation_freeze_ts"),
                delivery.lineage_hash,
                delivery.immutable_hash,
                delivery.status,
                delivery.reason,
                delivery.detail,
                str(row.get("source_fingerprint") or ""),
                row.get("signal_candle_open_time"),
                row.get("entry_timeframe"),
                price_decimal,
                self._json(payload),
            ),
        )
        self._commit()
        return IngestOutcome(status=delivery.status, reason=delivery.reason)

    def get_source_row(self, source_event_key: str) -> Mapping[str, Any] | None:
        cur = self._execute(
            f"""
            SELECT source_payload, source_reference_price
            FROM {INGEST_TABLE} WHERE source_event_key = %s
            """,
            (source_event_key,),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload = dict(payload)
        # Review P1-Decimal: restore the price as an EXACT Decimal.  The
        # NUMERIC column is authoritative; the payload string is a strict
        # fallback — arbitrary strings are rejected, never coerced.
        column_price = row[1]
        if column_price is not None:
            payload["source_reference_price"] = parse_source_price(
                column_price
            )
        else:
            payload["source_reference_price"] = parse_source_price(
                payload.get("source_reference_price")
            )
        return payload

    def ingest_event_count(self) -> int:
        cur = self._execute(f"SELECT COUNT(*) FROM {INGEST_TABLE}")
        row = cur.fetchone()
        return int(row[0]) if row is not None else 0

    def get_cursor(self) -> tuple[int, str]:
        cur = self._execute(
            f"""
            SELECT cursor_captured_at_us, cursor_key FROM {WATERMARK_TABLE}
            WHERE id = 1
            """
        )
        row = cur.fetchone()
        if row is None:
            return 0, ""
        return int(row[0]), row[1]

    def set_cursor(self, captured_at_us: int, source_event_key: str) -> None:
        self._execute(
            f"""
            INSERT INTO {WATERMARK_TABLE} (id, cursor_captured_at_us, cursor_key)
            VALUES (1, %s, %s)
            ON CONFLICT (id) DO UPDATE
            SET cursor_captured_at_us = EXCLUDED.cursor_captured_at_us,
                cursor_key = EXCLUDED.cursor_key,
                updated_at = NOW()
            """,
            (int(captured_at_us), str(source_event_key)),
        )
        self._commit()

    # ── overlap reconciliation state (review P1-overlap) ────────────────────
    def get_overlap_state(self) -> dict[str, Any]:
        cur = self._execute(
            f"""
            SELECT cursor_captured_at_us, cursor_key, pass_floor_us,
                   pass_ceiling_us, pass_active, passes_completed
            FROM {OVERLAP_TABLE} WHERE id = 1
            """
        )
        row = cur.fetchone()
        state = default_overlap_state()
        if row is not None:
            state.update(
                {
                    "cursor_captured_at_us": int(row[0]),
                    "cursor_key": str(row[1] or ""),
                    "pass_floor_us": int(row[2]),
                    "pass_ceiling_us": int(row[3]),
                    "pass_active": bool(row[4]),
                    "passes_completed": int(row[5]),
                }
            )
        return state

    def save_overlap_state(self, state: Mapping[str, Any]) -> None:
        # MERGE over the current state: a partial update must never regress
        # fields it did not mention (review FR-04 checkpoint safety)
        merged = self.get_overlap_state()
        merged.update(
            {k: v for k, v in state.items() if k in default_overlap_state()}
        )
        self._execute(
            f"""
            INSERT INTO {OVERLAP_TABLE} (
                id, cursor_captured_at_us, cursor_key, pass_floor_us,
                pass_ceiling_us, pass_active, passes_completed
            ) VALUES (1, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE
            SET cursor_captured_at_us = EXCLUDED.cursor_captured_at_us,
                cursor_key = EXCLUDED.cursor_key,
                pass_floor_us = EXCLUDED.pass_floor_us,
                pass_ceiling_us = EXCLUDED.pass_ceiling_us,
                pass_active = EXCLUDED.pass_active,
                passes_completed = EXCLUDED.passes_completed,
                updated_at = NOW()
            """,
            (
                int(merged["cursor_captured_at_us"]),
                str(merged["cursor_key"]),
                int(merged["pass_floor_us"]),
                int(merged["pass_ceiling_us"]),
                bool(merged["pass_active"]),
                int(merged["passes_completed"]),
            ),
        )
        self._commit()

    # ── research API budget state (review P1-budget) ────────────────────────
    def load_api_budget_state(self) -> dict[str, Any] | None:
        cur = self._execute(
            f"""
            SELECT hour_started_at_ms, requests_in_hour
            FROM {BUDGET_TABLE} WHERE id = 1
            """
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "hour_started_at_ms": int(row[0]),
            "requests_in_hour": int(row[1]),
        }

    def save_api_budget_state(self, state: Mapping[str, Any]) -> None:
        self._execute(
            f"""
            INSERT INTO {BUDGET_TABLE} (id, hour_started_at_ms, requests_in_hour)
            VALUES (1, %s, %s)
            ON CONFLICT (id) DO UPDATE
            SET hour_started_at_ms = EXCLUDED.hour_started_at_ms,
                requests_in_hour = EXCLUDED.requests_in_hour,
                updated_at = NOW()
            """,
            (
                int(state.get("hour_started_at_ms") or 0),
                int(state.get("requests_in_hour") or 0),
            ),
        )
        self._commit()

    # ── lifecycle ──────────────────────────────────────────────────────────
    def ensure_variant_state(
        self,
        *,
        source_event_key: str,
        variant_id: str,
        policy_fingerprint: str,
        policy: Mapping[str, Any],
        symbol: str,
        signal_time_us: int,
    ) -> str:
        cur = self._execute(
            f"""
            INSERT INTO {STATE_TABLE} (
                source_event_key, variant_id, policy_fingerprint, policy,
                state, symbol, signal_time_us
            ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
            ON CONFLICT (source_event_key, variant_id, policy_fingerprint)
            DO NOTHING
            RETURNING state
            """,
            (
                source_event_key,
                variant_id,
                policy_fingerprint,
                self._json(dict(policy)),
                STATE_PENDING_CONFIRMATION,
                symbol,
                int(signal_time_us),
            ),
        )
        inserted = cur.fetchone()
        if inserted is not None:
            self._commit()
            return STATE_PENDING_CONFIRMATION
        cur = self._execute(
            f"""
            SELECT state FROM {STATE_TABLE}
            WHERE source_event_key = %s AND variant_id = %s
              AND policy_fingerprint = %s
            """,
            (source_event_key, variant_id, policy_fingerprint),
        )
        row = cur.fetchone()
        self._commit()
        return row[0] if row is not None else STATE_PENDING_CONFIRMATION

    def get_variant_state(
        self, *, source_event_key: str, variant_id: str, policy_fingerprint: str
    ) -> VariantState | None:
        cur = self._execute(
            f"""
            SELECT source_event_key, variant_id, policy_fingerprint, state,
                   policy, symbol, signal_time_us, evaluation_attempts,
                   coverage_attempts
            FROM {STATE_TABLE}
            WHERE source_event_key = %s AND variant_id = %s
              AND policy_fingerprint = %s
            """,
            (source_event_key, variant_id, policy_fingerprint),
        )
        row = cur.fetchone()
        if row is None:
            return None
        policy = row[4]
        if isinstance(policy, str):
            policy = json.loads(policy)
        return VariantState(
            source_event_key=row[0],
            variant_id=row[1],
            policy_fingerprint=row[2],
            state=row[3],
            policy=dict(policy or {}),
            symbol=row[5],
            signal_time_us=int(row[6]),
            evaluation_attempts=int(row[7]),
            coverage_attempts=int(row[8]),
        )

    def list_variant_states(
        self, *, pending_only: bool = True, limit: int = 100
    ) -> list[VariantState]:
        clause = (
            "WHERE state = %s" if pending_only else ""
        )
        params: list[Any] = [STATE_PENDING_CONFIRMATION] if pending_only else []
        params.append(int(limit))
        cur = self._execute(
            f"""
            SELECT source_event_key, variant_id, policy_fingerprint, state,
                   policy, symbol, signal_time_us, evaluation_attempts,
                   coverage_attempts
            FROM {STATE_TABLE}
            {clause}
            ORDER BY signal_time_us, source_event_key, variant_id
            LIMIT %s
            """,
            params,
        )
        out: list[VariantState] = []
        for row in cur.fetchall():
            policy = row[4]
            if isinstance(policy, str):
                policy = json.loads(policy)
            out.append(
                VariantState(
                    source_event_key=row[0],
                    variant_id=row[1],
                    policy_fingerprint=row[2],
                    state=row[3],
                    policy=dict(policy or {}),
                    symbol=row[5],
                    signal_time_us=int(row[6]),
                    evaluation_attempts=int(row[7]),
                    coverage_attempts=int(row[8]),
                )
            )
        return out

    def apply_evaluation(
        self,
        *,
        source_event_key: str,
        variant_id: str,
        policy_fingerprint: str,
        result_dict: Mapping[str, Any],
        decision: LifecycleDecision,
        evaluation_asof_ms: int,
        window_elapsed: bool,
        candle_source: Mapping[str, Any] | None = None,
    ) -> ApplyOutcome:
        state = self.ensure_variant_state(
            source_event_key=source_event_key,
            variant_id=variant_id,
            policy_fingerprint=policy_fingerprint,
            policy=result_dict.get("policy") or {},
            symbol=str(result_dict.get("symbol") or ""),
            signal_time_us=int(result_dict.get("signal_time_us") or 0),
        )
        if state in TERMINAL_STATES:
            # Terminal states are locked: never re-decided, never recreated.
            # A rewrite with DIFFERENT immutable data is an explicit conflict.
            locked_hash = immutable_result_hash(dict(result_dict))
            cur = self._execute(
                f"""
                SELECT result_payload_hash FROM {RESULT_TABLE}
                WHERE source_event_key = %s AND variant_id = %s
                  AND policy_fingerprint = %s
                """,
                (source_event_key, variant_id, policy_fingerprint),
            )
            existing = cur.fetchone()
            if existing is not None and existing[0] != locked_hash:
                self.record_conflict(
                    conflict_kind=CONFLICT_IMMUTABLE_RESULT,
                    source_event_key=source_event_key,
                    variant_id=variant_id,
                    policy_fingerprint=policy_fingerprint,
                    stored_hash=existing[0],
                    delivered_hash=locked_hash,
                )
                return ApplyOutcome(state=state, conflict=True)
            return ApplyOutcome(state=state)

        self._execute(
            f"""
            UPDATE {STATE_TABLE}
            SET evaluation_attempts = evaluation_attempts + 1,
                coverage_attempts = coverage_attempts + %s,
                last_evaluation_asof_ms = %s,
                last_evaluated_at = NOW()
            WHERE source_event_key = %s AND variant_id = %s
              AND policy_fingerprint = %s
            """,
            (
                1 if str(result_dict.get("status")) == "INCOMPLETE_COVERAGE" else 0,
                int(evaluation_asof_ms),
                source_event_key,
                variant_id,
                policy_fingerprint,
            ),
        )
        self._execute(
            f"""
            INSERT INTO {EVALUATION_TABLE} (
                source_event_key, variant_id, policy_fingerprint,
                evaluation_asof_ms, pipeline_status, state_before, state_after,
                reason, detail, window_elapsed, candle_source
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                source_event_key,
                variant_id,
                policy_fingerprint,
                int(evaluation_asof_ms),
                str(result_dict.get("status") or ""),
                state,
                decision.new_state or state,
                decision.reason,
                decision.detail,
                bool(window_elapsed),
                self._json(dict(candle_source)) if candle_source else "{}",
            ),
        )

        if not decision.changes:
            self._commit()
            return ApplyOutcome(state=state)

        self._execute(
            f"""
            UPDATE {STATE_TABLE}
            SET state = %s,
                last_reason = %s,
                last_detail = %s,
                terminal_at = CASE WHEN %s THEN NOW() ELSE terminal_at END
            WHERE source_event_key = %s AND variant_id = %s
              AND policy_fingerprint = %s
            """,
            (
                decision.new_state,
                decision.reason,
                decision.detail,
                decision.new_state in TERMINAL_STATES,
                source_event_key,
                variant_id,
                policy_fingerprint,
            ),
        )

        if decision.new_state not in TERMINAL_STATES:
            self._commit()
            return ApplyOutcome(state=decision.new_state)

        payload = dict(result_dict)
        delivered_hash = immutable_result_hash(payload)
        cur = self._execute(
            f"""
            SELECT result_payload_hash, status FROM {RESULT_TABLE}
            WHERE source_event_key = %s AND variant_id = %s
              AND policy_fingerprint = %s
            """,
            (source_event_key, variant_id, policy_fingerprint),
        )
        existing = cur.fetchone()
        if existing is not None:
            self._commit()
            if existing[0] != delivered_hash:
                self.record_conflict(
                    conflict_kind=CONFLICT_IMMUTABLE_RESULT,
                    source_event_key=source_event_key,
                    variant_id=variant_id,
                    policy_fingerprint=policy_fingerprint,
                    stored_hash=existing[0],
                    delivered_hash=delivered_hash,
                )
                return ApplyOutcome(
                    state=existing[1], result_written=False, conflict=True
                )
            return ApplyOutcome(state=decision.new_state, result_written=False)

        self._execute(
            f"""
            INSERT INTO {RESULT_TABLE} (
                source_event_key, variant_id, policy_fingerprint, status,
                reason, detail, expiry_reason, source_identity, symbol,
                lineage_hash, signal_time_us, activation_freeze_ts_ms,
                policy, policy_fingerprint_check, rsi_calc_version,
                algorithm_version, pipeline_version, runtime_version,
                confirmation_candle_open_ms, confirmation_time_ms,
                confirmation_time_us, h2_rsi_at_confirmation,
                source_rsi_14_snapshot, source_rsi_delta_3_snapshot,
                source_entry_confirmation_rsi_rising, entry_candidate_time_ms,
                entry_candidate_time_us, entry_candidate_price,
                entry_price_basis, entry_reference_close_price,
                entry_reference_label, entry_delay_ms, entry_delay_us,
                observed_window_candles, coverage_status, coverage_detail,
                original_price, original_price_exact, original_price_basis,
                candle_source, evaluation_asof_ms, result_payload,
                result_payload_hash
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s,
                %s::jsonb, %s
            )
            ON CONFLICT (source_event_key, variant_id, policy_fingerprint)
            DO NOTHING
            """,
            (
                source_event_key,
                variant_id,
                policy_fingerprint,
                decision.new_state,
                str(result_dict.get("reason") or ""),
                str(result_dict.get("detail") or ""),
                result_dict.get("expiry_reason"),
                str(result_dict.get("lineage", {}).get("source_identity") or ""),
                str(result_dict.get("symbol") or ""),
                str(result_dict.get("lineage", {}).get("lineage_hash") or ""),
                int(result_dict.get("lineage", {}).get("signal_time_us") or 0),
                int(result_dict.get("lineage", {}).get("activation_freeze_ts_ms") or 0),
                self._json(result_dict.get("policy") or {}),
                str(result_dict.get("policy_fingerprint") or ""),
                str(result_dict.get("rsi_calc_version") or ""),
                "H2_RSI_ONLY_1.0.0",
                "H2_PIPELINE_1.1.0",
                "H2_PROSPECTIVE_RUNTIME_1.0.0",
                result_dict.get("confirmation_candle_open_ms"),
                result_dict.get("confirmation_time_ms"),
                result_dict.get("confirmation_time_us"),
                result_dict.get("h2_rsi_at_confirmation"),
                result_dict.get("source_rsi_14_snapshot"),
                result_dict.get("source_rsi_delta_3_snapshot"),
                result_dict.get("source_entry_confirmation_rsi_rising"),
                result_dict.get("entry_candidate_time_ms"),
                result_dict.get("entry_candidate_time_us"),
                result_dict.get("entry_candidate_price"),
                str(result_dict.get("entry_price_basis") or ""),
                result_dict.get("entry_reference_close_price"),
                str(result_dict.get("entry_reference_label") or ""),
                result_dict.get("entry_delay_ms"),
                result_dict.get("entry_delay_us"),
                int(result_dict.get("observed_window_candles") or 0),
                str(result_dict.get("coverage_status") or ""),
                str(result_dict.get("coverage_detail") or ""),
                result_dict.get("original_price"),
                result_dict.get("original_price_exact"),
                str(result_dict.get("original_price_basis") or ""),
                self._json(result_dict.get("candle_source") or {}),
                int(result_dict.get("evaluation_asof_ms") or 0),
                self._json(payload),
                delivered_hash,
            ),
        )
        self._commit()
        return ApplyOutcome(state=decision.new_state, result_written=True)

    # ── telemetry / integrity ──────────────────────────────────────────────
    def record_conflict(
        self,
        *,
        conflict_kind: str,
        source_event_key: str,
        variant_id: str = "",
        policy_fingerprint: str = "",
        stored_hash: str = "",
        delivered_hash: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._execute(
            f"""
            INSERT INTO {CONFLICT_TABLE} (
                conflict_kind, source_event_key, variant_id,
                policy_fingerprint, stored_hash, delivered_hash, details
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                conflict_kind,
                source_event_key,
                variant_id,
                policy_fingerprint,
                stored_hash,
                delivered_hash,
                self._json(dict(details or {})),
            ),
        )
        self._commit()

    def record_health(
        self, status: str, counters: Mapping[str, Any], details: Mapping[str, Any] | None = None
    ) -> None:
        self._execute(
            f"""
            INSERT INTO {HEALTH_TABLE} (status, counters, details)
            VALUES (%s, %s::jsonb, %s::jsonb)
            """,
            (
                status,
                self._json(dict(counters)),
                self._json(dict(details or {})),
            ),
        )
        self._commit()


def connect_pg_store(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    unix_sock: str | None = None,
    statement_timeout_ms: int = 10_000,
) -> PgProspectiveStore:
    """Open a DEDICATED pg8000 connection for the H2 prospective runtime.

    Never shared with the scanner/paper runners; every socket operation is
    bounded (10s) and every statement carries an explicit statement timeout.
    """
    import pg8000

    kwargs: dict[str, Any] = {
        "database": database,
        "user": user,
        "password": password,
        "timeout": 10,
    }
    if unix_sock:
        kwargs["unix_sock"] = unix_sock
    else:
        kwargs["host"] = host
        kwargs["port"] = port
    conn = pg8000.connect(**kwargs)
    cur = conn.cursor()
    cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
    conn.commit()
    return PgProspectiveStore(conn)
