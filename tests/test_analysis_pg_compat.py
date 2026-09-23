"""Tests for PostgreSQL compatibility of ME_REVERSE_LONG_V1_V2 analysis SQL.

Ensures:
1. No ROUND() call receives a double precision argument without explicit ::numeric cast.
2. No SQL references pt.features — features live in dds.scanner_setup.features.
3. SQL that extracts features uses JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id.
4. Safe NULLIF wrapping for JSONB-to-numeric casts.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_ROOT / "scripts" / "analysis" / "me_reverse_long_v1_v2"

# All SQL-bearing files to scan
SQL_FILES = sorted(ANALYSIS_DIR.glob("*.sql"))
PY_FILE = ANALYSIS_DIR / "run_analysis.py"


def _extract_sql_strings(py_path: Path) -> list[str]:
    """Extract all triple-quoted SQL strings from a Python file."""
    content = py_path.read_text(encoding="utf-8")
    dq_pattern = r'(?:"""(.*?)""")'
    sq_pattern = r"""(?:'''(.*?)''')"""
    results = re.findall(dq_pattern, content, re.DOTALL)
    results += re.findall(sq_pattern, content, re.DOTALL)
    return results


def _find_unsafe_round(sql: str) -> list[str]:
    """Find ROUND() calls whose first argument is NOT wrapped with ::numeric."""
    violations = []
    i = 0
    text = sql
    while True:
        idx = text.find("ROUND(", i)
        if idx == -1:
            break

        # Skip if inside a comment
        line_start = text.rfind("\n", 0, idx) + 1
        line_text = text[line_start:idx]
        if "--" in line_text:
            i = idx + 6
            continue

        # Walk parens to find matching )
        depth = 0
        start = idx + 6
        j = start
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                if depth == 0:
                    break
                depth -= 1
            j += 1

        round_body = text[start:j]

        # Split on the LAST top-level comma
        comma_pos = -1
        d = 0
        for k in range(len(round_body) - 1, -1, -1):
            ch = round_body[k]
            if ch == ")":
                d += 1
            elif ch == "(":
                d -= 1
            elif ch == "," and d == 0:
                comma_pos = k
                break

        if comma_pos == -1:
            i = j + 1
            continue

        expr_part = round_body[:comma_pos].strip()

        if not expr_part.endswith("::numeric"):
            line_num = text[:idx].count("\n") + 1
            violations.append(
                f"Line ~{line_num}: ROUND({expr_part[:60]}...) — missing ::numeric cast"
            )

        i = j + 1

    return violations


def _find_pt_features_refs(sql: str) -> list[str]:
    """Find any reference to pt.features — this column does not exist."""
    violations = []
    for i, line in enumerate(sql.split("\n"), 1):
        stripped = line.strip()
        # Skip comments
        if stripped.startswith("--"):
            continue
        # Match pt.features or paper_trade.features
        if re.search(r'\bpt\.features\b', line, re.IGNORECASE):
            violations.append(f"Line {i}: {stripped[:80]}")
        if re.search(r'\bpaper_trade\.features\b', line, re.IGNORECASE):
            violations.append(f"Line {i}: {stripped[:80]}")
    return violations


def _find_features_without_join(sql: str) -> list[str]:
    """Check if features are extracted but no JOIN to scanner_setup exists.

    Only flags cases where ss.features is used but there's no JOIN dds.scanner_setup.
    """
    uses_ss_features = bool(re.search(r'\bss\.features\b', sql, re.IGNORECASE))
    has_join = bool(re.search(r'JOIN\s+dds\.scanner_setup\b', sql, re.IGNORECASE))

    if uses_ss_features and not has_join:
        return ["Uses ss.features but no JOIN dds.scanner_setup found"]
    return []


def _find_unsafe_feature_casts(sql: str) -> list[str]:
    """Find feature JSONB-to-numeric casts without NULLIF protection."""
    violations = []
    # Pattern: (ss.features->>'key')::numeric without NULLIF wrapper
    # Safe:   NULLIF(ss.features->>'key', '')::numeric
    # Unsafe: (ss.features->>'key')::numeric
    pattern = r"\(ss\.features->>'[^']+'\)::numeric"
    for match in re.finditer(pattern, sql):
        # Check it's not inside NULLIF
        start = match.start()
        prefix = sql[max(0, start - 20):start]
        if "NULLIF" not in prefix.upper():
            line_num = sql[:match.start()].count("\n") + 1
            violations.append(f"Line ~{line_num}: {match.group()[:60]} — missing NULLIF wrapper")
    return violations


# ============================================================
# ROUND() safety tests
# ============================================================

class TestSQLFilesRoundSafety:
    """Each standalone .sql file must use ::numeric with ROUND()."""

    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_sql_file_round_safety(self, sql_file: Path):
        content = sql_file.read_text(encoding="utf-8")
        violations = _find_unsafe_round(content)
        assert not violations, (
            f"{sql_file.name} has ROUND() without ::numeric cast:\n"
            + "\n".join(violations)
        )


class TestPythonFileRoundSafety:
    """SQL strings embedded in run_analysis.py must use ::numeric with ROUND()."""

    def test_run_analysis_sql_safety(self):
        assert PY_FILE.exists(), f"File not found: {PY_FILE}"
        sql_strings = _extract_sql_strings(PY_FILE)
        assert sql_strings, f"No SQL strings found in {PY_FILE.name}"

        all_violations = []
        for idx, sql in enumerate(sql_strings):
            violations = _find_unsafe_round(sql)
            for v in violations:
                all_violations.append(f"SQL block #{idx + 1}: {v}")

        assert not all_violations, (
            f"run_analysis.py has ROUND() without ::numeric cast:\n"
            + "\n".join(all_violations)
        )


class TestNoDoublePrecisionPatterns:
    """Additional check: scan for known problematic patterns."""

    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_no裸_avg_in_round(self, sql_file: Path):
        """ROUND(AVG(...), N) without ::numeric is the #1 offender."""
        content = sql_file.read_text(encoding="utf-8")
        pattern = r'ROUND\s*\(\s*AVG\s*\([^)]+\)\s*,'
        matches = re.findall(pattern, content, re.IGNORECASE)
        assert not matches, (
            f"{sql_file.name}: Found ROUND(AVG(...), N) without ::numeric cast: {matches}"
        )

    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_no裸_percentile_in_round(self, sql_file: Path):
        """ROUND(PERCENTILE_CONT(...)...) without ::numeric."""
        content = sql_file.read_text(encoding="utf-8")
        pattern = r'ROUND\s*\(\s*PERCENTILE_CONT\s*\('
        matches = re.findall(pattern, content, re.IGNORECASE)
        assert not matches, (
            f"{sql_file.name}: Found ROUND(PERCENTILE_CONT...) without ::numeric cast: {matches}"
        )


