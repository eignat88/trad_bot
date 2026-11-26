"""Issue #312 — H2 prospective runtime: source reader tests (FR-01).

Acceptance criteria covered:

* 1  — a new valid #305 event is accepted exactly once;
* 2  — repeated delivery is an idempotent DUPLICATE (no second outcome);
* 3  — a pre-freeze event is rejected (strict ``signal_time > freeze_ts``);
* 4  — no source events -> ``SOURCE_EMPTY_WAITING`` (expected state);
* 15 — lineage integrity: a stored ``lineage_hash`` mismatch is an explicit
  rejection and a redelivery with drifted immutable lineage is an explicit
  ``EVENT_KEY_LINEAGE_CONFLICT`` — never a silent overwrite;
* 18 — production isolation: the reader is structurally SELECT-only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.research.me_entry_quality_h2_prospective_v1.contracts import (
    INGEST_ACCEPTED,
    INGEST_REJECTED_INVALID,
    INGEST_REJECTED_LINEAGE,
    INGEST_REJECTED_PRE_FREEZE,
    REASON_EVENT_KEY_LINEAGE_CONFLICT,
    REASON_LINEAGE_HASH_MISMATCH,
    REASON_PRE_FREEZE_SIGNAL_TIME,
    REASON_SOURCE_ROW_PARSE_ERROR,
    RUNTIME_SOURCE_EMPTY_WAITING,
)
from app.research.me_entry_quality_h2_prospective_v1.config import (
    H2ProspectiveConfig,
)
from app.research.me_entry_quality_h2_prospective_v1.runtime import (
    H2ProspectiveRuntime,
)
from app.research.me_entry_quality_h2_prospective_v1.source_reader import (
    SOURCE_EVENT_COLUMNS,
    assert_select_only,
    cursor_of,
    delivery_from_row,
    row_mapping,
)
from app.research.me_entry_quality_h2_prospective_v1.store import (
    InMemoryProspectiveStore,
)
from app.research.me_entry_quality_h2_prospective_v1.telemetry import (
    SOURCE_EMPTY_WAITING_LOG,
)
from app.research.me_upstream_source_v1.contracts import lineage_hash

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
BASE_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z


def make_source_row(
    *,
    key: str = "evt-1",
    signal_ms: int = BASE_MS + 300_000,
    freeze_before_ms: int = 3_600_000,
    captured_after_ms: int = 20_000,
    identity: str = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|BTCUSDT|LONG|5m|1",
    lineage_tweak: dict | None = None,
) -> dict:
    """A well-formed #305 ``source_event`` row mapping (valid lineage hash)."""
    signal = EPOCH + timedelta(milliseconds=signal_ms)
    freeze = EPOCH + timedelta(milliseconds=signal_ms - freeze_before_ms)
    captured = EPOCH + timedelta(milliseconds=signal_ms + captured_after_ms)
    lineage = {
        "source_identity": identity,
        "source_signal_time": signal.isoformat(),
        "source_signal_candle_open_time": signal_ms // 300_000 * 300_000,
        "source_scanner": "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        "source_direction": "LONG",
        "source_features": {"rsi_14": 71.25},
        "source_pipeline_fingerprint": "fp-test-1",
    }
    lineage.update(lineage_tweak or {})
    return {
        "source_event_key": key,
        "source_identity": identity,
        "symbol": "BTCUSDT",
        "signal_time": signal,
        "captured_at": captured,
        "activation_freeze_ts": freeze,
        "signal_candle_open_time": signal_ms // 300_000 * 300_000,
        "entry_timeframe": "5m",
        "source_fingerprint": "fp-test-1",
        "source_reference_price": 1234.5,
        "features": {
            "rsi_14": 71.25,
            "rsi_delta_3": 4.5,
            "entry_confirmation_rsi_rising": True,
        },
        "lineage": lineage,
        "lineage_hash": lineage_hash(lineage),
    }


