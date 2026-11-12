"""ME_LONG_V2_INVERSE_SHORT_OOS2_V1 — P1-1 production source-path wiring.

Requirement 3.4 (diagnostic control): V2 capture must be demonstrably on
the REAL prospective-only publication chain

    ScannerOrchestrator.scan_all_with_stats  (pre-gate publication)
        -> stats["_me_inverse_short_source_events"]
        -> scanner_runner production loop
        -> _capture_me_inverse_short_oos2_source  (OOS-2 hook, fail-open)
        -> capture_with_status(ROUTE_PRE_FILTER_SOURCE)
        -> PostgreSQL (V2 event ledger)

— not merely an importable module.  This suite proves:

* the shared static audit (``audit_source_path``) passes on the checked-out
  sources AND has NEGATIVE controls: it detects a removed OOS-2 delivery, a
  removed V1 delivery, a publication moved behind the gate and missing
  source files (an audit that cannot fail proves nothing);
* the runtime diagnostic ``scanner_runner.me_inverse_short_oos2_source_wiring()``
  reports the production path plus runtime delivery counters;
* the V2 hook's runtime behaviour: deliveries counted, flag-off = zero DB
  touch, store-unavailable = ``ME_INVERSE_SHORT_OOS2_SOURCE_CAPTURE_ERROR``
  sink (never a scanner failure), success = CAPTURED into the V2 ledger,
  idempotent on re-delivery;
* V1/V2 independence: own flag, own store state, own error sink — a V2
  failure never touches V1 state and the V2 hook never reads the V1 flag;
* V1 byte-identity against the PINNED Git baseline revision
  (``BASELINE_SHA`` — this PR's merge-base with main), read with local
  ``git show`` / ``git ls-tree``: the checks are self-contained and work in
  any checkout without a local ``baseline_repo/`` snapshot, without network
  access and without depending on the mutable ``origin/main``.

No PostgreSQL is required by this suite.
"""
from __future__ import annotations

import ast
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.paper.me_long_v2_inverse_short_oos2_v1.activation import ACTIVATION_FLAG
from app.paper.me_long_v2_inverse_short_oos2_v1.models import ActivationRecord
from app.paper.me_long_v2_inverse_short_oos2_v1.protocol import (
    EXPERIMENT_ID,
    SOURCE_SCANNER,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.source_path import audit_source_path
from app.paper.me_long_v2_inverse_short_oos2_v1.store import (
    InMemoryInverseShortStore,
)
from app.scanners.models import SetupCandidate, SetupState

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Pinned baseline: merge-base of this PR branch against main.  Fixed by
# content (never ``origin/main`` — its content changes on merge), and read
# from the LOCAL git object store only (no network from pytest).
BASELINE_SHA = "6bc7ea777d606a3068e7ff62aef2b8d771be19a4"
GIT_CMD = "git"

SIGNAL_AT = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)

V1_FLAG = "ME_INVERSE_SHORT_V1_ENABLED"
V1_ERROR_TYPE = "ME_INVERSE_SHORT_SOURCE_CAPTURE_ERROR"
V2_ERROR_TYPE = "ME_INVERSE_SHORT_OOS2_SOURCE_CAPTURE_ERROR"

V1_PACKAGE = "app/paper/me_long_v2_inverse_short_v1"
SOURCE_FILES = (
    "scanner_runner.py",
    "app/scanners/orchestrator.py",
    "app/paper/me_long_v2_inverse_short_oos2_v1/capture.py",
    "app/paper/me_long_v2_inverse_short_oos2_v1/engine.py",
    "app/paper/me_long_v2_inverse_short_oos2_v1/protocol.py",
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
        mod._me_inverse_short_oos2_store,
        mod._me_inverse_short_oos2_store_retry_at,
        dict(mod._me_inverse_short_oos2_source_stats),
        mod._me_inverse_short_store,
        mod._me_inverse_short_store_retry_at,
    )
    mod._me_inverse_short_oos2_store = None
    mod._me_inverse_short_oos2_store_retry_at = 0.0
    mod._me_inverse_short_oos2_source_stats.update(
        {key: 0 for key in mod._me_inverse_short_oos2_source_stats},
    )
    yield mod
    (
        mod._me_inverse_short_oos2_store,
        mod._me_inverse_short_oos2_store_retry_at,
        stats,
        mod._me_inverse_short_store,
        mod._me_inverse_short_store_retry_at,
    ) = saved
    mod._me_inverse_short_oos2_source_stats.clear()
    mod._me_inverse_short_oos2_source_stats.update(stats)


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


