"""ME_LONG_V2_INVERSE_SHORT_V2 — P1-1 production source-path wiring.

Requirement 3.4 (diagnostic control): V2 capture must be demonstrably on
the REAL prospective-only publication chain

    ScannerOrchestrator.scan_all_with_stats  (pre-gate publication)
        -> stats["_me_inverse_short_source_events"]
        -> scanner_runner production loop
        -> _capture_me_inverse_short_v2_source  (V2 hook, fail-open)
        -> capture_with_status(ROUTE_PRE_FILTER_SOURCE)
        -> PostgreSQL (V2 event ledger)

— not merely an importable module.  This suite proves:

* the shared static audit (``audit_source_path``) passes on the checked-out
  sources AND has NEGATIVE controls: it detects a removed V2 delivery, a
  removed V1 delivery, a publication moved behind the gate and missing
  source files (an audit that cannot fail proves nothing);
* the runtime diagnostic ``scanner_runner.me_inverse_short_v2_source_wiring()``
  reports the production path plus runtime delivery counters;
* the V2 hook's runtime behaviour: deliveries counted, flag-off = zero DB
  touch, store-unavailable = ``ME_INVERSE_SHORT_V2_SOURCE_CAPTURE_ERROR``
  sink (never a scanner failure), success = CAPTURED into the V2 ledger,
  idempotent on re-delivery;
* V1/V2 independence: own flag, own store state, own error sink — a V2
  failure never touches V1 state and the V2 hook never reads the V1 flag;
* V1 byte-identity against the pre-change baseline snapshot.

No PostgreSQL is required by this suite.
"""
from __future__ import annotations

import ast
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.paper.me_long_v2_inverse_short_v2.activation import ACTIVATION_FLAG
from app.paper.me_long_v2_inverse_short_v2.models import ActivationRecord
from app.paper.me_long_v2_inverse_short_v2.protocol import (
    EXPERIMENT_ID,
    SOURCE_SCANNER,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_v2.source_path import audit_source_path
from app.paper.me_long_v2_inverse_short_v2.store import (
    InMemoryInverseShortStore,
)
from app.scanners.models import SetupCandidate, SetupState

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_ROOT = PROJECT_ROOT / "baseline_repo"

SIGNAL_AT = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)

V1_FLAG = "ME_INVERSE_SHORT_V1_ENABLED"
V1_ERROR_TYPE = "ME_INVERSE_SHORT_SOURCE_CAPTURE_ERROR"
V2_ERROR_TYPE = "ME_INVERSE_SHORT_V2_SOURCE_CAPTURE_ERROR"

