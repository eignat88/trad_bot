"""Issue #312 / FR-07 — H2 prospective runtime orchestration (research only).

One bounded, restart-safe polling cycle:

1. read new #305 deliveries (SELECT-only) and ingest them idempotently into
   the research store (freeze/lineage checks at the boundary; watermark
   checkpoint — unfinished events are never skipped);
2. for every pending (event x variant x policy) state: acquire CLOSED 5m
   candles under the dedicated research API budget and evaluate the pure
   PR #311 H2 engine at the ACTUAL observation instant;
3. persist lifecycle decisions / terminal outcomes and telemetry.

Isolation (FR-07, criterion 18):

* no ``scanner_runner.py``, no trading signals, no orders, no paper/live, no
  production detector / scoring / gate interaction of any kind;
* with ``config.enabled`` False the runtime performs NO database connection,
  NO Bybit request and NO write (criterion 17);
* when the candle budget is unconfirmed the provider stays OFF, source
  ingest continues and the cycle reports ``BLOCKED_CANDLE_SOURCE``;
* a Bybit / API failure is fail-closed for the research runtime ONLY — it
  never touches the production scanner (criterion 14).

Missing data never fabricates an outcome: candle-fetch failures leave states
``PENDING_CONFIRMATION`` and only the explicit lifecycle rules
(:mod:`.lifecycle`) can finalize ``INCOMPLETE_COVERAGE``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from app.research.me_entry_quality_h2_prospective_v1.candle_adapter import (
    BybitClosedCandleProvider,
    observation_grid,
    observation_window_elapsed,
    rows_as_mappings,
)
from app.research.me_entry_quality_h2_prospective_v1.config import (
    H2ProspectiveConfig,
)
from app.research.me_entry_quality_h2_prospective_v1.clean_boundary import (
    activation_timestamp_us,
    eligible_for_clean_holdout,
)
from app.research.me_entry_quality_h2_prospective_v1.contracts import (
    INGEST_ACCEPTED,
    INGEST_REJECTED_INVALID,
    INGEST_REJECTED_LINEAGE,
    INGEST_REJECTED_PRE_FREEZE,
    REASON_INVALID_SOURCE_PRICE,
    RUNTIME_DISABLED,
    STATE_CONFIRMED_DELAYED,
    STATE_EXPIRED,
    STATE_INCOMPLETE_COVERAGE,
    STATE_INVALID_SOURCE,
    STATE_NOT_READY,
    candidate_policies,
    policy_manifest,
)
from app.research.me_entry_quality_h2_prospective_v1.lifecycle import (
    EvaluationObservation,
    LifecycleDecision,
    decide_next_state,
)
from app.research.me_entry_quality_h2_prospective_v1.source_reader import (
    SourceDelivery,
    cursor_of,
    delivery_from_row,
    count_source_events_between,
    read_source_events_after,
)
from app.research.me_entry_quality_h2_prospective_v1.store import (
    H2ProspectiveStore,
    VariantState,
)
from app.research.me_entry_quality_h2_prospective_v1.telemetry import (
    TelemetryCounters,
    cycle_status,
    log_cycle,
    log_source_empty_waiting,
)
from app.research.me_entry_quality_h2_v1.pipeline import run_h2_event
from app.research.me_entry_quality_h2_v1.result import H2Policy

H2P_RUNTIME_VERSION = "H2_PROSPECTIVE_RUNTIME_1.0.0"

logger = logging.getLogger(__name__)


@dataclass
class CycleReport:
    """Deterministic summary of ONE runtime cycle (telemetry payload)."""

    status: str
    counters: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


class H2ProspectiveRuntime:
    """Restart-safe research runtime (disabled by default, fail closed)."""

    def __init__(
        self,
        *,
        config: H2ProspectiveConfig,
        store: H2ProspectiveStore,
        candle_provider: BybitClosedCandleProvider,
        source_connection_factory: Callable[[], Any] | None = None,
        policies: Sequence[H2Policy] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._store = store
        self._provider = candle_provider
        self._source_connection_factory = source_connection_factory
        self._clock = clock
        # Review P1-budget: the hourly request window is PERSISTED — hydrate
        # it on start and keep saving it, so a restart never resets the
        # research budget unsafely.
        budget = getattr(candle_provider, "budget", None)
        if budget is not None:
            budget.hydrate(self._store.load_api_budget_state())
            budget.on_change = self._store.save_api_budget_state
        # Review P1-overlap FR-04: a pass interrupted by a restart resumes
        # where its durable cursor stopped (reported once as
        # ``overlap_restart_recovery``).
        self._overlap_restart_recovery_pending = bool(
            self._store.get_overlap_state().get("pass_active")
        )
        self._clean_activation_us = (
            activation_timestamp_us(config.clean_activation_ts)
            if config.clean_activation_ts else None
        )
        self._policies = tuple(policies) if policies is not None else (
            candidate_policies(config.observation_windows)
        )
        if not self._policies:
            raise ValueError("at least one H2Policy is required")
        pins = set(config.policy_fingerprint_pins)
        if pins:
            for policy in self._policies:
                if policy.fingerprint() not in pins:
                    raise ValueError(
                        "POLICY_FINGERPRINT_MISMATCH: computed policy "
                        f"fingerprint {policy.fingerprint()} is not pinned"
                    )
        if self._clean_activation_us is not None and config.enabled:
            # No DB write can occur before every fingerprint is checked.
            # In clean pinned mode, the approved set must be exact.
            expected = {p.fingerprint() for p in self._policies}
            if pins and pins != expected:
                raise ValueError("POLICY_FINGERPRINT_MISMATCH: pin set differs")
            mode = "CLEAN_FROZEN" if pins else "PRE_FREEZE_RESEARCH"
            self._store.bind_clean_activation(
                self._clean_activation_us, tuple(sorted(expected)), mode=mode
            )

    @property
    def policies(self) -> tuple[H2Policy, ...]:
        return self._policies

    def policy_manifest(self) -> dict[str, Any]:
        return policy_manifest(self._policies)

    # ── one bounded cycle ──────────────────────────────────────────────────
    def run_cycle(self) -> CycleReport:
        if not self._config.enabled:
            # Criterion 17: nothing connects, nothing is requested, nothing
            # is written while the runtime is disabled.
            return CycleReport(status=RUNTIME_DISABLED)

        counters = TelemetryCounters()
        self._provider.budget.start_cycle()
        details: dict[str, Any] = {
            "runtime_version": H2P_RUNTIME_VERSION,
            "policy_manifest": self.policy_manifest(),
        }

        rows_seen = self._ingest_phase(counters, details)
        candle_ready = self._evaluate_phase(counters, details)

        cohort_empty = (
            rows_seen == 0 and self._store.ingest_event_count() == 0
        )
        status = cycle_status(
            counters=counters,
            source_rows_seen=rows_seen,
            candle_provider_enabled=candle_ready,
            source_cohort_empty=cohort_empty,
        )
        if cohort_empty:
            # FR-08: expected state — not an RSI-algorithm error and not
            # proof of source coverage.
            log_source_empty_waiting(logger)
            details["source_empty_waiting"] = True

        counters.variants_pending = len(
            self._store.list_variant_states(pending_only=True, limit=1000)
        )
        self._store.record_health(status, counters.to_dict(), details)
        log_cycle(logger, status=status, counters=counters, details=details)
        return CycleReport(
            status=status, counters=counters.to_dict(), details=details
        )

    # ── phase 1: source ingest (FR-01) ─────────────────────────────────────
    def _ingest_phase(self, counters: TelemetryCounters, details: dict) -> int:
        if self._source_connection_factory is None:
            details["source_reader"] = "NO_CONNECTION_FACTORY"
            return 0
        conn = self._source_connection_factory()
        try:
            cursor_us, cursor_key = self._store.get_cursor()
            # Review P1-watermark: the delivery cursor is the CAPTURE order
            # (captured_at, source_event_key) — never signal_time.  Two
            # phases per cycle with INDEPENDENT bounded budgets (review
            # P1-overlap FR-02) — forward backlog can never starve
            # reconciliation and vice versa:
            #   1. forward scan from the forward cursor — guaranteed forward
            #      progress for every visible row (the cursor IS capture
            #      order, so a monotonic scan is complete);
            #   2. a DURABLE reconciliation pass over the overlap range —
            #      catches capture-to-commit disorder (rows whose capture
            #      instant predates the forward cursor but whose commit
            #      became visible later) and ``captured_at`` ties, with its
            #      own persisted progress cursor (FR-01/FR-03/FR-04).
            rows_seen = 0

            # phase 1: forward scan from the capture-order cursor
            f_us, f_key = int(cursor_us), str(cursor_key)
            fwd_start_us, fwd_start_key = f_us, f_key
            for _ in range(self._config.source_forward_batches_per_cycle):
                rows = read_source_events_after(
                    conn,
                    after_captured_us=f_us,
                    after_key=f_key,
                    limit=self._config.source_batch_limit,
                )
                if not rows:
                    break
                counters.forward_batches_scanned += 1
                rows_seen += len(rows)
                counters.source_events_received += len(rows)
                for row in rows:
                    self._ingest_row(row, counters)
                f_us, f_key = cursor_of(rows)
                if len(rows) < self._config.source_batch_limit:
                    break
            if (f_us, f_key) > (fwd_start_us, fwd_start_key):
                # checkpoint-last: forward progress only after durable writes
                self._store.set_cursor(f_us, f_key)

            # phase 2: durable overlap reconciliation pass (own budget)
            rows_seen += self._overlap_pass(conn, counters, details, f_us)
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover - defensive
                logger.debug("source connection close failed", exc_info=True)

        details["source_rows_read"] = rows_seen
        return rows_seen

    def _overlap_pass(
        self,
        conn: Any,
        counters: TelemetryCounters,
        details: dict,
        forward_us: int,
    ) -> int:
        """One bounded reconciliation pass with DURABLE per-batch progress.

        Review P1-overlap (FR-01..FR-05):

        * the pass range ``[pass_floor_us, pass_ceiling_us]`` is FIXED when
          the pass starts (the ceiling pins the forward cursor position at
          that moment) — forward-cursor advancement during the pass can
          never exclude unchecked ranges (FR-03);
        * the next pass's floor is ``min(previous ceiling, new ceiling -
          source_overlap_us)``: the documented window is re-checked while
          the sweep keeps up, and the pass simply continues where the last
          one ended when it fell behind — ranges are NEVER silently
          skipped; when it falls behind (the floor must reach back BEYOND
          the documented window to stay contiguous), the documented
          disorder guarantee is not honoured for the slid-past ranges and
          the cycle reports ``OVERLAP_COVERAGE_DEGRADED`` instead of a
          false confirmation (FR-05);
        * progress is checkpointed AFTER every durably processed batch and
          survives restarts (FR-04); a crashed pass resumes where it
          stopped (``overlap_restart_recovery``);
        * completeness is BOUNDED by the documented overlap guarantee — no
          absolute no-loss claim is made for disorder beyond it; the
          boundary is published as ``guarantee_floor_us`` /
          ``oldest_unchecked_capture_ts`` diagnostics.
        """
        state = self._store.get_overlap_state()
        rows_seen = 0
        window_us = int(self._config.source_overlap_us)
        passes_completed = int(state["passes_completed"])

        if state["pass_active"]:
            ceiling_us = int(state["pass_ceiling_us"])
            floor_us = int(state["pass_floor_us"])
            o_us = int(state["cursor_captured_at_us"])
            o_key = str(state["cursor_key"])
            if self._overlap_restart_recovery_pending:
                counters.overlap_restart_recovery = True
                self._overlap_restart_recovery_pending = False
        else:
            # ── start a NEW pass with fixed bounds (FR-03) ──
            ceiling_us = int(forward_us)
            window_floor_us = max(0, ceiling_us - window_us)
            if passes_completed > 0:
                # contiguity guard: never exclude ranges the previous pass
                # has not reached — floor <= previous pass ceiling
                floor_us = min(int(state["pass_ceiling_us"]), window_floor_us)
            else:
                floor_us = window_floor_us
            o_us, o_key = floor_us, ""
            state = {
                "cursor_captured_at_us": o_us,
                "cursor_key": o_key,
                "pass_floor_us": floor_us,
                "pass_ceiling_us": ceiling_us,
                "pass_active": True,
                "passes_completed": passes_completed,
            }
            self._store.save_overlap_state(state)

        # FR-05 coverage diagnostic: the documented window is honoured iff the
        # pass floor equals the documented window floor.  A strictly LOWER
        # floor means the previous pass fell behind by more than one window —
        # ranges slid out of the window while unchecked, so the documented
        # disorder guarantee was NOT honoured for them (the sweep catches up
        # contiguously instead of re-checking the window).
        documented_floor = max(0, ceiling_us - window_us)
        coverage_degraded = floor_us < documented_floor
        counters.overlap_coverage_degraded = coverage_degraded

        exhausted = ceiling_us <= o_us  # empty range completes immediately
        for _ in range(self._config.source_overlap_batches_per_cycle):
            if exhausted:
                break
            rows = read_source_events_after(
                conn,
                after_captured_us=o_us,
                after_key=o_key,
                limit=self._config.source_batch_limit,
                to_captured_us=ceiling_us,
            )
            if not rows:
                exhausted = True
                break
            counters.overlap_batches_scanned += 1
            rows_seen += len(rows)
            counters.source_events_received += len(rows)
            for row in rows:
                dup_before = counters.idempotent_duplicates
                pre_before = counters.source_events_pre_freeze
                self._ingest_row(row, counters)
                if (counters.idempotent_duplicates == dup_before
                        and counters.source_events_pre_freeze == pre_before):
                    # first-seen HERE = late commit (forward never saw it)
                    counters.late_events_discovered += 1
            o_us, o_key = cursor_of(rows)
            # FR-03/FR-04: durable per-batch checkpoint AFTER the covered
            # rows are ingested and their variant states exist (checkpoint
            # last — a crash re-delivers idempotently, never skips).
            self._store.save_overlap_state(
                {
                    "cursor_captured_at_us": o_us,
                    "cursor_key": o_key,
                    "pass_floor_us": floor_us,
                    "pass_ceiling_us": ceiling_us,
                    "pass_active": True,
                    "passes_completed": passes_completed,
                }
            )
            if len(rows) < self._config.source_batch_limit:
                exhausted = True

        if exhausted:
            # pass end: advance past the fixed ceiling and close the pass
            passes_completed += 1
            self._store.save_overlap_state(
                {
                    "cursor_captured_at_us": ceiling_us,
                    "cursor_key": "",
                    "pass_floor_us": floor_us,
                    "pass_ceiling_us": ceiling_us,
                    "pass_active": False,
                    "passes_completed": passes_completed,
                }
            )
            counters.overlap_pass_completed = True

        # telemetry: lag, backlog, unchecked boundary (bounded queries)
        counters.overlap_cursor_lag_s = max(0.0, (int(forward_us) - o_us) / 1e6)
        counters.oldest_unchecked_capture_ts = o_us if not exhausted else None
        try:
            counters.overlap_backlog_estimate = count_source_events_between(
                conn,
                after_captured_us=o_us,
                after_key=o_key,
                to_captured_us=ceiling_us,
            )
        except Exception:  # pragma: no cover - diagnostic only
            logger.debug("overlap backlog estimate failed", exc_info=True)
        details["overlap"] = {
            "pass_floor_us": floor_us,
            "pass_ceiling_us": ceiling_us,
            "pass_active": not exhausted,
            "passes_completed": passes_completed,
            # the DOCUMENTED completeness boundary (FR-05): capture
            # instants strictly below it are outside this pass's guarantee
            "guarantee_floor_us": floor_us,
            "coverage_degraded": coverage_degraded,
            "coverage_guarantee": (
                "BROKEN_WINDOW_CONTIGUOUS_SWEEP"
                if coverage_degraded
                else "DOCUMENTED_OVERLAP_WINDOW"
            ),
        }
        return rows_seen

    def _ingest_row(self, row: Mapping[str, Any], counters: TelemetryCounters) -> None:
        if self._clean_activation_us is not None:
            # Historical #305 events advance the delivery cursor, but NEVER
            # enter the H2 ingest ledger or create evaluated variant states.
            if not eligible_for_clean_holdout(row, self._clean_activation_us):
                counters.source_events_pre_freeze += 1
                return
        delivery: SourceDelivery = delivery_from_row(row)
        outcome = self._store.ingest_event(delivery)
        if outcome.duplicate:
            counters.idempotent_duplicates += 1
            # Review P1-watermark crash-safety: variant states are re-ensured
            # even on duplicates — a crash between ingest and state creation
            # can never leave an accepted event without its lifecycle rows.
            if outcome.status == INGEST_ACCEPTED:
                self._ensure_variant_states(delivery)
            return
        if outcome.conflict:
            counters.lineage_conflicts += 1
        if outcome.status == INGEST_ACCEPTED:
            counters.source_events_accepted += 1
            self._ensure_variant_states(delivery)
        elif outcome.status == INGEST_REJECTED_PRE_FREEZE:
            counters.source_events_pre_freeze += 1
            counters.source_events_rejected += 1
        elif outcome.status in (INGEST_REJECTED_LINEAGE, INGEST_REJECTED_INVALID):
            counters.source_events_invalid += 1
            counters.source_events_rejected += 1

    def _ensure_variant_states(self, delivery: SourceDelivery) -> None:
        row = delivery.row or {}
        for policy in self._policies:
            self._store.ensure_variant_state(
                source_event_key=delivery.source_event_key,
                variant_id=policy.variant_id,
                policy_fingerprint=policy.fingerprint(),
                policy=policy.to_dict(),
                symbol=str(row.get("symbol") or ""),
                signal_time_us=delivery.signal_time_us,
            )

    # ── phase 2: closed-candle evaluation (FR-02/FR-03/FR-04) ──────────────
    def _evaluate_phase(self, counters: TelemetryCounters, details: dict) -> bool:
        if not self._provider.enabled:
            details["candle_provider"] = "DISABLED_BUDGET_UNCONFIRMED"
            return False

        pending = self._store.list_variant_states(
            pending_only=True, limit=self._config.max_events_per_cycle
        )
        details["pending_evaluated"] = len(pending)
        if not pending:
            return True

        by_event: dict[str, list[VariantState]] = {}
        for state in pending:
            by_event.setdefault(state.source_event_key, []).append(state)

        asof_ms = int(self._clock() * 1000)
        for event_key, states in by_event.items():
            self._evaluate_event(event_key, states, asof_ms, counters, details)
        return True

    def _evaluate_event(
        self,
        event_key: str,
        states: Sequence[VariantState],
        asof_ms: int,
        counters: TelemetryCounters,
        details: dict,
    ) -> None:
        try:
            row = self._store.get_source_row(event_key)
        except ValueError as exc:
            # Review P1-Decimal: a stored price that fails the strict restore
            # parse is an explicit INVALID_SOURCE outcome — never a silent
            # coercion and never a fabricated evaluation.
            self._apply_invalid_source(
                event_key, states, asof_ms, str(exc), counters, details
            )
            return
        if row is None:
            details.setdefault("missing_source_rows", []).append(event_key)
            return
        signal_time_us = int(states[0].signal_time_us or 0)
        if signal_time_us <= 0:
            from app.research.me_entry_quality_h1c_v1.event_adapter import (
                parse_utc_us,
            )

            signal_time_us = parse_utc_us(
                row.get("signal_time"), field_name="signal_time"
            )
        symbol = states[0].symbol or str(row.get("symbol") or "")
        grid = observation_grid(signal_time_us)

        outcome = self._provider.fetch_closed_5m(
            symbol=symbol, grid=grid, evaluation_asof_ms=asof_ms
        )
        counters.api_requests += outcome.api_requests
        counters.api_retries += outcome.retries
        counters.rate_limit_skips += outcome.rate_limit_skips
        if not outcome.ok:
            counters.candle_fetch_errors += 1
            details.setdefault("candle_fetch_failures", {})[event_key] = (
                outcome.reason
            )
            return

        policies_by_fp = {state.policy_fingerprint: state for state in states}
        policies = []
        for state in states:
            policy_dict = state.policy
            policies.append(
                H2Policy(
                    variant_id=str(policy_dict.get("variant_id") or state.variant_id),
                    observation_window=int(policy_dict.get("observation_window") or 1),
                    rsi_period=int(policy_dict.get("rsi_period") or 14),
                    peak_rule=policy_dict.get("peak_rule"),
                )
            )

        try:
            pipeline_outcome = run_h2_event(
                row,
                rows_5m=rows_as_mappings(outcome.rows),
                source_identity=outcome.source_identity,
                policies=policies,
                evaluation_asof_ms=asof_ms,
            )
        except ValueError as exc:
            self._apply_invalid_source(
                event_key, states, asof_ms, str(exc), counters, details
            )
            return
        for result in pipeline_outcome.results:
            state = policies_by_fp.get(result.policy_fingerprint)
            if state is None:  # pragma: no cover - defensive
                continue
            self._apply_variant_result(
                state=state,
                result=result,
                asof_ms=asof_ms,
                grid=grid,
                candle_source=pipeline_outcome.source,
                counters=counters,
            )

    def _apply_invalid_source(
        self,
        event_key: str,
        states: Sequence[VariantState],
        asof_ms: int,
        detail: str,
        counters: TelemetryCounters,
        details: dict,
    ) -> None:
        """Explicit INVALID_SOURCE outcome for unusable source rows
        (review P1-Decimal: e.g. an unparseable stored price)."""
        details.setdefault("invalid_source_rows", {})[event_key] = detail[:300]
        for state in states:
            decision = LifecycleDecision(
                new_state=STATE_INVALID_SOURCE,
                terminal=True,
                reason=REASON_INVALID_SOURCE_PRICE,
                detail=detail[:500],
            )
            result_dict = {
                "status": STATE_INVALID_SOURCE,
                "reason": REASON_INVALID_SOURCE_PRICE,
                "detail": detail[:500],
                "symbol": state.symbol,
                "signal_time_us": int(state.signal_time_us or 0),
                "policy": dict(state.policy),
                "policy_fingerprint": state.policy_fingerprint,
            }
            counters.variant_states_evaluated += 1
            apply = self._store.apply_evaluation(
                source_event_key=state.source_event_key,
                variant_id=state.variant_id,
                policy_fingerprint=state.policy_fingerprint,
                result_dict=result_dict,
                decision=decision,
                evaluation_asof_ms=asof_ms,
                window_elapsed=False,
                candle_source=None,
            )
            if apply.conflict:
                counters.immutable_result_conflicts += 1
            counters.note_variant(state.variant_id, apply.state)
            if apply.state == STATE_INVALID_SOURCE:
                counters.variants_invalid_source += 1

    def _apply_variant_result(
        self,
        *,
        state: VariantState,
        result: Any,
        asof_ms: int,
        grid: Any,
        candle_source: Mapping[str, Any] | None,
        counters: TelemetryCounters,
    ) -> None:
        window = int(state.policy.get("observation_window") or 1)
        window_elapsed = observation_window_elapsed(
            grid, observation_window=window, evaluation_asof_ms=asof_ms
        )
        coverage_attempts = state.coverage_attempts + (
            1 if result.status == STATE_INCOMPLETE_COVERAGE else 0
        )
        decision = decide_next_state(
            EvaluationObservation(
                current_state=state.state,
                pipeline_status=result.status,
                coverage_reason=str(result.reason or result.coverage_detail or ""),
                observed_window_candles=int(result.observed_window_candles),
                observation_window=window,
                window_elapsed=window_elapsed,
                coverage_attempts=max(1, coverage_attempts),
                coverage_finalize_after_attempts=(
                    self._config.coverage_finalize_after_attempts
                ),
            )
        )
        counters.variant_states_evaluated += 1
        apply = self._store.apply_evaluation(
            source_event_key=state.source_event_key,
            variant_id=state.variant_id,
            policy_fingerprint=state.policy_fingerprint,
            result_dict=result.to_dict(),
            decision=decision,
            evaluation_asof_ms=asof_ms,
            window_elapsed=window_elapsed,
            candle_source=candle_source,
        )
        if apply.conflict:
            counters.immutable_result_conflicts += 1
        counters.note_variant(state.variant_id, apply.state)
        self._count_terminal(counters, result, apply.state)

    @staticmethod
    def _count_terminal(counters: TelemetryCounters, result: Any, state: str) -> None:
        if state == STATE_CONFIRMED_DELAYED and result.status == STATE_CONFIRMED_DELAYED:
            counters.variants_confirmed_delayed += 1
            if result.entry_delay_ms is not None:
                counters.confirmation_delays_ms.append(int(result.entry_delay_ms))
        elif state == STATE_EXPIRED:
            counters.variants_expired += 1
        elif state == STATE_INCOMPLETE_COVERAGE:
            counters.variants_incomplete_coverage += 1
        elif state == STATE_INVALID_SOURCE:
            counters.variants_invalid_source += 1
        elif state == STATE_NOT_READY:
            counters.variants_not_ready += 1