def _copy_sources_from_git(tmp_path: Path, *, revision: str = BASELINE_SHA) -> Path:
    """Minimal source tree materialized FROM the pinned Git baseline.

    Lets the audit run against the pre-change tree without any local
    ``baseline_repo/`` directory.  A source file that does not exist AT the
    baseline (the V2 package predates nothing — it was added by this
    branch) is simply absent from the materialized tree: absence is
    baseline content, not a skipped comparison.
    """
    present = set(_git_ls_tree_names(revision))
    for rel in SOURCE_FILES:
        if rel not in present:
            continue
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(_git_show_bytes(rel, revision=revision))
    return tmp_path


def _copy_v1_package(tmp_path: Path) -> Path:
    """Copy the current V1 package into ``tmp_path`` (negative controls)."""
    for src in sorted((PROJECT_ROOT / V1_PACKAGE).glob("*.py")):
        dst = tmp_path / V1_PACKAGE / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    return tmp_path


def _norm_eol(data: bytes) -> bytes:
    """Checkout-normalized blob equivalence (core.autocrlf=true on Windows).

    Raw bytes may legitimately differ between a checked-out file (CRLF) and
    the stored Git blob (LF); only EOL normalization is allowed to differ.
    """
    return data.replace(b"\r\n", b"\n")


def _git_failure(action: str, detail: str) -> AssertionError:
    return AssertionError(
        f"{action} (pinned baseline {BASELINE_SHA}): {detail}. The tests "
        "read the baseline from the LOCAL git object store only — no "
        "network access from pytest. Fix the checkout instead of skipping "
        "(actions/checkout uses fetch-depth: 0; otherwise run "
        f"`git fetch origin {BASELINE_SHA}` or `git fetch --unshallow`)."
    )


def _git_show_bytes(relative_path: str, *, revision: str = BASELINE_SHA) -> bytes:
    """Read one file from the pinned baseline revision via local git.

    FAILS with an explicit diagnostic when git is missing or the
    revision/path is unavailable (e.g. a shallow checkout without
    ``BASELINE_SHA``) — never skips, never silently passes.
    """
    try:
        proc = subprocess.run(
            [GIT_CMD, "show", f"{revision}:{relative_path}"],
            cwd=PROJECT_ROOT, capture_output=True, check=False,
        )
    except FileNotFoundError:
        raise _git_failure(
            "git executable not found",
            f"the V1 baseline identity checks require a local git "
            f"checkout ({GIT_CMD!r} is unavailable)",
        ) from None
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise _git_failure(
            "baseline revision unavailable",
            f"git show {revision}:{relative_path} exited "
            f"{proc.returncode}: {stderr or '<no stderr>'}",
        )
    return proc.stdout


def _git_ls_tree_names(revision: str, *path: str) -> list[str]:
    """File names tracked at ``revision`` (optionally under ``path``).

    FAILS with an explicit diagnostic when git is missing or the revision
    is unavailable — never skips.
    """
    cmd = [GIT_CMD, "ls-tree", "-r", "--name-only", revision]
    if path:
        cmd += ["--", *path]
    try:
        proc = subprocess.run(
            cmd, cwd=PROJECT_ROOT, capture_output=True, check=False,
        )
    except FileNotFoundError:
        raise _git_failure(
            "git executable not found",
            f"the V1 baseline identity checks require a local git "
            f"checkout ({GIT_CMD!r} is unavailable)",
        ) from None
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise _git_failure(
            "baseline revision unavailable",
            f"git ls-tree {revision} {' '.join(path)} exited "
            f"{proc.returncode}: {stderr or '<no stderr>'}",
        )
    return [
        line for line in proc.stdout.decode("utf-8", "replace").splitlines()
        if line
    ]