V1_PACKAGE = "app/paper/me_long_v2_inverse_short_v1"
SOURCE_FILES = (
    "scanner_runner.py",
    "app/scanners/orchestrator.py",
    "app/paper/me_long_v2_inverse_short_v2/capture.py",
    "app/paper/me_long_v2_inverse_short_v2/engine.py",
    "app/paper/me_long_v2_inverse_short_v2/protocol.py",
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _candidate(
    symbol: str = "BTCUSDT",
    scanner_name: str = SOURCE_SCANNER,
    direction: str = "LONG",
) -> SetupCandidate:
    candle_ms = int(SIGNAL_AT.timestamp() // 300) * 300_000
    return SetupCandidate(
        scanner_name=scanner_name,
        scanner_version="1.0.0",
        symbol=symbol,
        direction=direction,
        entry_timeframe="5m",
        detected_at=SIGNAL_AT,
        setup_started_at=SIGNAL_AT,
        signal_candle_open_time=candle_ms,
        reference_price=100.0,
        entry_zone_low=99.8,
        entry_zone_high=100.1,
        invalidation_price=97.5,
        target_1=103.0,
        score=45.0,
        reasons=("stub fixture",),
        market_regime="TREND_UP",
        state=SetupState.SETUP_READY,
        features={"exhaustion_magnitude": 0.01, "rsi_14": 70.0},
    )


def _activation() -> ActivationRecord:
    return ActivationRecord(
        experiment_id=EXPERIMENT_ID,
        status="RUNNING",
        protocol_fingerprint=protocol_fingerprint(),
        freeze_ts=FREEZE_TS,
    )


class _Repository:
    """Fake scanner repository: only the error sink is used."""

    def __init__(self) -> None:
        self.errors: list[dict] = []
        self._host = "never-used"
        self._port = 0
        self._database = "never-used"
        self._user = "never-used"
        self._password = "never-used"
        self._unix_sock = None

    def save_error(self, **kwargs) -> None:
        self.errors.append(kwargs)


@pytest.fixture
def runner_module():
    """Import scanner_runner with its V1/V2 capture state reset."""
    import scanner_runner as mod

    saved = (
        mod._me_inverse_short_v2_store,
        mod._me_inverse_short_v2_store_retry_at,
        dict(mod._me_inverse_short_v2_source_stats),
        mod._me_inverse_short_store,
        mod._me_inverse_short_store_retry_at,
    )
    mod._me_inverse_short_v2_store = None
    mod._me_inverse_short_v2_store_retry_at = 0.0
    mod._me_inverse_short_v2_source_stats.update(
        {key: 0 for key in mod._me_inverse_short_v2_source_stats},
    )
    yield mod
    (
        mod._me_inverse_short_v2_store,
        mod._me_inverse_short_v2_store_retry_at,
        stats,
        mod._me_inverse_short_store,
        mod._me_inverse_short_store_retry_at,
    ) = saved
    mod._me_inverse_short_v2_source_stats.clear()
    mod._me_inverse_short_v2_source_stats.update(stats)


@pytest.fixture
def repository():
    return _Repository()


def _v2_store() -> InMemoryInverseShortStore:
    return InMemoryInverseShortStore(_activation())


def _copy_sources(tmp_path: Path) -> Path:
    """Minimal source tree for the negative-control audits."""
    for rel in SOURCE_FILES:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((PROJECT_ROOT / rel).read_bytes())
    return tmp_path


def _function_source(path: Path, name: str) -> str:
    """Byte-exact source of one top-level function (AST line range)."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            lines = source.splitlines(keepends=True)
            return "".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"function {name!r} not found in {path}")


def _v2_hook_source() -> str:
    return _function_source(
        PROJECT_ROOT / "scanner_runner.py", "_capture_me_inverse_short_v2_source",
    )


# ---------------------------------------------------------------------------
# 1. Static production-path audit (+ negative controls)
# ---------------------------------------------------------------------------
class TestStaticProductionPathAudit:
    def test_audit_passes_on_the_checked_out_sources(self):
        result = audit_source_path(PROJECT_ROOT)
        assert result["wired"] is True
        for name, check in result["checks"].items():
            assert check["status"] == "PASS", (name, check)

    def test_audit_detects_a_removed_v2_delivery(self, tmp_path):
        root = _copy_sources(tmp_path)
        runner = root / "scanner_runner.py"
        source = runner.read_text(encoding="utf-8")
        needle = (
            "                    "
            "_capture_me_inverse_short_v2_source(repository, inv_candidate)\n"
        )
        assert needle in source, "call site moved; update the control"
        # Replace the delivery with a no-op statement: the production loop
        # must stay syntactically intact, only its V2 delivery is removed.
        runner.write_text(
            source.replace(needle, "                    pass\n"),
            encoding="utf-8",
        )

        result = audit_source_path(root)
        assert result["wired"] is False
        check = result["checks"]["scanner_loop_delivers_v1_and_v2"]
        assert check["status"] == "FAIL"
        assert check["reason"] == "V2_DELIVERY_MISSING_FROM_PRODUCTION_LOOP"
        # V1 delivery and the hook definition are untouched by this control:
        assert result["checks"]["v2_hook_fail_open"]["status"] == "PASS"
        assert result["checks"]["v2_hook_flag_independent"]["status"] == "PASS"

    def test_audit_detects_a_removed_v1_delivery(self, tmp_path):
        root = _copy_sources(tmp_path)
        runner = root / "scanner_runner.py"
        source = runner.read_text(encoding="utf-8")
        needle = (
            "                    "
            "_capture_me_inverse_short_source(repository, inv_candidate)\n"
        )
        assert needle in source, "call site moved; update the control"
        runner.write_text(
            source.replace(needle, "                    pass\n"),
            encoding="utf-8",
        )

        result = audit_source_path(root)
        assert result["wired"] is False
        check = result["checks"]["scanner_loop_delivers_v1_and_v2"]
        assert check["status"] == "FAIL"
        assert check["reason"] == "V1_DELIVERY_MISSING_FROM_PRODUCTION_LOOP"

    def test_audit_detects_a_publication_moved_behind_the_gate(self, tmp_path):
        root = _copy_sources(tmp_path)
        orch = root / "app/scanners/orchestrator.py"
        source = orch.read_text(encoding="utf-8")
        gate = "gate_accepted: list[SetupCandidate] = []"
        assert gate in source
        # Make the gate marker appear BEFORE the publication:
        orch.write_text(gate + "\n" + source, encoding="utf-8")

        result = audit_source_path(root)
        assert result["wired"] is False
        check = result["checks"]["orchestrator_publishes_pre_gate"]
        assert check["status"] == "FAIL"
        assert check["reason"] == "SOURCE_PUBLICATION_NOT_PRE_GATE"

    def test_audit_detects_missing_source_files(self, tmp_path):
        result = audit_source_path(tmp_path)  # empty tree
        assert result["wired"] is False
        for name, check in result["checks"].items():
            assert check["status"] == "FAIL", name
            assert check["reason"].startswith("SOURCE_FILE_MISSING"), name

    def test_audit_never_reports_not_verified(self):
        # The audit either proves the wiring or reports a concrete FAIL —
        # it must never emit a soft status that could be read as PASS.
        for root in (PROJECT_ROOT, BASELINE_ROOT):
            result = audit_source_path(root)
            for check in result["checks"].values():
                assert check["status"] in ("PASS", "FAIL")
                assert check["status"] != "NOT_VERIFIED"


# ---------------------------------------------------------------------------
# 2. Runtime diagnostic control (requirement 3.4)
# ---------------------------------------------------------------------------
class TestRuntimeWiringDiagnostic:
    def test_wiring_diagnostic_reports_the_production_path(
        self, runner_module, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "0")
        wiring = runner_module.me_inverse_short_v2_source_wiring()
        assert wiring["wired"] is True
        for name, check in wiring["checks"].items():
            assert check["status"] == "PASS", (name, check)
        assert wiring["runtime_flag"] == "0"
        stats = wiring["stats"]
        assert set(stats) == {
            "deliveries", "captured", "duplicates", "disabled",
            "not_active", "pre_freeze", "invalid", "errors",
        }
        assert stats["deliveries"] == 0  # no scan ran in this process

    def test_diagnostic_runs_without_touching_the_database(
        self, runner_module, monkeypatch,
    ):
        monkeypatch.delenv(ACTIVATION_FLAG, raising=False)

        def _boom(*_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("the diagnostic must not connect anywhere")

        monkeypatch.setattr(
            runner_module, "_get_me_inverse_short_v2_store", _boom,
        )
        wiring = runner_module.me_inverse_short_v2_source_wiring()
        assert wiring["wired"] is True


# ---------------------------------------------------------------------------
# 3. The V2 hook on the production path (runtime behaviour)
# ---------------------------------------------------------------------------
class TestV2HookRuntimeDelivery:
    def test_flag_off_performs_zero_db_touch(
        self, runner_module, repository, monkeypatch,
    ):
        monkeypatch.delenv(ACTIVATION_FLAG, raising=False)

        def _boom(*_args, **_kwargs):  # pragma: no cover
            raise AssertionError("flag OFF must not open a store connection")

        monkeypatch.setattr(
            runner_module, "_get_me_inverse_short_v2_store", _boom,
        )
        runner_module._capture_me_inverse_short_v2_source(
            repository, _candidate(),
        )
        stats = runner_module._me_inverse_short_v2_source_stats
        assert stats["deliveries"] == 1
        assert stats["disabled"] == 1
        assert stats["captured"] == 0
        assert stats["errors"] == 0
        assert repository.errors == []

    def test_flag_on_delivers_into_the_v2_ledger(
        self, runner_module, repository, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        store = _v2_store()
        monkeypatch.setattr(
            runner_module, "_me_inverse_short_v2_store", store,
        )
        runner_module._capture_me_inverse_short_v2_source(
            repository, _candidate(),
        )
        stats = runner_module._me_inverse_short_v2_source_stats
        assert stats["deliveries"] == 1
        assert stats["captured"] == 1
        assert stats["errors"] == 0
        assert repository.errors == []
        assert len(store.list_events()) == 1
        assert store.list_events()[0].status == "CAPTURED"

    def test_predelivery_redelivery_is_idempotent(
        self, runner_module, repository, monkeypatch,
    ):
        # The publication is PRE-dedup: a re-delivered candidate must be
        # counted as DUPLICATE, never as a second event row.
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        store = _v2_store()
        monkeypatch.setattr(
            runner_module, "_me_inverse_short_v2_store", store,
        )
        candidate = _candidate()
        runner_module._capture_me_inverse_short_v2_source(
            repository, candidate,
        )
        runner_module._capture_me_inverse_short_v2_source(
            repository, candidate,
        )
        stats = runner_module._me_inverse_short_v2_source_stats
        assert stats["deliveries"] == 2
        assert stats["captured"] == 1
        assert stats["duplicates"] == 1
        assert len(store.list_events()) == 1

    def test_store_unavailable_sinks_a_v2_error_and_never_raises(
        self, runner_module, repository, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        # Throttle the getter so it returns None WITHOUT attempting a
        # connection (no network, no DB): the hook must fail open.
        monkeypatch.setattr(
            runner_module, "_me_inverse_short_v2_store_retry_at",
            time.monotonic(),
        )
        runner_module._capture_me_inverse_short_v2_source(
            repository, _candidate(),
        )
        stats = runner_module._me_inverse_short_v2_source_stats
        assert stats["errors"] == 1
        assert len(repository.errors) == 1
        assert repository.errors[0]["error_type"] == V2_ERROR_TYPE
        assert V1_ERROR_TYPE not in {
            error["error_type"] for error in repository.errors
        }

    @pytest.mark.parametrize(
        ("scanner", "direction"),
        [
            ("MOMENTUM_EXHAUSTION_REVERSE_LONG", "LONG"),
            ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", "SHORT"),
            ("SOME_OTHER_SCANNER", "LONG"),
        ],
    )
    def test_foreign_candidates_are_ignored(
        self, runner_module, repository, monkeypatch, scanner, direction,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        candidate = _candidate(scanner_name=scanner, direction=direction)
        runner_module._capture_me_inverse_short_v2_source(
            repository, candidate,
        )
        assert all(
            value == 0
            for value in runner_module._me_inverse_short_v2_source_stats.values()
        )
        assert repository.errors == []

    def test_hook_source_never_references_v1(self, runner_module):
        source = (
            PROJECT_ROOT / "scanner_runner.py"
        ).read_text(encoding="utf-8")
        start = source.index("def _capture_me_inverse_short_v2_source")
        end = source.index("\ndef ", start + 1)
        hook_source = source[start:end]
        # V2-only: no V1 module reference, no V1 flag literal — the flag
        # comes from the V2 activation import (single source of truth).
        assert "me_long_v2_inverse_short_v1" not in hook_source
        assert V1_FLAG not in hook_source
        assert "is_runtime_enabled" in hook_source
        assert "from app.paper.me_long_v2_inverse_short_v2.activation import" in (
            hook_source
        )

    def test_hook_source_never_depends_on_trading_gates(self):
        """V2 capture must not consult gates/regime/expectancy/scoring.

        Requirement: V2 routing is independent of trading decisions — the
        hook only filters scanner identity/direction and talks to the V2
        activation flag + store.
        """
        hook_source = _v2_hook_source().lower()
        for token in (
            "gate_accepted", "expectancy", "market_regime", "scanner_setup",
            "dds.scanner_setup", "setup_state", "score", "allowlist",
            "blacklist", "blocklist", "min_score",
        ):
            assert token not in hook_source, token
        # Only V2-only surfaces are consulted:
        assert "me_long_v2_inverse_short_v2" in hook_source
        assert "_get_me_inverse_short_v2_store" in _v2_hook_source()


# ---------------------------------------------------------------------------
# 4. V1 / V2 independence
# ---------------------------------------------------------------------------
class TestV1V2Independence:
    def test_v2_capture_does_not_read_the_v1_flag(
        self, runner_module, repository, monkeypatch,
    ):
        monkeypatch.setenv(V1_FLAG, "0")   # V1 explicitly OFF
        monkeypatch.setenv(ACTIVATION_FLAG, "1")  # V2 explicitly ON
        store = _v2_store()
        monkeypatch.setattr(
            runner_module, "_me_inverse_short_v2_store", store,
        )
        runner_module._capture_me_inverse_short_v2_source(
            repository, _candidate(),
        )
        stats = runner_module._me_inverse_short_v2_source_stats
        assert stats["captured"] == 1, "V2 must ignore the V1 flag"
        assert len(store.list_events()) == 1

    def test_v2_capture_is_off_while_only_the_v1_flag_is_on(
        self, runner_module, repository, monkeypatch,
    ):
        monkeypatch.setenv(V1_FLAG, "1")
        monkeypatch.delenv(ACTIVATION_FLAG, raising=False)
        store = _v2_store()
        monkeypatch.setattr(
            runner_module, "_me_inverse_short_v2_store", store,
        )
        runner_module._capture_me_inverse_short_v2_source(
            repository, _candidate(),
        )
        stats = runner_module._me_inverse_short_v2_source_stats
        assert stats["disabled"] == 1
        assert stats["captured"] == 0
        assert len(store.list_events()) == 0

    def test_v2_failure_never_touches_v1_state(
        self, runner_module, repository, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        # V1 store cache holds a sentinel that must remain untouched:
        v1_sentinel = object()
        runner_module._me_inverse_short_store = v1_sentinel
        runner_module._me_inverse_short_store_retry_at = time.monotonic()
        # V2 store unavailable (throttled -> None):
        monkeypatch.setattr(
            runner_module, "_me_inverse_short_v2_store_retry_at",
            time.monotonic(),
        )

        runner_module._capture_me_inverse_short_v2_source(
            repository, _candidate(),
        )

        assert runner_module._me_inverse_short_store is v1_sentinel
        error_types = {error["error_type"] for error in repository.errors}
        assert error_types == {V2_ERROR_TYPE}
        stats = runner_module._me_inverse_short_v2_source_stats
        assert stats["errors"] == 1

    def test_v1_and_v2_keep_separate_store_state(
        self, runner_module, repository, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        v1_sentinel = object()
        runner_module._me_inverse_short_store = v1_sentinel
        v2_store = _v2_store()
        monkeypatch.setattr(
            runner_module, "_me_inverse_short_v2_store", v2_store,
        )
        runner_module._capture_me_inverse_short_v2_source(
            repository, _candidate(),
        )
        assert runner_module._me_inverse_short_store is v1_sentinel
        assert len(v2_store.list_events()) == 1

    def test_v2_error_type_is_distinct_from_v1(self):
        # Two separate sinks: a V2 capture failure can never be confused
        # with a V1 one (and vice versa) in dds.scanner_error.
        assert V1_ERROR_TYPE != V2_ERROR_TYPE
        assert V1_ERROR_TYPE in (
            PROJECT_ROOT / "scanner_runner.py"
        ).read_text(encoding="utf-8")
        assert V2_ERROR_TYPE in (
            PROJECT_ROOT / "scanner_runner.py"
        ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 5. V1 byte-identity vs the pre-change baseline snapshot
# ---------------------------------------------------------------------------
class TestV1ByteIdentity:
    def test_v1_package_files_are_byte_identical_to_baseline(self):
        baseline_pkg = BASELINE_ROOT / V1_PACKAGE
        assert baseline_pkg.is_dir(), "baseline snapshot missing"
        compared = 0
        for current in sorted((PROJECT_ROOT / V1_PACKAGE).glob("*.py")):
            baseline = baseline_pkg / current.name
            if not baseline.is_file():
                continue
            compared += 1
            assert current.read_bytes() == baseline.read_bytes(), (
                f"V1 file changed: {current.name}",
            )
        assert compared >= 8, "expected the whole V1 package in baseline"

    def test_v1_capture_hook_in_scanner_runner_is_byte_identical(self):
        current = PROJECT_ROOT / "scanner_runner.py"
        baseline = BASELINE_ROOT / "scanner_runner.py"
        assert baseline.is_file(), "baseline snapshot missing"
        for function in (
            "_get_me_inverse_short_store",
            "_capture_me_inverse_short_source",
        ):
            assert (
                _function_source(current, function)
                == _function_source(baseline, function)
            ), f"V1 function changed: {function}"

    def test_v1_delivery_call_site_still_present(self):
        source = (
            PROJECT_ROOT / "scanner_runner.py"
        ).read_text(encoding="utf-8")
        assert '"_me_inverse_short_source_events", []' in source
        assert "_capture_me_inverse_short_source(repository, inv_candidate)" in source

    def test_migration_085_and_v1_fingerprint_files_untouched(self):
        for rel in (
            "sql/migrations/085_me_long_v2_inverse_short_v1.sql",
            "sql/migrations/086_me_long_v2_inverse_short_v1_close_outbox.sql",
        ):
            current = PROJECT_ROOT / rel
            baseline = BASELINE_ROOT / rel
            if baseline.is_file():
                # Line endings may differ (checkout normalization); content
                # must be identical.
                assert (
                    current.read_bytes().replace(b"\r\n", b"\n")
                    == baseline.read_bytes().replace(b"\r\n", b"\n")
                ), rel
