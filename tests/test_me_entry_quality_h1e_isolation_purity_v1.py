"""Issue #324 / H1-E — isolation & purity: no scanner/paper/evaluator
wiring, zero network/DB in Phase A, mirrored-hash parity with the frozen
#305 derivations, and deterministic identity across PYTHONHASHSEED.

Offline: zero network, zero production DB.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "app" / "research" / "me_entry_quality_h1e_v1"

FORBIDDEN_IMPORT_ROOTS = (
    "app.scanners",
    "app.paper",
    "app.execution",
    "app.exchange",
    "app.services",
    "app.db",
    "app.database",
    "scanner_runner",
    "paper_runner",
    "bot",
    "me_inverse_short_runner",
    "app.research.evaluator",
    "app.research.evaluator_runner",
    "app.research.observer",
    "app.research.prospective_observer",
    "app.research.prospective_evaluator",
    "app.research.repository",
    "app.research.me_upstream_source_v1",  # mirrored, never imported (isolation)
)

FORBIDDEN_IO_MODULES = (
    "socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp",
    "pg8000", "psycopg2", "psycopg", "sqlalchemy", "subprocess", "ftplib",
    "telnetlib", "paramiko", "asyncio",
)


def _module_files():
    return sorted(PACKAGE_DIR.glob("*.py"))


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


def test_package_modules_exist():
    files = _module_files()
    assert files, "H1-E package is empty"
    assert (PACKAGE_DIR / "protocol.py").is_file()
    assert (PACKAGE_DIR / "capture.py").is_file()
    assert (PACKAGE_DIR / "classification.py").is_file()
    assert (PACKAGE_DIR / "ledger.py").is_file()


def test_no_scanner_paper_or_evaluator_wiring():
    for path in _module_files():
        for name in _imported_names(path):
            for root in FORBIDDEN_IMPORT_ROOTS:
                assert not (name == root or name.startswith(root + ".")), (
                    f"{path.name} imports {name} — forbidden wiring"
                )


def test_zero_network_and_db_imports_in_phase_a():
    for path in _module_files():
        for name in _imported_names(path):
            root = name.split(".")[0]
            assert root not in FORBIDDEN_IO_MODULES, (
                f"{path.name} imports {name} — Phase A must be offline"
            )


def test_no_ddl_execution_and_no_production_sql():
    for path in _module_files():
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "CREATE TABLE", "DROP TABLE", "ALTER TABLE", "INSERT INTO",
            "psycopg", "connect(", "cursor()", "requests.", "urlopen",
        ):
            assert forbidden not in source, (
                f"{path.name} contains {forbidden!r} — DDL/DB/HTTP is out of "
                "Phase A scope"
            )


def test_mirrored_hash_functions_match_the_frozen_source305_ones():
    # parity with the frozen #305 derivations (tests may import them; the
    # observer package itself never does)
    from app.research.me_upstream_source_v1.contracts import (
        lineage_hash as source305_lineage_hash,
    )
    from app.research.me_upstream_source_v1.identity import (
        identity_key_from_normalized,
    )
    from app.research.me_entry_quality_h1e_v1.contracts import (
        canonical_lineage_hash,
        expected_source_event_key,
    )

    lineage = {
        "source_identity": "SCANNER|SYM|LONG|5m|1700000000000",
        "source_pipeline_fingerprint": "abc",
        "nested": {"a": 1, "b": [1, 2, 3]},
    }
    assert canonical_lineage_hash(lineage) == source305_lineage_hash(lineage)

    normalized = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|SYM|LONG|5m|1700000000000"
    assert expected_source_event_key(normalized) == (
        identity_key_from_normalized(normalized))


_DETERMINISM_SCRIPT = r"""
import json, sys
sys.path.insert(0, r"{root}")
from tests.h1e_v1_fixtures import FakeReader, make_classification_record, make_source_row
from app.research.me_entry_quality_h1e_v1.capture import CaptureConfig, CaptureCursor, run_capture_cycle
from app.research.me_entry_quality_h1e_v1.contracts import H1EFreeze
from app.research.me_entry_quality_h1e_v1.ledger import InMemoryH1EStore

freeze = H1EFreeze.synthetic_fixture(1_798_761_600_000_000)
rows = [
    make_source_row(signal_time_us=1_798_761_610_000_000 + i * 10_000_000,
                    captured_us=1_798_761_700_000_000 + i * 1_000_000,
                    candle_open_ms=1_700_000_000_000 + i * 300_000)
    for i in range(3)
]
store = InMemoryH1EStore()
report = run_capture_cycle(
    reader=FakeReader(rows), store=store,
    classifier=lambda event, prior_deferrals=0: make_classification_record(event),
    freeze=freeze, cursor=CaptureCursor(),
    config=CaptureConfig(allow_fixture_freeze=True))
document = {{
    "report": report.to_dict(),
    "entries": [
        {{"identity": e.ledger_identity, "payload": e.payload,
          "classification": e.final_classification}}
        for e in store.entries()
    ],
}}
with open(r"{out}", "w", encoding="utf-8") as handle:
    json.dump(document, handle, sort_keys=True, ensure_ascii=True)
"""


def test_deterministic_identity_across_pythonhashseed(tmp_path):
    outputs = []
    for seed in ("1", "2654435761"):
        out_path = tmp_path / f"hashseed_{seed}.json"
        script = _DETERMINISM_SCRIPT.format(
            root=str(REPO_ROOT), out=str(out_path))
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=True,
        )
        outputs.append(out_path.read_bytes())
    assert outputs[0] == outputs[1], (
        "identity/ledger output differs across PYTHONHASHSEED values")
    document = json.loads(outputs[0])
    assert document["report"]["admitted_count"] == 3
    assert len(document["entries"]) == 3
