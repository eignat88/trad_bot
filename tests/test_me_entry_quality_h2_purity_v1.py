"""Issue #298 — H2 package purity + production isolation tests.

The ``me_entry_quality_h2_v1`` package must stay pure research code:
no I/O, no network, no database, no clock reads, and no wiring into any
production surface (scanner, orchestrator, systemd, migrations).
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "app" / "research" / "me_entry_quality_h2_v1"

#: stdlib + pure research dependencies + this package's own modules
ALLOWED_IMPORTS = {
    "__future__",
    "collections",
    "dataclasses",
    "decimal",  # exact PostgreSQL NUMERIC handling — pure stdlib
    "hashlib",
    "json",
    "math",
    "typing",
    "app.models",
    "app.research.me_entry_quality_h1_v1",
    "app.research.me_entry_quality_h1c_v1",
    "app.research.me_entry_quality_h2_v1",
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


_PKG_PARTS = ("app", "research", "me_entry_quality_h2_v1")
_PKG_PREFIX = "app.research.me_entry_quality_h2_v1"


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
            "candle_provider.py",
            "pipeline.py",
            "result.py",
            "rsi_confirmation.py",
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

    def test_no_network_db_or_clock_module_imports(self):
        banned_roots = {
            "asyncio", "http", "socket", "ssl", "urllib", "requests",
            "psycopg2", "pg8000", "sqlite3", "subprocess", "os", "sys",
            "time", "pathlib", "io", "tempfile", "shutil", "glob",
        }
        found = _imported_roots()
        for module in found:
            root = module.split(".")[0]
            assert root not in banned_roots, f"banned root import {module}"


class TestProductionIsolation:
    #: non-research surfaces that must never reference the H2 package
    SURFACES = (
        "app/scanners/momentum_exhaustion_reverse_long_v2.py",
        "app/scanners/scanner_runner.py",
        "app/scanners/orchestrator.py",
        "app/scanners/paper_runner.py",
        "app/indicators/technical.py",
        "bot.py",
    )
    #: The pure engine (Issue #298/#311) is ``me_entry_quality_h2_v1``.  It
    #: must stay unwired from every production surface, migration and unit.
    ENGINE_NEEDLE = "me_entry_quality_h2_v1"
    #: Issue #312 authorizes ONE narrow exception surface: the research-only
    #: prospective runtime (``me_entry_quality_h2_prospective``) may appear in
    #: its own package, its own migration and disabled-by-default deploy
    #: templates — nowhere else.
    RUNTIME_NEEDLE = "me_entry_quality_h2_prospective"
    AUTHORIZED_RESEARCH_RUNTIME_SURFACES = (
        "deploy/env/h2-prospective-runtime.env.example",
        "deploy/systemd/trad-bot-h2-prospective-runtime.service",
        "deploy/sql/312_h2_prospective_role.sql",
        "sql/migrations/095_me_entry_quality_h2_prospective_v1.sql",
    )

    def _production_targets(self):
        targets = []
        for rel in self.SURFACES:
            path = ROOT / rel
            if path.exists():
                targets.append(path)
        targets.extend((ROOT / "app").rglob("*.py"))
        targets.extend((ROOT / "deploy").rglob("*"))
        return [
            p for p in targets
            if p.is_file()
            and "research" not in p.parts  # research<->research reuse is offline
            and "test_me_entry_quality_h2" not in str(p)
        ]

    def test_no_production_module_references_h2(self):
        # the pure H2 engine is never referenced from any production surface
        offenders = [
            str(p.relative_to(ROOT))
            for p in self._production_targets()
            if self.ENGINE_NEEDLE
            in p.read_text(encoding="utf-8", errors="ignore")
        ]
        assert offenders == []

    def test_prospective_runtime_confined_to_authorized_surfaces(self):
        # Issue #312: the prospective runtime may only appear inside its own
        # authorized (disabled-by-default) surfaces
        authorized = {
            str((ROOT / rel).resolve())
            for rel in self.AUTHORIZED_RESEARCH_RUNTIME_SURFACES
        }
        offenders = [
            str(p.relative_to(ROOT))
            for p in self._production_targets()
            if str(p.resolve()) not in authorized
            and self.RUNTIME_NEEDLE
            in p.read_text(encoding="utf-8", errors="ignore")
        ]
        assert offenders == []

    def test_no_sql_migration_for_h2(self):
        sql_dir = ROOT / "sql"
        if not sql_dir.exists():
            return
        for path in sql_dir.rglob("*.sql"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert self.ENGINE_NEEDLE not in text, path.name
            if self.RUNTIME_NEEDLE in text:
                assert path.name == "095_me_entry_quality_h2_prospective_v1.sql", (
                    f"unauthorized prospective-runtime migration: {path.name}"
                )

    def test_no_systemd_unit_for_h2(self):
        deploy_dir = ROOT / "deploy"
        if not deploy_dir.exists():
            return
        for path in deploy_dir.rglob("*.service"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert self.ENGINE_NEEDLE not in text, path.name
            if self.RUNTIME_NEEDLE in text:
                assert path.name == "trad-bot-h2-prospective-runtime.service", (
                    f"unauthorized prospective-runtime unit: {path.name}"
                )

    def test_detector_module_untouched_by_h2(self):
        text = (ROOT / "app" / "scanners" /
                "momentum_exhaustion_reverse_long_v2.py").read_text(
            encoding="utf-8", errors="ignore"
        )
        assert "h2" not in text.lower()