# ============================================================
# pt.features must not exist — features live in scanner_setup
# ============================================================

class TestNoPtFeatures:
    """dds.paper_trade has no 'features' column. All feature extraction
    must go through dds.scanner_setup.features via JOIN."""

    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_no_pt_features_in_sql_files(self, sql_file: Path):
        content = sql_file.read_text(encoding="utf-8")
        violations = _find_pt_features_refs(content)
        assert not violations, (
            f"{sql_file.name} references pt.features (column does not exist):\n"
            + "\n".join(violations)
        )

    def test_no_pt_features_in_run_analysis(self):
        assert PY_FILE.exists(), f"File not found: {PY_FILE}"
        content = PY_FILE.read_text(encoding="utf-8")
        violations = _find_pt_features_refs(content)
        assert not violations, (
            f"run_analysis.py references pt.features (column does not exist):\n"
            + "\n".join(violations)
        )


class TestFeaturesUseScannerSetup:
    """SQL that extracts features must JOIN dds.scanner_setup."""

    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_features_have_join(self, sql_file: Path):
        content = sql_file.read_text(encoding="utf-8")
        violations = _find_features_without_join(content)
        assert not violations, (
            f"{sql_file.name}: {violations}"
        )

    def test_run_analysis_features_have_join(self):
        assert PY_FILE.exists(), f"File not found: {PY_FILE}"
        sql_strings = _extract_sql_strings(PY_FILE)
        all_violations = []
        for idx, sql in enumerate(sql_strings):
            violations = _find_features_without_join(sql)
            for v in violations:
                all_violations.append(f"SQL block #{idx + 1}: {v}")
        assert not all_violations, (
            f"run_analysis.py features without JOIN:\n"
            + "\n".join(all_violations)
        )


class TestSafeFeatureCasts:
    """Feature JSONB-to-numeric casts should use NULLIF for empty string safety."""

    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_nullif_wrapper(self, sql_file: Path):
        content = sql_file.read_text(encoding="utf-8")
        violations = _find_unsafe_feature_casts(content)
        assert not violations, (
            f"{sql_file.name}: Unsafe feature casts:\n"
            + "\n".join(violations)
        )

    def test_run_analysis_nullif_wrapper(self):
        assert PY_FILE.exists(), f"File not found: {PY_FILE}"
        sql_strings = _extract_sql_strings(PY_FILE)
        all_violations = []
        for idx, sql in enumerate(sql_strings):
            violations = _find_unsafe_feature_casts(sql)
            for v in violations:
                all_violations.append(f"SQL block #{idx + 1}: {v}")
        assert not all_violations, (
            f"run_analysis.py unsafe feature casts:\n"
            + "\n".join(all_violations)
        )