def _git_baseline_v1_files(*, revision: str = BASELINE_SHA) -> list[str]:
    """V1 package files at the pinned revision (via ``git ls-tree``)."""
    return _git_ls_tree_names(revision, V1_PACKAGE)


def _function_source(path: Path, name: str) -> str:
    """Byte-exact source of one top-level function (AST line range)."""
    return _function_source_from_text(
        path.read_text(encoding="utf-8"), name, origin=str(path),
    )


def _function_source_from_text(source: str, name: str, *, origin: str) -> str:
    # Normalize line endings first: the checked-out file may be CRLF while
    # the baseline blob is LF (core.autocrlf=true) — EOL is not content.
    source = source.replace("\r\n", "\n")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            lines = source.splitlines(keepends=True)
            return "".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"function {name!r} not found in {origin}")


def _v2_hook_source() -> str:
    return _function_source(
        PROJECT_ROOT / "scanner_runner.py", "_capture_me_inverse_short_oos2_source",
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
            "_capture_me_inverse_short_oos2_source(\n"
            "                        repository, inv_candidate,\n"
            "                    )\n"
        )
        assert needle in source, "call site moved; update the control"
        # Replace the delivery with a no-op statement: the production loop
        # must stay syntactically intact, only its OOS-2 delivery is removed.
        runner.write_text(
            source.replace(needle, "                    pass\n"),
            encoding="utf-8",
        )

        result = audit_source_path(root)
        assert result["wired"] is False
        check = result["checks"]["scanner_loop_delivers_v1_and_oos2"]
        assert check["status"] == "FAIL"
        assert check["reason"] == "OOS2_DELIVERY_MISSING_FROM_PRODUCTION_LOOP"
        # V1 delivery and the hook definition are untouched by this control:
        assert result["checks"]["oos2_hook_fail_open"]["status"] == "PASS"
        assert result["checks"]["oos2_hook_flag_independent"]["status"] == "PASS"

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
        check = result["checks"]["scanner_loop_delivers_v1_and_oos2"]
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

    def test_audit_never_reports_not_verified(self, tmp_path):
        # The audit either proves the wiring or reports a concrete FAIL —
        # it must never emit a soft status that could be read as PASS.
        # Run it on the checked-out tree AND on the pinned-baseline tree
        # (materialized from git — the pre-change code has no V2 wiring, so
        # its V2 checks FAIL concretely instead of being "not verified").
        baseline_tree = _copy_sources_from_git(tmp_path)
        for root in (PROJECT_ROOT, baseline_tree):
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
        wiring = runner_module.me_inverse_short_oos2_source_wiring()
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
            runner_module, "_get_me_inverse_short_oos2_store", _boom,
        )
        wiring = runner_module.me_inverse_short_oos2_source_wiring()
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
            runner_module, "_get_me_inverse_short_oos2_store", _boom,
        )
        runner_module._capture_me_inverse_short_oos2_source(
            repository, _candidate(),
        )
        stats = runner_module._me_inverse_short_oos2_source_stats
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
            runner_module, "_me_inverse_short_oos2_store", store,
        )
        runner_module._capture_me_inverse_short_oos2_source(
            repository, _candidate(),
        )
        stats = runner_module._me_inverse_short_oos2_source_stats
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
            runner_module, "_me_inverse_short_oos2_store", store,
        )
        candidate = _candidate()
        runner_module._capture_me_inverse_short_oos2_source(
            repository, candidate,
        )
        runner_module._capture_me_inverse_short_oos2_source(
            repository, candidate,
        )
        stats = runner_module._me_inverse_short_oos2_source_stats
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
            runner_module, "_me_inverse_short_oos2_store_retry_at",
            time.monotonic(),
        )
        runner_module._capture_me_inverse_short_oos2_source(
            repository, _candidate(),
        )
        stats = runner_module._me_inverse_short_oos2_source_stats
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
        runner_module._capture_me_inverse_short_oos2_source(
            repository, candidate,
        )
        assert all(
            value == 0
            for value in runner_module._me_inverse_short_oos2_source_stats.values()
        )
        assert repository.errors == []

    def test_hook_source_never_references_v1(self, runner_module):
        source = (
            PROJECT_ROOT / "scanner_runner.py"
        ).read_text(encoding="utf-8")
        start = source.index("def _capture_me_inverse_short_oos2_source")
        end = source.index("\ndef ", start + 1)
        hook_source = source[start:end]
        # V2-only: no V1 module reference, no V1 flag literal — the flag
        # comes from the V2 activation import (single source of truth).
        assert "me_long_v2_inverse_short_v1" not in hook_source
        assert V1_FLAG not in hook_source
        assert "is_runtime_enabled" in hook_source
        assert "from app.paper.me_long_v2_inverse_short_oos2_v1.activation import" in (
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
        assert "me_long_v2_inverse_short_oos2_v1" in hook_source
        assert "_get_me_inverse_short_oos2_store" in _v2_hook_source()


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
            runner_module, "_me_inverse_short_oos2_store", store,
        )
        runner_module._capture_me_inverse_short_oos2_source(
            repository, _candidate(),
        )
        stats = runner_module._me_inverse_short_oos2_source_stats
        assert stats["captured"] == 1, "V2 must ignore the V1 flag"
        assert len(store.list_events()) == 1

    def test_v2_capture_is_off_while_only_the_v1_flag_is_on(
        self, runner_module, repository, monkeypatch,
    ):
        monkeypatch.setenv(V1_FLAG, "1")
        monkeypatch.delenv(ACTIVATION_FLAG, raising=False)
        store = _v2_store()
        monkeypatch.setattr(
            runner_module, "_me_inverse_short_oos2_store", store,
        )
        runner_module._capture_me_inverse_short_oos2_source(
            repository, _candidate(),
        )
        stats = runner_module._me_inverse_short_oos2_source_stats
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
            runner_module, "_me_inverse_short_oos2_store_retry_at",
            time.monotonic(),
        )

        runner_module._capture_me_inverse_short_oos2_source(
            repository, _candidate(),
        )

        assert runner_module._me_inverse_short_store is v1_sentinel
        error_types = {error["error_type"] for error in repository.errors}
        assert error_types == {V2_ERROR_TYPE}
        stats = runner_module._me_inverse_short_oos2_source_stats
        assert stats["errors"] == 1

    def test_v1_and_v2_keep_separate_store_state(
        self, runner_module, repository, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        v1_sentinel = object()
        runner_module._me_inverse_short_store = v1_sentinel
        v2_store = _v2_store()
        monkeypatch.setattr(
            runner_module, "_me_inverse_short_oos2_store", v2_store,
        )
        runner_module._capture_me_inverse_short_oos2_source(
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
# 5. V1 byte-identity vs the pinned Git baseline (self-contained)
# ---------------------------------------------------------------------------
def _assert_v1_package_identical(root: Path, *, revision: str = BASELINE_SHA) -> None:
    """Compare the V1 package under ``root`` to the pinned Git baseline.

    Detects files ADDED since the baseline, files REMOVED since the
    baseline and content changes.  Content is compared as normalized Git
    blobs (EOL-only differences are checkout normalization under
    ``core.autocrlf=true``); a missing baseline file or an unavailable
    revision FAILS with a diagnostic — the check never skips.
    """
    current = sorted(
        f"{V1_PACKAGE}/{p.name}" for p in (root / V1_PACKAGE).glob("*.py")
    )
    assert current, f"V1 package not found under {root}"
    baseline = _git_baseline_v1_files(revision=revision)
    added = sorted(set(current) - set(baseline))
    removed = sorted(set(baseline) - set(current))
    assert not added, f"V1 files added since baseline {revision}: {added}"
    assert not removed, f"V1 files removed since baseline {revision}: {removed}"
    for rel in current:
        baseline_bytes = _git_show_bytes(rel, revision=revision)
        current_bytes = (root / rel).read_bytes()
        assert _norm_eol(current_bytes) == _norm_eol(baseline_bytes), (
            f"V1 file changed: {Path(rel).name}",
        )
    assert len(current) >= 8, "expected the whole V1 package"


def _assert_v1_capture_functions_identical(
    scanner_path: Path, *, revision: str = BASELINE_SHA,
) -> None:
    """Compare the two V1 capture functions to their pinned-baseline text.

    Detects any change in the bodies or signatures of
    ``_get_me_inverse_short_store`` and ``_capture_me_inverse_short_source``
    (AST line range, EOL-normalized); a missing function or baseline
    revision FAILS with a diagnostic.
    """
    baseline_source = _git_show_bytes(
        "scanner_runner.py", revision=revision,
    ).decode("utf-8", "replace")
    current_source = scanner_path.read_text(encoding="utf-8")
    for function in (
        "_get_me_inverse_short_store",
        "_capture_me_inverse_short_source",
    ):
        baseline_fn = _function_source_from_text(
            baseline_source, function, origin=f"{revision}:scanner_runner.py",
        )
        current_fn = _function_source_from_text(
            current_source, function, origin=str(scanner_path),
        )
        assert current_fn == baseline_fn, f"V1 function changed: {function}"


def _tamper_function(path: Path, name: str) -> Path:
    """Insert a statement INSIDE ``name``'s body (negative controls)."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )
    lines = source.splitlines(keepends=True)
    # A statement (not a comment) so the AST line range grows and the
    # extracted function source provably changes.
    lines.insert(node.end_lineno, "    pass  # tampered by the negative control\n")
    path.write_text("".join(lines), encoding="utf-8")
    return path


class TestV1ByteIdentity:
    def test_v1_package_files_match_the_pinned_git_baseline(self):
        _assert_v1_package_identical(PROJECT_ROOT)

    def test_v1_capture_hook_functions_match_the_pinned_git_baseline(self):
        _assert_v1_capture_functions_identical(PROJECT_ROOT / "scanner_runner.py")

    def test_changed_v1_file_is_detected(self, tmp_path):
        root = _copy_v1_package(tmp_path)
        target = root / V1_PACKAGE / "capture.py"
        target.write_bytes(target.read_bytes() + b"# tampered\n")
        with pytest.raises(AssertionError, match="V1 file changed"):
            _assert_v1_package_identical(root)

    def test_v1_file_absent_from_the_baseline_is_detected(self, tmp_path):
        root = _copy_v1_package(tmp_path)
        (root / V1_PACKAGE / "zz_added_since_baseline.py").write_text(
            "# not in the pinned baseline\n", encoding="utf-8",
        )
        with pytest.raises(AssertionError, match="added since baseline"):
            _assert_v1_package_identical(root)

    def test_removed_v1_file_is_detected(self, tmp_path):
        root = _copy_v1_package(tmp_path)
        (root / V1_PACKAGE / "models.py").unlink()
        with pytest.raises(AssertionError, match="removed since baseline"):
            _assert_v1_package_identical(root)

    def test_changed_v1_capture_hook_is_detected(self, tmp_path):
        scanner = tmp_path / "scanner_runner.py"
        scanner.write_bytes((PROJECT_ROOT / "scanner_runner.py").read_bytes())
        _tamper_function(scanner, "_capture_me_inverse_short_source")
        with pytest.raises(AssertionError, match="V1 function changed"):
            _assert_v1_capture_functions_identical(scanner)

    def test_unavailable_baseline_revision_fails_with_diagnostics(self):
        bogus = "0" * 40
        with pytest.raises(AssertionError, match="baseline revision unavailable"):
            _git_show_bytes("scanner_runner.py", revision=bogus)
        with pytest.raises(AssertionError, match="baseline revision unavailable"):
            _git_baseline_v1_files(revision=bogus)

    def test_missing_git_fails_with_diagnostics(self, monkeypatch):
        def _no_git(*_args, **_kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(subprocess, "run", _no_git)
        with pytest.raises(AssertionError, match="git executable not found"):
            _git_show_bytes("scanner_runner.py")

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
            baseline_bytes = _git_show_bytes(rel)
            current_bytes = (PROJECT_ROOT / rel).read_bytes()
            # EOL-normalized blob equivalence (checkout normalization only).
            assert _norm_eol(current_bytes) == _norm_eol(baseline_bytes), rel
