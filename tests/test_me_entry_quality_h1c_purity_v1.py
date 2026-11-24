"""Issue #308 — H1-C package purity + production isolation tests.

The ``me_entry_quality_h1c_v1`` package must stay pure research code:
no I/O, no network, no database, no clock reads, and no wiring into any
production surface (scanner, orchestrator, systemd, migrations).
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "app" / "research" / "me_entry_quality_h1c_v1"

#: stdlib + the two pure research dependencies + this package's own modules
ALLOWED_IMPORTS = {
    "__future__",
    "dataclasses",
    "datetime",
    "hashlib",
    "math",
    "typing",
    "app.models",
    "app.research.me_entry_quality_h1_v1",
    "app.research.me_entry_quality_h1c_v1",
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


_PKG_PARTS = ("app", "research", "me_entry_quality_h1c_v1")
_PKG_PREFIX = "app.research.me_entry_quality_h1c_v1"


def _imported_roots() -> set[str]:
    found: set[str] = set()
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    base = _PKG_PARTS[: len(_PKG_PARTS) - (node.level - 1)]
                    resolved = ".".join(
                        list(base) + (node.module or "").split(".") if node.module
                        else list(base)
                    )
                    if resolved == _PKG_PREFIX or resolved.startswith(_PKG_PREFIX + "."):
                        continue  # intra-package — scanned on its own
                    found.add(resolved)
                elif node.module:
                    found.add(node.module)
    return found


def _assert_allowed(module: str) -> None:
    for allowed in ALLOWED_IMPORTS:
        if module == allowed or module.startswith(allowed + "."):
            return
    raise AssertionError(f"forbidden import in pure package: {module}")


class TestPackagePurity:
    def test_package_files_exist(self):
        files = sorted(p.name for p in PACKAGE_DIR.glob("*.py"))
        assert files == [
            "__init__.py",
            "event_adapter.py",
            "h1_result.py",
            "htf_provider.py",
            "pipeline.py",
        ]

    def test_imports_are_pure(self):
        for module in sorted(_imported_roots()):
            _assert_allowed(module)

    def test_no_forbidden_tokens(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_TOKENS:
                assert token not in text, f"{token!r} found in {path.name}"

    def test_sources_parse(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            ast.parse(path.read_text(encoding="utf-8"))


class TestProductionIsolation:
    def test_package_is_not_wired_into_any_production_surface(self):
        """No production module may import or reference the H1-C package."""
        targets = []
        for rel in (
            "app/scanners/swing_engine.py",
            "app/scanners/scanner_runner.py",
            "app/scanners/orchestrator.py",
            "app/scanners/htf_context.py",
            "app/scanners/paper_runner.py",
            "bot.py",
        ):
            path = ROOT / rel
            if path.exists():
                targets.append(path)
        targets.extend((ROOT / "app").rglob("*.py"))
        targets.extend((ROOT / "deploy").rglob("*"))
        needle = "me_entry_quality_h1c"
        offenders = [
            str(p.relative_to(ROOT))
            for p in targets
            if p.is_file()
            and "research" not in p.parts  # research↔research reuse is offline
            and needle in p.read_text(encoding="utf-8", errors="ignore")
            and "test_me_entry_quality_h1c" not in str(p)
        ]
        assert offenders == []

    def test_no_sql_migration_for_h1c(self):
        for path in (ROOT / "sql").rglob("*.sql"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "me_entry_quality_h1c" not in text, path.name

    def test_no_systemd_unit_for_h1c(self):
        for path in (ROOT / "deploy").rglob("*.service"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "me_entry_quality_h1c" not in text, path.name
