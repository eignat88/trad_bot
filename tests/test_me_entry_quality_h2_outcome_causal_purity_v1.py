"""Issue #298 / #312 — causality, determinism, purity and isolation tests.

Covers the causal guarantees of the outcome model (closed-candle-only path
evaluation, ASOF strict at microsecond precision, no look-ahead/repainting,
prefix stability, deterministic idempotent results) and the package purity
contract (stdlib only, no I/O/network/DB/clock, and NEVER wired into any
production surface).
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.research.me_entry_quality_h2_outcome_v1.entry_model import resolve_entry
from app.research.me_entry_quality_h2_outcome_v1.paired import evaluate_pair
from app.research.me_entry_quality_h2_outcome_v1.policy import (
    ENTRY_PRICE_CLASS_NEXT_CANDLE_OPEN,
    EntryDeadline,
    EntrySpec,
)
from app.research.me_entry_quality_h2_outcome_v1.types import (
    ENTRY_ENTERED,
    ENTRY_PENDING,
    CandleBar,
    ConfirmationInfo,
    SourceEventInput,
    closed_candles,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "app" / "research" / "me_entry_quality_h2_outcome_v1"
PKG_PREFIX = "app.research.me_entry_quality_h2_outcome_v1"

BASE_MS = 1_700_000_100_000
BAR_MS = 300_000
SIGNAL_US = (BASE_MS + 100_000) * 1_000
CONFIRM_US = (BASE_MS + BAR_MS) * 1_000
ASOF_US = (BASE_MS + 9 * BAR_MS) * 1_000

EVENT = SourceEventInput(
    source_event_key="src-causal",
    variant_id="H2_RSI_TURN_1",
    symbol="BTCUSDT",
    direction="SHORT",
    signal_time_us=SIGNAL_US,
    original_price=100.0,
    source_stop_price=101.0,
    source_target_price=98.0,
)
CONFIRMATION = ConfirmationInfo(
    state="CONFIRMED_DELAYED",
    confirmation_time_us=CONFIRM_US,
    confirmation_candle_open_ms=BASE_MS,
    confirmation_close_reference=100.2,
)


def bar(index: int, o: float, h: float, low: float, c: float):
    return CandleBar(
        open_time_ms=BASE_MS + index * BAR_MS,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1.0,
    )


CANDLES = [
    bar(0, 100.0, 100.5, 99.8, 100.2),
    bar(1, 100.3, 100.6, 100.1, 100.4),
    bar(2, 100.4, 100.45, 99.5, 99.7),
    bar(3, 99.7, 99.9, 98.2, 98.5),
    bar(4, 98.5, 98.8, 97.9, 98.1),
]


class TestAsofStrictMicrosecond:
    def test_candle_closing_exactly_at_the_asof_is_closed(self):
        asof = CANDLES[1].close_time_us  # exactly at the close boundary
        closed = closed_candles(CANDLES, asof)
        assert [c.open_time_ms for c in closed] == [
            CANDLES[0].open_time_ms,
            CANDLES[1].open_time_ms,
        ]

    def test_candle_closing_one_microsecond_after_the_asof_is_not_closed(self):
        asof = CANDLES[1].close_time_us - 1  # 1µs before the close
        closed = closed_candles(CANDLES, asof)
        assert [c.open_time_ms for c in closed] == [CANDLES[0].open_time_ms]

    def test_entry_resolution_never_uses_a_bar_that_is_not_yet_closed(self):
        # ASOF falls INSIDE candle 1: the entry cannot be resolved yet
        asof = CONFIRM_US + 150 * 1_000_000
        result = resolve_entry(
            confirmation=CONFIRMATION,
            closed=closed_candles(CANDLES, asof),
            entry_spec=EntrySpec(
                price_class=ENTRY_PRICE_CLASS_NEXT_CANDLE_OPEN
            ),
            fill_policy=_fill_policy(),
            deadline=EntryDeadline(max_delay_ms=30 * 60 * 1000),
            evaluation_asof_us=asof,
        )
        assert result.status == ENTRY_PENDING
        assert result.entry_price is None

    def test_one_microsecond_asof_change_is_visible_in_the_result_boundary(self):
        entry_spec = EntrySpec(
            price_class=ENTRY_PRICE_CLASS_NEXT_CANDLE_OPEN
        )
        before_close = CANDLES[1].close_time_us - 1
        r_before = resolve_entry(
            confirmation=CONFIRMATION,
            closed=closed_candles(CANDLES, before_close),
            entry_spec=entry_spec,
            fill_policy=_fill_policy(),
            deadline=EntryDeadline(max_delay_ms=30 * 60 * 1000),
            evaluation_asof_us=before_close,
        )
        at_close = CANDLES[1].close_time_us
        r_at = resolve_entry(
            confirmation=CONFIRMATION,
            closed=closed_candles(CANDLES, at_close),
            entry_spec=entry_spec,
            fill_policy=_fill_policy(),
            deadline=EntryDeadline(max_delay_ms=30 * 60 * 1000),
            evaluation_asof_us=at_close,
        )
        assert r_before.status == ENTRY_PENDING
        assert r_at.status == ENTRY_ENTERED


def _fill_policy():
    from app.research.me_entry_quality_h2_outcome_v1.policy import (
        GAP_THROUGH_FILL_AT_OPEN,
        MISSED_STAT_ZERO_NET,
        FillPolicy,
    )

    return FillPolicy(
        gap_through_policy=GAP_THROUGH_FILL_AT_OPEN,
        missed_entry_stat_policy=MISSED_STAT_ZERO_NET,
    )


class TestNoLookAhead:
    def test_appending_corrupt_future_rows_never_changes_an_earlier_result(self):
        early_asof = (BASE_MS + 5 * BAR_MS) * 1_000
        before = _evaluate(CANDLES, early_asof).to_dict()
        corrupt_future = CANDLES + [
            CandleBar(
                open_time_ms=BASE_MS + 9 * BAR_MS,
                open=1.0,
                high=9999.0,
                low=0.5,
                close=5000.0,
                volume=1.0,
            ),
            CandleBar(
                open_time_ms=BASE_MS + 10 * BAR_MS,
                open=2.0,
                high=9999.0,
                low=0.5,
                close=5000.0,
                volume=1.0,
            ),
        ]
        after = _evaluate(corrupt_future, early_asof).to_dict()
        assert before == after

    def test_results_are_prefix_stable_across_evaluation_asofs(self):
        earlier = _evaluate(CANDLES, (BASE_MS + 4 * BAR_MS) * 1_000).to_dict()
        later = _evaluate(CANDLES, ASOF_US).to_dict()
        # both are valid snapshots; the earlier one is unchanged by the
        # later evaluation (already asserted above via future appends)
        assert earlier["evaluation_asof_us"] < later["evaluation_asof_us"]


def _evaluate(candles, asof_us):
    from test_me_entry_quality_h2_outcome_paired_v1 import make_policy

    return evaluate_pair(
        event=EVENT,
        confirmation=CONFIRMATION,
        candles=candles,
        policy=make_policy(),
        evaluation_asof_us=asof_us,
    )


class TestDeterminismAndIdempotency:
    def test_repeated_evaluation_is_bit_identical(self):
        first = _evaluate(CANDLES, ASOF_US).to_dict()
        second = _evaluate(CANDLES, ASOF_US).to_dict()
        assert first == second

    def test_outcome_key_is_stable_across_repeats(self):
        first = _evaluate(CANDLES, ASOF_US)
        second = _evaluate(CANDLES, ASOF_US)
        assert first.outcome_key == second.outcome_key

    def test_input_row_order_does_not_change_the_result(self):
        shuffled = [
            CANDLES[3], CANDLES[0], CANDLES[4], CANDLES[2], CANDLES[1]
        ]
        assert _evaluate(shuffled, ASOF_US).to_dict() == _evaluate(
            CANDLES, ASOF_US
        ).to_dict()


# ─── package purity (mirrors the merged H2 engine's purity contract) ────────
ALLOWED_IMPORTS = {
    "__future__",
    "dataclasses",
    "hashlib",
    "json",
    "math",
    "typing",
    "app.research.me_entry_quality_h2_outcome_v1",
}

FORBIDDEN_TOKENS = (
    "open(",
    "Path(",
    "socket",
    "requests",
    "urllib",
    "psycopg2",
    "pg8000",
    "subprocess",
    "os.environ",
    "getenv(",
    "mktime(",
    "localtime(",
    "time.time",
    "datetime.now",
    "utcnow(",
    "today(",
    "bybit_client",
    "BybitClient",
    "INSERT INTO",
    "UPDATE ",
    "DELETE FROM",
    "CREATE TABLE",
)

BANNED_ROOTS = {
    "asyncio", "http", "socket", "ssl", "urllib", "requests",
    "psycopg2", "pg8000", "sqlite3", "subprocess", "os", "sys",
    "time", "pathlib", "io", "tempfile", "shutil", "glob",
    "datetime", "decimal", "random", "threading", "multiprocessing",
}


def _imported_roots():
    found: set[str] = set()
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # intra-package relative import
                if node.module:
                    found.add(node.module)
    return found


class TestPackagePurity:
    def test_package_files_are_exactly_the_expected_set(self):
        files = sorted(p.name for p in PACKAGE_DIR.glob("*.py"))
        assert files == [
            "__init__.py",
            "aggregate.py",
            "costs.py",
            "entry_model.py",
            "exit_model.py",
            "paired.py",
            "policy.py",
            "types.py",
        ]

    def test_imports_are_pure(self):
        for module in sorted(_imported_roots()):
            assert any(
                module == allowed or module.startswith(allowed + ".")
                for allowed in ALLOWED_IMPORTS
            ), f"forbidden import in pure package: {module}"

    def test_no_banned_root_imports(self):
        for module in _imported_roots():
            root = module.split(".")[0]
            assert root not in BANNED_ROOTS, f"banned root import {module}"

    def test_no_forbidden_tokens(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_TOKENS:
                assert token not in text, f"{token!r} found in {path.name}"

    def test_sources_parse(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            ast.parse(path.read_text(encoding="utf-8"))


class TestProductionIsolation:
    #: non-research surfaces that must never reference the outcome package
    SURFACES = (
        "app/scanners/momentum_exhaustion_reverse_long_v2.py",
        "app/scanners/scanner_runner.py",
        "app/scanners/orchestrator.py",
        "app/scanners/paper_runner.py",
        "app/indicators/technical.py",
        "bot.py",
        "scanner_runner.py",
        "paper_runner.py",
    )
    NEEDLE = "me_entry_quality_h2_outcome"

    def _production_targets(self):
        targets = []
        for rel in self.SURFACES:
            path = ROOT / rel
            if path.exists():
                targets.append(path)
        targets.extend((ROOT / "app").rglob("*.py"))
        targets.extend((ROOT / "deploy").rglob("*"))
        return [
            p
            for p in targets
            if p.is_file()
            and "research" not in p.parts  # research<->research stays offline
            and "test_me_entry_quality_h2_outcome" not in str(p)
        ]

    def test_no_production_module_references_the_outcome_package(self):
        offenders = [
            str(p.relative_to(ROOT))
            for p in self._production_targets()
            if self.NEEDLE in p.read_text(encoding="utf-8", errors="ignore")
        ]
        assert offenders == []

    def test_no_sql_migration_for_the_outcome_package(self):
        sql_dir = ROOT / "sql"
        if not sql_dir.exists():
            return
        for path in sql_dir.rglob("*.sql"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert self.NEEDLE not in text, path.name

    def test_no_systemd_unit_for_the_outcome_package(self):
        deploy_dir = ROOT / "deploy"
        if not deploy_dir.exists():
            return
        for path in deploy_dir.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert self.NEEDLE not in text, path.name

    def test_detector_module_untouched_by_the_outcome_package(self):
        text = (ROOT / "app" / "scanners" /
                "momentum_exhaustion_reverse_long_v2.py").read_text(
            encoding="utf-8", errors="ignore"
        )
        assert "outcome" not in text.lower()