class TestIngestChecks:
    def test_valid_event_is_accepted(self):
        delivery = delivery_from_row(make_source_row())
        assert delivery.status == INGEST_ACCEPTED
        assert delivery.source_event_key == "evt-1"
        assert delivery.immutable_hash
        assert delivery.row is not None

    def test_pre_freeze_event_is_rejected(self):
        # criterion 3: signal exactly AT the freeze is already inadmissible
        row = make_source_row(freeze_before_ms=0)
        delivery = delivery_from_row(row)
        assert delivery.status == INGEST_REJECTED_PRE_FREEZE
        assert delivery.reason == REASON_PRE_FREEZE_SIGNAL_TIME

    def test_pre_freeze_captured_at_is_rejected(self):
        row = make_source_row(freeze_before_ms=3_600_000, captured_after_ms=-3_700_000)
        delivery = delivery_from_row(row)
        assert delivery.status == INGEST_REJECTED_PRE_FREEZE

    def test_lineage_hash_mismatch_is_explicit_integrity_rejection(self):
        row = make_source_row()
        row["lineage_hash"] = "00" * 32
        delivery = delivery_from_row(row)
        assert delivery.status == INGEST_REJECTED_LINEAGE
        assert delivery.reason == REASON_LINEAGE_HASH_MISMATCH

    def test_malformed_row_is_rejected_not_dropped(self):
        delivery = delivery_from_row({"source_event_key": ""})
        assert delivery.status == INGEST_REJECTED_INVALID
        assert delivery.reason == REASON_SOURCE_ROW_PARSE_ERROR

    def test_identity_fields_are_carried_verbatim(self):
        row = make_source_row()
        delivery = delivery_from_row(row)
        assert delivery.source_identity == row["source_identity"]
        assert delivery.lineage_hash == row["lineage_hash"]
        assert delivery.signal_time_us == (
            int((row["signal_time"] - EPOCH) // timedelta(microseconds=1))
        )


class TestStoreIngestIdempotency:
    def test_accepted_once_then_idempotent_duplicate(self):
        # criteria 1 + 2
        store = InMemoryProspectiveStore()
        row = make_source_row()
        first = store.ingest_event(delivery_from_row(row))
        assert first.status == INGEST_ACCEPTED and not first.duplicate
        second = store.ingest_event(delivery_from_row(make_source_row()))
        assert second.duplicate
        assert second.status == INGEST_ACCEPTED  # first-seen snapshot kept
        assert len(store.ingest_rows) == 1
        assert store.results == {}  # no outcome recreated on redelivery

    def test_redelivery_lineage_drift_is_explicit_conflict(self):
        # criterion 15
        store = InMemoryProspectiveStore()
        store.ingest_event(delivery_from_row(make_source_row()))
        drifted = delivery_from_row(
            make_source_row(lineage_tweak={"source_scanner_version": "v2"})
        )
        outcome = store.ingest_event(drifted)
        assert outcome.conflict
        assert outcome.status == INGEST_REJECTED_LINEAGE
        assert outcome.reason == REASON_EVENT_KEY_LINEAGE_CONFLICT
        assert store.conflicts
        assert store.conflicts[0]["conflict_kind"] == "EVENT_KEY_LINEAGE_CONFLICT"
        # first-seen snapshot preserved verbatim
        assert (
            store.ingest_rows["evt-1"]["signature"]["lineage_hash"]
            == make_source_row()["lineage_hash"]
        )


class TestSelectOnlyGuard:
    def test_reader_is_structurally_select_only(self):
        import inspect

        import app.research.me_entry_quality_h2_prospective_v1.source_reader as mod

        assert_select_only("SELECT 1")
        for bad in ("INSERT INTO t VALUES (1)", "UPDATE t SET a = 1", "DELETE FROM t"):
            try:
                assert_select_only(bad)
            except ValueError:
                pass
            else:  # pragma: no cover - fail loudly
                raise AssertionError(f"non-SELECT accepted: {bad}")
        # structural check: every statement constant in the reader is a SELECT
        for name, value in vars(mod).items():
            if name.endswith("_SQL") and isinstance(value, str):
                assert value.strip().upper().startswith("SELECT"), name

    def test_row_mapping_carries_jsonb_columns(self):
        row = make_source_row()
        values = [row.get(col) for col in SOURCE_EVENT_COLUMNS]
        mapped = row_mapping(values)
        assert mapped["source_event_key"] == "evt-1"
        assert isinstance(mapped["lineage"], dict)
        assert isinstance(mapped["features"], dict)

    def test_row_mapping_rejects_wrong_width(self):
        try:
            row_mapping([1, 2, 3])
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("wrong-width row accepted")

    def test_cursor_is_the_batch_tail(self):
        # review P1-watermark: the cursor tracks the CAPTURE order
        rows = [make_source_row(key=f"e{i}") for i in range(3)]
        us, key = cursor_of(rows)
        assert key == "e2"
        assert us > 0
        assert cursor_of([]) == (0, "")


class TestSourcePriceValidation:
    """Review P1-Decimal: strict, exact, type-safe source prices."""

    def test_decimal_price_is_delivered_exact(self):
        from decimal import Decimal

        row = make_source_row()
        row["source_reference_price"] = Decimal("123.4567890123456789")
        delivery = delivery_from_row(row)
        assert delivery.status == INGEST_ACCEPTED
        assert delivery.row["source_reference_price"] == Decimal(
            "123.4567890123456789"
        )

    def test_invalid_prices_are_rejected_not_coerced(self):
        for bad in (
            float("nan"),
            float("inf"),
            float("-inf"),
            0,
            0.0,
            -1,
            -0.5,
            "abc",
            "12,34",
            "1e5",
            " 12 ",
            "",
            None,
            True,
            [1.0],
        ):
            row = make_source_row()
            row["source_reference_price"] = bad
            delivery = delivery_from_row(row)
            assert delivery.status == INGEST_REJECTED_INVALID, bad
            assert delivery.reason == "INVALID_SOURCE_PRICE", bad

    def test_plain_numeric_strings_are_parsed_exactly(self):
        from decimal import Decimal

        row = make_source_row()
        row["source_reference_price"] = "123.4567890123456789"
        delivery = delivery_from_row(row)
        assert delivery.status == INGEST_ACCEPTED
        assert delivery.row["source_reference_price"] == Decimal(
            "123.4567890123456789"
        )


class TestSourceEmptyWaiting:
    def test_no_source_events_logs_source_empty_waiting(self, caplog):
        # criterion 4: expected state, not an algorithm error
        store = InMemoryProspectiveStore()
        config = H2ProspectiveConfig(enabled=True)
        from app.research.me_entry_quality_h2_prospective_v1.candle_adapter import (
            BybitClosedCandleProvider,
            CandleApiBudget,
        )

        provider = BybitClosedCandleProvider(
            budget=CandleApiBudget(),
            enabled=True,
            client_factory=_MustNotFetch,
        )
        runtime = H2ProspectiveRuntime(
            config=config,
            store=store,
            candle_provider=provider,
            source_connection_factory=_EmptySourceConnection,
        )
        with caplog.at_level(logging.INFO):
            report = runtime.run_cycle()
        assert report.status == RUNTIME_SOURCE_EMPTY_WAITING
        assert SOURCE_EMPTY_WAITING_LOG in caplog.text
        assert report.details.get("source_empty_waiting") is True
        # no source events means no candle work at all
        assert provider.budget.api_requests == 0


class _EmptyCursor:
    def execute(self, sql, params=()):
        assert_select_only(sql)

    def fetchall(self):
        return []

    def close(self):
        return None


class _EmptySourceConnection:
    def cursor(self):
        return _EmptyCursor()

    def close(self):
        return None


def _MustNotFetch():
    raise AssertionError("Bybit must never be called without source events")
