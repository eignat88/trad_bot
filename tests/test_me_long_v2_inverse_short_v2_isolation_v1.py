"""ME_LONG_V2_INVERSE_SHORT_V2 — V1/V2 isolation + observer contract tests.

Owner decision Q2 (variant B) under test: the entry-admissibility change is
a NEW experiment with its OWN fingerprint, activation, data namespace and
clean prospective holdout, while V1 keeps running byte-identical.

Covered acceptance criteria:
  * new protocol fingerprint pinned (and provably != the frozen V1 value);
  * frozen V2 execution spec carries entry_max_age_minutes = 120;
  * V1 fingerprint / freeze / tables / two-state event model unchanged;
  * separate activation flag + activation row + migration 087 additive;
  * namespaced position_key; no cross-version store/source references;
  * Grafana V2 dashboard exposes the required panels;
  * signal-level observer is read-only, creates no trades when the book is
    full, and has no look-ahead (stored rows + current clock only).

Everything runs against the in-memory store — no PostgreSQL in this suite.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings
from app.paper.me_long_v2_inverse_short_v1 import protocol as v1_protocol
from app.paper.me_long_v2_inverse_short_v1 import store as v1_store
from app.paper.me_long_v2_inverse_short_v1.models import (
    EVENT_STATUSES as V1_EVENT_STATUSES,
)
from app.paper.me_long_v2_inverse_short_v2 import (
    ENTRY_MAX_AGE_MINUTES,
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_CAPACITY_REJECTED,
    EVENT_STATUS_ENTERED,
    EVENT_STATUS_EXPIRED,
    EVENT_STATUSES,
    InMemoryInverseShortStore,
    InverseShortContour,
    SignalLevelProspectiveObserver,
    capture_with_status,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_v2 import (
    activation as v2_activation,
    models as v2_models,
    store as v2_store,
)
from app.paper.me_long_v2_inverse_short_v2.activation import (
    ACTIVATION_FLAG,
    evaluate_activation,
)
from app.paper.me_long_v2_inverse_short_v2.capture import CAPTURED
from app.paper.me_long_v2_inverse_short_v2.identity import (
    event_key as v2_event_key,
    position_key as v2_position_key,
)
from app.paper.me_long_v2_inverse_short_v2.protocol import (
    EXPERIMENT_ID as V2_EXPERIMENT_ID,
    ROUTE_PRE_FILTER_SOURCE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
    execution_spec,
)
from app.paper.me_long_v2_inverse_short_v1.identity import (
    event_key as v1_event_key,
)
from app.paper.me_long_v2_inverse_short_v1.activation import (
    ACTIVATION_FLAG as V1_ACTIVATION_FLAG,
)
from app.paper.me_long_v2_inverse_short_v1.protocol import (
    EXPERIMENT_ID as V1_EXPERIMENT_ID,
)
from app.paper.me_long_v2_inverse_short_v1.models import (
    ActivationRecord as V1ActivationRecord,
)
from app.scanners.models import SetupCandidate, SetupState

REPO_ROOT = Path(__file__).resolve().parents[1]

FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIGNAL_AT = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)

# The V2 fingerprint produced by the frozen execution spec below. If any
# spec field ever changes, this pin must fail loudly — a silent change of
# the research protocol is exactly what these tests exist to prevent.
PINNED_V2_PROTOCOL_FINGERPRINT = (
    "5a9047ca3621cefb4d53b45275ad6348887440568cb055f843f85e2eb1abdacc"
)
# The frozen V1 value — must stay byte-identical forever.
PINNED_V1_PROTOCOL_FINGERPRINT = (
    "4f97850478d65360a8a9500633d98cde5d8cb476c7a755fe05731c3748ab163c"
)


# ---------------------------------------------------------------------------
# Local helpers (V2 identity)
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


def _settings(**overrides) -> Settings:
    defaults = dict(
        initial_balance=10_000.0,
        risk_per_trade=0.005,
        max_symbol_exposure=0.20,
        max_open_positions=3,
        taker_fee=0.00055,
        slippage_percent=0.0005,
        paper_funding_interval_hours=8,
        setup_ttl_multiplier=2.0,
    )
    defaults.update(overrides)
    return dataclasses.replace(Settings(), **defaults)


def _v2_activation(
    status: str = "RUNNING",
    fingerprint: str | None = None,
    freeze_ts: datetime | None = FREEZE_TS,
) -> v2_models.ActivationRecord:
    return v2_models.ActivationRecord(
        experiment_id=V2_EXPERIMENT_ID,
        status=status,
        protocol_fingerprint=(
            fingerprint if fingerprint is not None else protocol_fingerprint()
        ),
        freeze_ts=freeze_ts,
    )


def _store(activation=None) -> InMemoryInverseShortStore:
    if activation is None:
        activation = _v2_activation()
    return InMemoryInverseShortStore(
        activation, initial_balance=_settings().initial_balance,
    )


def _candidate(
    symbol: str = "BTCUSDT",
    price: float = 100.0,
    *,
    detected_at: datetime = SIGNAL_AT,
) -> SetupCandidate:
    candle_ms = int(detected_at.timestamp() // 300) * 300_000
    return SetupCandidate(
        scanner_name=SOURCE_SCANNER,
        scanner_version="1.0.0",
        symbol=symbol,
        direction=SOURCE_DIRECTION,
        entry_timeframe="5m",
        detected_at=detected_at,
        setup_started_at=detected_at,
        signal_candle_open_time=candle_ms,
        reference_price=price,
        entry_zone_low=price * 0.998,
        entry_zone_high=price * 1.001,
        invalidation_price=price * 0.975,
        target_1=price * 1.03,
        score=45.0,
        market_regime="RANGE",
        state=SetupState.SETUP_READY,
        features={"exhaustion_magnitude": 0.01},
    )


def _capture(store, candidate, *, clock, env=None) -> str:
    return capture_with_status(
        store, candidate, capture_route=ROUTE_PRE_FILTER_SOURCE,
        env={ACTIVATION_FLAG: "1"} if env is None else env,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# 1. Frozen protocol fingerprint + frozen spec fields
# ---------------------------------------------------------------------------

class TestV2Fingerprint:
    def test_v2_fingerprint_is_pinned(self):
        assert protocol_fingerprint() == PINNED_V2_PROTOCOL_FINGERPRINT
        assert len(protocol_fingerprint()) == 64           # sha256-shaped

    def test_v1_fingerprint_is_still_the_frozen_v1_value(self):
        assert v1_protocol.protocol_fingerprint() == (
            PINNED_V1_PROTOCOL_FINGERPRINT
        )

    def test_v2_fingerprint_differs_from_v1(self):
        # Q2: the entry-admissibility change moved the protocol fingerprint
        # — V1 and V2 can never share an activation row or a holdout.
        assert protocol_fingerprint() != v1_protocol.protocol_fingerprint()

    def test_execution_spec_carries_entry_ttl_120(self):
        spec = execution_spec()
        assert spec["entry_max_age_minutes"] == 120
        assert spec["entry_max_age_minutes"] == ENTRY_MAX_AGE_MINUTES
        # frozen V1 spec has NO entry-admissibility field at all
        assert "entry_max_age_minutes" not in v1_protocol.execution_spec()

    def test_spec_change_would_move_the_fingerprint(self):
        # sanity: the TTL really is inside the hashed payload
        import hashlib
        import json as _json

        def _canonical(spec: dict) -> str:
            return _json.dumps(
                spec, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            )

        baseline = _canonical(execution_spec())
        mutated = dict(execution_spec())
        mutated["entry_max_age_minutes"] = 119
        assert baseline != _canonical(mutated)
        assert hashlib.sha256(baseline.encode("utf-8")).hexdigest() == (
            protocol_fingerprint()
        )
        assert hashlib.sha256(
            _canonical(mutated).encode("utf-8"),
        ).hexdigest() != protocol_fingerprint()


# ---------------------------------------------------------------------------
# 2. Activation / identity isolation
# ---------------------------------------------------------------------------

class TestActivationIsolation:
    def test_flag_is_v2_only(self):
        assert ACTIVATION_FLAG == "ME_INVERSE_SHORT_V2_ENABLED"
        assert V1_ACTIVATION_FLAG == "ME_INVERSE_SHORT_V1_ENABLED"
        assert ACTIVATION_FLAG != V1_ACTIVATION_FLAG

    def test_experiment_ids_differ(self):
        assert V2_EXPERIMENT_ID == "ME_LONG_V2_INVERSE_SHORT_V2"
        assert V1_EXPERIMENT_ID == "ME_LONG_V2_INVERSE_SHORT_V1"
        assert V2_EXPERIMENT_ID != V1_EXPERIMENT_ID

    def test_v2_activation_module_rejects_a_v1_record(self):
        identity, reason = evaluate_activation(
            V1ActivationRecord(
                experiment_id=V1_EXPERIMENT_ID,
                status="RUNNING",
                protocol_fingerprint=PINNED_V1_PROTOCOL_FINGERPRINT,
                freeze_ts=FREEZE_TS,
            ),
            enabled=True,
        )
        assert identity is None
        assert reason == "ACTIVATION_ROW_MISSING"  # wrong experiment → closed

    def test_v2_activation_requires_the_v2_fingerprint(self):
        identity, reason = evaluate_activation(
            _v2_activation(fingerprint="0" * 64), enabled=True,
        )
        assert identity is None
        assert reason == "PROTOCOL_FINGERPRINT_MISMATCH"
        identity, reason = evaluate_activation(_v2_activation(), enabled=True)
        assert identity is not None
        assert identity.mode == "FULL"

    def test_v2_activation_rejects_ready_to_start(self):
        identity, reason = evaluate_activation(
            _v2_activation(status="READY_TO_START", fingerprint=None,
                           freeze_ts=None),
            enabled=True,
        )
        assert identity is None
        assert reason in {"ACTIVATION_NOT_RUNNING", "ACTIVATION_ROW_MISSING"}


# ---------------------------------------------------------------------------
# 3. Store / data namespace isolation
# ---------------------------------------------------------------------------

class TestStoreIsolation:
    def test_v2_store_tables_are_v2_only(self):
        for name in (v2_store.EVENT_TABLE, v2_store.POSITION_TABLE,
                     v2_store.ACTIVATION_TABLE, v2_store.CLOSE_OUTBOX_TABLE):
            assert "me_long_v2_inverse_short_v2" in name, name
            assert "_v1_" not in name, name

    def test_v2_store_source_never_mentions_v1_tables(self):
        source = (Path(v2_store.__file__)).read_text(encoding="utf-8")
        assert "me_long_v2_inverse_short_v1" not in source

    def test_v1_store_source_untouched_still_points_at_v1(self):
        assert "me_long_v2_inverse_short_v1" in v1_store.EVENT_TABLE
        source = (Path(v1_store.__file__)).read_text(encoding="utf-8")
        assert "me_long_v2_inverse_short_v2" not in source

    def test_v2_position_keys_are_namespaced(self):
        key = v2_position_key("deadbeefcafe")
        assert key == "ME_LONG_V2_INVERSE_SHORT_V2:deadbeefcafe"
        # the same event_key fed through the V1 namespace never collides
        assert key != f"ME_LONG_V2_INVERSE_SHORT_V1:deadbeefcafe"

    def test_event_keys_are_deterministic_and_versioned(self):
        cand = _candidate()
        # deterministic: the same source event redelivered maps to one row
        assert v2_event_key(cand) == v2_event_key(cand)
        assert len(v2_event_key(cand)) == 64
        # the V2 identity payload embeds the V2 experiment id, so a V1
        # event and a V2 event for the same source candle can never share
        # a row key across ledgers
        assert v2_event_key(cand) != v1_event_key(cand)

    def test_v2_event_statuses_are_four_state(self):
        assert EVENT_STATUSES == (
            "CAPTURED", "ENTERED", "EXPIRED", "CAPACITY_REJECTED",
        )
        # V1 stays two-state — untouched
        assert V1_EVENT_STATUSES == ("CAPTURED", "ENTERED")
        # ENTERED/EXPIRED/CAPACITY_REJECTED never return to CAPTURED
        assert v2_models.EVENT_TERMINAL_STATUSES == (
            v2_models.EVENT_STATUS_ENTERED,
            v2_models.EVENT_STATUS_EXPIRED,
            v2_models.EVENT_STATUS_CAPACITY_REJECTED,
        )
        assert not hasattr(v1_protocol, "ENTRY_MAX_AGE_MINUTES")


# ---------------------------------------------------------------------------
# 4. Migration 087: additive, V2-only, four-state
# ---------------------------------------------------------------------------

class TestMigration087:
    @property
    def sql(self) -> str:
        path = REPO_ROOT / "sql" / "migrations" / "087_me_long_v2_inverse_short_v2.sql"
        assert path.is_file(), f"missing migration: {path}"
        return path.read_text(encoding="utf-8")

    @property
    def executable_lines(self) -> list[str]:
        out = []
        for line in self.sql.splitlines():
            code = line.split("--", 1)[0].strip()
            if code:
                out.append(code)
        return out

    def test_creates_all_four_v2_tables(self):
        body = "\n".join(self.executable_lines)
        for table in ("activation", "event", "position", "close_outbox"):
            assert f"CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_v2_{table}" in body
        assert body.count("CREATE TABLE") == 4

    def test_never_touches_v1_or_production_tables(self):
        body = "\n".join(self.executable_lines).upper()
        assert "_V1_" not in body                       # no V1 object at all
        assert "DROP " not in body
        assert "TRUNCATE" not in body
        assert "ALTER TABLE" not in body
        # no production/research writes either
        for forbidden in ("DDS.PAPER_TRADE", "DDS.SIGNAL_OUTCOME",
                          "RESEARCH."):
            assert forbidden not in body

    def test_four_state_check_and_refusal_audit_columns(self):
        body = "\n".join(self.executable_lines)
        assert "'CAPTURED', 'ENTERED', 'EXPIRED', 'CAPACITY_REJECTED'" in body
        assert "status_reason" in body and "status_at" in body
        assert "'ENTRY_TTL'" in body and "'ENTRY_CAP'" in body
        # terminal refusal requires cause + timestamp
        assert "status_reason IS NOT NULL AND status_at IS NOT NULL" in body

    def test_seeded_fail_closed_activation_and_no_backfill_check(self):
        body = "\n".join(self.executable_lines)
        assert "VALUES ('ME_LONG_V2_INVERSE_SHORT_V2', 'READY_TO_START')" in body
        assert "signal_time > activation_freeze_ts" in body

    def test_guard_trigger_is_namespaced(self):
        body = "\n".join(self.executable_lines)
        assert "fn_me_long_v2_inverse_short_v2_guard" in body
        assert "trg_me_long_v2_inverse_short_v2_guard" in body
        assert "ME_LONG_V2_INVERSE_SHORT_V2:%" in body


# ---------------------------------------------------------------------------
# 5. Grafana dashboard: required panels, V2 namespace only
# ---------------------------------------------------------------------------

class TestGrafanaDashboard:
    @property
    def dashboard(self) -> dict:
        path = (REPO_ROOT / "monitoring" / "grafana" / "dashboards"
                / "me-inverse-short-v2-oos.json")
        assert path.is_file(), f"missing dashboard: {path}"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_dashboard_identity_and_required_panels(self):
        dash = self.dashboard
        assert dash["uid"] == "me-inverse-short-v2-oos"
        titles = [p["title"] for p in dash["panels"]]
        assert any("Captured" in t for t in titles)
        assert any("Entered" in t for t in titles)
        assert any("Expired" in t for t in titles)
        assert any("Capacity Rejected" in t for t in titles)
        assert any("Open Positions" in t for t in titles)
        assert any("Finalized" in t for t in titles)

    def test_every_query_targets_v2_tables_only(self):
        queries = [
            t["rawSql"]
            for p in self.dashboard["panels"]
            for t in p.get("targets", [])
        ]
        assert queries, "dashboard has no queries"
        for q in queries:
            assert "me_long_v2_inverse_short_v2_" in q, q
            assert "_v1_" not in q, q

    def test_disposition_counts_are_observable(self):
        queries = " ".join(
            t["rawSql"]
            for p in self.dashboard["panels"]
            for t in p.get("targets", [])
        )
        for status in ("CAPTURED", "ENTERED", "EXPIRED", "CAPACITY_REJECTED"):
            assert f"status='{status}'" in queries, status


# ---------------------------------------------------------------------------
# 6. Signal-level prospective observer: read-only, no look-ahead
# ---------------------------------------------------------------------------

class TestSignalLevelObserver:
    def _scenario(self):
        """3 open + 1 capacity-rejected (stale) + 1 entered-closed book."""
        settings = _settings()
        store = _store()
        clock = _Clock(SIGNAL_AT)
        contour = InverseShortContour(
            settings, store, clock=clock, activation_enabled=True,
        )
        # three fresh signals open the book to the cap
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            assert _capture(store, _candidate(symbol), clock=clock) == CAPTURED
        cycle = contour.run_cycle(
            {s: 100.0 for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")},
        )
        assert cycle["entries"] == 3, cycle
        # one more FRESH signal stays CAPTURED (cap full)
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES + 1)
        assert _capture(store, _candidate("XRPUSDT", detected_at=clock()),
                        clock=clock) == CAPTURED
        # and one that is already past the TTL goes terminal while the
        # book is still full
        assert _capture(store, _candidate(
            "DOGEUSDT",
            detected_at=clock() - timedelta(minutes=ENTRY_MAX_AGE_MINUTES + 1),
        ), clock=clock) == CAPTURED
        cycle = contour.run_cycle(
            {s: 100.0 for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT",
                                "XRPUSDT", "DOGEUSDT")},
        )
        assert cycle["entries"] == 0, cycle
        return store, clock, contour

    def test_observer_is_read_only_and_creates_no_trades_when_cap_full(self):
        store, clock, contour = self._scenario()
        positions_before = [
            (p.position_key, p.status) for p in store.list_positions()
        ]
        events_before = [
            (e.event_key, e.status) for e in store.list_events()
        ]

        observer = SignalLevelProspectiveObserver(store, clock=clock)
        metrics = observer.observe()

        assert metrics["read_only"] is True
        # nothing changed: no positions, no event dispositions, no entries
        assert [
            (p.position_key, p.status) for p in store.list_positions()
        ] == positions_before
        assert [
            (e.event_key, e.status) for e in store.list_events()
        ] == events_before
        assert len(contour.engine.open_positions) == 3

        # the metrics themselves describe the scenario correctly
        assert metrics["open_positions"] == 3
        assert metrics["events_total"] == 5
        assert metrics["events_by_status"]["CAPTURED"] == 1   # XRP fresh
        assert metrics["capacity_rejected"] == 1              # DOGE stale
        assert metrics["eligible_pending"] == 1
        assert metrics["stale_unswept"] == 0                  # already disposed
        assert metrics["finalized_outcomes"] == 0

    def test_observer_source_is_structurally_read_only(self):
        import app.paper.me_long_v2_inverse_short_v2.observer as obs_module
        source = Path(obs_module.__file__).read_text(encoding="utf-8")
        # never CALLS any mutation path (the observer's own docstring may
        # legitimately name them while explaining what it does not do)
        for forbidden in (".open_entries", "._open_event", ".save_position",
                          ".update_position", ".stage_close",
                          ".close_position", ".mark_event_entered",
                          ".mark_event_expired", ".capture_with_status",
                          ".run_cycle", "InverseShortContour("):
            assert forbidden not in source, forbidden
        assert not hasattr(obs_module, "InverseShortContour")

    def test_observer_has_no_lookahead_across_clock_moves(self):
        store, clock, contour = self._scenario()
        observer = SignalLevelProspectiveObserver(store, clock=clock)
        first = observer.observe()

        # move the clock forward WITHOUT touching any stored row: entry
        # latency (stored facts) must not change; only age-based eligibility
        # may shrink — that is "now", not future data.
        clock.advance(minutes=10)
        second = observer.observe()

        assert first["entry_latency_minutes"] == second["entry_latency_minutes"]
        assert first["entered"] == second["entered"]
        assert first["events_by_status"] == second["events_by_status"]
        assert first["finalized_net_pnl"] == second["finalized_net_pnl"]
        # no event ever became stale from a clock move within the TTL
        assert second["expired"] == first["expired"]

        statuses_after = {e.event_key: e.status for e in store.list_events()}
        assert all(
            s in EVENT_STATUSES for s in statuses_after.values()
        )
        assert statuses_after == {e.event_key: e.status
                                  for e in store.list_events()}

    def test_observer_dispositions_expose_reason_and_timestamp(self):
        store, clock, contour = self._scenario()
        rows = SignalLevelProspectiveObserver(
            store, clock=clock,
        ).dispositions()
        by_symbol = {r["symbol"]: r for r in rows}
        doge = by_symbol["DOGEUSDT"]
        assert doge["status"] == EVENT_STATUS_CAPACITY_REJECTED
        assert doge["status_reason"] == "ENTRY_CAP"
        assert doge["status_at"] is not None
        assert doge["age_minutes"] >= ENTRY_MAX_AGE_MINUTES
        assert doge["eligible_now"] is False
        xrp = by_symbol["XRPUSDT"]
        assert xrp["status"] == EVENT_STATUS_CAPTURED
        assert xrp["eligible_now"] is True
        assert xrp["age_minutes"] < ENTRY_MAX_AGE_MINUTES

    def test_observer_after_expiry_reports_expired_disposition(self):
        settings = _settings()
        store = _store()
        clock = _Clock(SIGNAL_AT)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES)
        contour = InverseShortContour(
            settings, store, clock=clock, activation_enabled=True,
        )
        assert contour.run_cycle({"BTCUSDT": 100.0})["entries"] == 0
        metrics = SignalLevelProspectiveObserver(store, clock=clock).observe()
        assert metrics["expired"] == 1
        assert metrics["events_by_status"][EVENT_STATUS_EXPIRED] == 1
        row = next(r for r in SignalLevelProspectiveObserver(
            store, clock=clock,
        ).dispositions() if r["symbol"] == "BTCUSDT")
        assert row["status_reason"] == "ENTRY_TTL"
        assert row["status_at"] is not None


# ---------------------------------------------------------------------------
# 7. V1 must be untouched (behavioural smoke on the V1 contour)
# ---------------------------------------------------------------------------

class TestV1Untouched:
    def test_v1_contour_still_enters_and_is_two_state(self):
        from app.paper.me_long_v2_inverse_short_v1 import (
            InMemoryInverseShortStore as V1Store,
            InverseShortContour as V1Contour,
        )
        from app.paper.me_long_v2_inverse_short_v1.activation import (
            ACTIVATION_FLAG as FLAG,
        )
        from app.paper.me_long_v2_inverse_short_v1.capture import (
            capture_with_status as v1_capture,
        )
        from app.paper.me_long_v2_inverse_short_v1.protocol import (
            ROUTE_PRE_FILTER_SOURCE as V1_ROUTE,
            SOURCE_DIRECTION as V1_DIR,
            SOURCE_SCANNER as V1_SCANNER,
        )

        activation = V1ActivationRecord(
            experiment_id=V1_EXPERIMENT_ID,
            status="RUNNING",
            protocol_fingerprint=PINNED_V1_PROTOCOL_FINGERPRINT,
            freeze_ts=FREEZE_TS,
        )
        settings = _settings()
        store = V1Store(activation, initial_balance=settings.initial_balance)
        clock = _Clock(SIGNAL_AT)
        contour = V1Contour(
            settings, store, clock=clock, activation_enabled=True,
        )
        cand = SetupCandidate(
            scanner_name=V1_SCANNER,
            scanner_version="1.0.0",
            symbol="BTCUSDT",
            direction=V1_DIR,
            entry_timeframe="5m",
            detected_at=SIGNAL_AT,
            setup_started_at=SIGNAL_AT,
            signal_candle_open_time=int(SIGNAL_AT.timestamp() // 300) * 300_000,
            reference_price=100.0,
            entry_zone_low=99.8,
            entry_zone_high=100.1,
            invalidation_price=97.5,
            target_1=103.0,
            score=45.0,
            market_regime="RANGE",
            state=SetupState.SETUP_READY,
            features={},
        )
        status = v1_capture(
            store, cand, capture_route=V1_ROUTE, env={FLAG: "1"}, clock=clock,
        )
        assert status == CAPTURED
        # V1 has NO TTL: a 10-hour-old event still enters (old contract)
        clock.advance(minutes=600)
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1, cycle          # ← V1 behaviour intact
        event = store.list_events()[0]
        assert event.status == "ENTERED"             # V1 two-state model
        assert not hasattr(event, "status_reason") or (
            getattr(event, "status_reason", None) is None
        )
