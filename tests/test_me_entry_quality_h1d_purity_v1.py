"""Issue #314 / H1-D — research isolation & purity tests.

The ``me_entry_quality_h1d_v1`` package must stay research-only:

* no production trading modules beyond the SHARED Bybit client
  (``app.exchange.bybit_client`` + its ``app.config.Settings``), the frozen
  H1-C / H1-A packages and ``app.models``;
* no SQL, no systemd, no wiring into scanner/orchestrator/trading surfaces,
  no production writes;
* NO direct HTTP anywhere — the ONLY request path is
  ``BybitClient._public_get()``;
* native ``interval=D`` support lives ONLY in this research adapter and
  NEVER reaches ``BybitClient.get_klines_range()`` (whose minute-only
  semantics stay frozen);
* nothing OUTSIDE the package references it (purity check).

Deterministic and offline.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "app" / "research" / "me_entry_quality_h1d_v1"
H1C_DIR = ROOT / "app" / "research" / "me_entry_quality_h1c_v1"
BYBIT_CLIENT = ROOT / "app" / "exchange" / "bybit_client.py"

EXPECTED_FILES = [
    "__init__.py",
    "classification.py",
    "coverage.py",
    "htf_fetch.py",
    "provenance.py",
    "runner.py",
]

#: stdlib + the shared Bybit client (+ its Settings) + H1-A/H1-C + app.models
ALLOWED_IMPORTS = {
    "__future__",
    "argparse",
    "dataclasses",
    "datetime",
    "hashlib",
    "json",
    "math",
    "pathlib",
    "sys",
    "time",
    "typing",
    "app.models",
    "app.config",
    "app.exchange.bybit_client",
    "app.research.me_entry_quality_h1_v1",
    "app.research.me_entry_quality_h1c_v1",
    "app.research.me_entry_quality_h1d_v1",
}

#: tokens that must NEVER appear in the research package
FORBIDDEN_TOKENS = (
    "requests",
    "http.client",
    "urlopen",
    "urllib",
    "socket",
    "psycopg2",
    "pg8000",
    "sqlalchemy",
    "subprocess",
    "os.environ",
    "getenv(",
    "INSERT INTO",
    "UPDATE ",
    "DELETE FROM",
    "CREATE TABLE",
    "DROP TABLE",
    "systemd",
    "app.db",
    "app.execution",
    "app.scanners",
    "app.strategy",
    "app.risk",
    "app.paper",
    "app.storage",
)

#: clock / local-time reads are forbidden in the PURE modules; only the
#: runner (fetch timestamp) may read the clock.
CLOCK_TOKENS = ("time.time", "datetime.now", "utcnow(", "today(", "mktime(")
PURE_MODULES = ("coverage.py", "classification.py",
                "provenance.py", "htf_fetch.py")

_PKG_PREFIX = "app.research.me_entry_quality_h1d_v1"
_PKG_PARTS = ("app", "research", "me_entry_quality_h1d_v1")


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
                        continue  # intra-package
                    found.add(resolved)
                elif node.module:
                    found.add(node.module)
    return found


class TestPackagePurity:
    def test_package_files_are_exactly_the_substreams(self):
        files = sorted(p.name for p in PACKAGE_DIR.glob("*.py"))
        assert files == sorted(EXPECTED_FILES)

    def test_imports_are_research_scoped(self):
        for module in sorted(_imported_roots()):
            assert any(
                module == allowed or module.startswith(allowed + ".")
                for allowed in ALLOWED_IMPORTS
            ), f"forbidden import in research package: {module}"

    def test_no_forbidden_tokens(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_TOKENS:
                trailing = r"\b" if re.search(r"\w$", token) else ""
                match = re.search(r"\b" + re.escape(token) + trailing, text)
                assert match is None, f"{token!r} found in {path.name}"

    def test_no_clock_reads_in_pure_modules(self):
        for name in PURE_MODULES:
            text = (PACKAGE_DIR / name).read_text(encoding="utf-8")
            for token in CLOCK_TOKENS:
                assert token not in text, f"{token!r} found in {name}"

    def test_sources_parse(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            ast.parse(path.read_text(encoding="utf-8"))

    def test_no_direct_http_calls_anywhere_in_package(self):
        """No ``urlopen``/``Request``/``send`` — the token scan covers the
        known HTTP entry points; anything new must be reviewed manually."""
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "attr",
                                   getattr(node.func, "id", ""))
                    assert name not in {
                        "urlopen", "Request", "urlopen", "Session",
                        "create_connection",
                    }, f"direct HTTP call {name!r} in {path.name}"

    def test_only_public_get_reaches_the_client(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name) and \
                            node.func.value.id in {"client", "_client", "self.client"}:
                        assert node.func.attr in {"_public_get"}, (
                            f"client method {node.func.attr!r} used in "
                            f"{path.name} — only _public_get is allowed")


class TestNativeDStaysInResearchAdapter:
    def test_get_klines_range_semantics_are_frozen(self):
        """Minute-only contract unchanged: native ``D`` is still rejected."""
        from app.exchange.bybit_client import BybitClient
        client = object.__new__(BybitClient)
        with pytest.raises(ValueError):
            client.get_klines_range("SOXSUSDT", "D", start_ms=0, end_ms=1)
        with pytest.raises(ValueError):
            client.get_klines_range("SOXSUSDT", "1D", start_ms=0, end_ms=1)

    def test_native_d_mapping_lives_only_in_h1d(self):
        text = BYBIT_CLIENT.read_text(encoding="utf-8")
        assert '"D"' not in text and "'D'" not in text
        htf_fetch = (PACKAGE_DIR / "htf_fetch.py").read_text(encoding="utf-8")
        assert '"D"' in htf_fetch and '"240"' in htf_fetch

    def test_h1d_never_calls_get_klines_range(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = getattr(func, "attr", getattr(func, "id", ""))
                    assert name != "get_klines_range", (
                        f"get_klines_range called in {path.name}")


class TestProductionIsolation:
    def test_nothing_outside_the_package_references_h1d(self):
        """No production module may import or reference the H1-D package.

        SANCTIONED EXCEPTION (Issue #324): the research-only
        ``app/research/me_entry_quality_h1e_v1`` package is the single
        approved consumer of the FROZEN H1-D contracts — the #324 spec
        requires H1-E to integrate the H1-D ``240``/native ``D`` adapter
        and ``H1D_HTF_SNAPSHOT_2`` provenance UNCHANGED.  H1-E itself is
        RESEARCH_ONLY / NO_DEPLOY and is verified wiring-free by
        ``test_me_entry_quality_h1e_isolation_purity_v1.py``.
        """
        targets = []
        targets.extend((ROOT / "app").rglob("*.py"))
        targets.extend((ROOT / "deploy").rglob("*"))
        targets.extend((ROOT / "sql").rglob("*"))
        targets.extend((ROOT / "scripts").rglob("*.py"))
        targets.append(ROOT / "bot.py")
        offenders = []
        sanctioned = []
        for path in targets:
            if not path.is_file():
                continue
            if "me_entry_quality_h1d_v1" in path.parts:
                continue  # the package itself
            if "test_me_entry_quality_h1d" in path.name:
                continue  # its own tests
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "me_entry_quality_h1d" in text:
                if "me_entry_quality_h1e_v1" in path.parts:
                    sanctioned.append(str(path.relative_to(ROOT)))
                    continue
                offenders.append(str(path.relative_to(ROOT)))
        assert offenders == []
        # the sanctioned consumer set is EXACTLY the H1-E research package
        assert all(
            "me_entry_quality_h1e_v1" in Path(p).parts for p in sanctioned
        )
        assert all(
            p.startswith("app\\research\\") or p.startswith("app/research/")
            for p in sanctioned
        )

    def test_no_sql_migration_for_h1d(self):
        for path in (ROOT / "sql").rglob("*.sql"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "me_entry_quality_h1d" not in text, path.name

    def test_no_systemd_unit_for_h1d(self):
        for path in (ROOT / "deploy").rglob("*.service"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "me_entry_quality_h1d" not in text, path.name

    def test_h1c_package_is_untouched_by_h1d(self):
        """H1-D may import H1-C but never modify it (contract reuse)."""
        files = sorted(p.name for p in H1C_DIR.glob("*.py"))
        assert files == [
            "__init__.py",
            "event_adapter.py",
            "h1_result.py",
            "htf_provider.py",
            "pipeline.py",
        ]
