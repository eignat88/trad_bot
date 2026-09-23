"""Tests for PostgreSQL compatibility of ME_REVERSE_LONG_V1_V2 analysis SQL.

Ensures that no ROUND() call receives a double precision argument without
explicit ::numeric cast.  PostgreSQL raises:
    function round(double precision, integer) does not exist

This happens when ROUND wraps AVG(), PERCENTILE_CONT(), SUM()/COUNT() divisions,
or other expressions that return double precision.

The rule: every ROUND(expr, N) must have expr wrapped as (expr)::numeric.
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
    # Match triple-quoted strings (both """ and ''')
    dq_pattern = r'(?:"""(.*?)""")'
    sq_pattern = r"""(?:'''(.*?)''')"""
    results = re.findall(dq_pattern, content, re.DOTALL)
    results += re.findall(sq_pattern, content, re.DOTALL)
    return results


def _find_unsafe_round(sql: str) -> list[str]:
    """Find ROUND() calls whose first argument is NOT wrapped with ::numeric.

    Safe:   ROUND(AVG(x)::numeric, 4)
            ROUND((SUM(a) / SUM(b))::numeric, 4)
            ROUND(SUM(x)::numeric, 2)

    Unsafe: ROUND(AVG(x), 4)
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x), 2)
            ROUND(SUM(a) / SUM(b), 4)
    """
    violations = []

    # Find all ROUND(...) calls — need to handle nested parens
    # Strategy: find ROUND( then walk parens to find the matching )
    i = 0
    text = sql
    while True:
        idx = text.find("ROUND(", i)
        if idx == -1:
            break

        # Skip if this is inside a comment
        line_start = text.rfind("\n", 0, idx) + 1
        line_text = text[line_start:idx]
        if "--" in line_text:
            i = idx + 6
            continue

        # Walk parens to find matching )
        depth = 0
        start = idx + 6  # after "ROUND("
        j = start
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                if depth == 0:
                    break
                depth -= 1
            j += 1

        round_body = text[start:j]  # content inside ROUND(...)

        # Split on the LAST comma to get (expr, precision)
        # But we need to be careful about nested parens
        # Find the top-level comma separating expr from precision
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

        # Check if the expression ends with ::numeric
        # Allow cases like: (expr)::numeric, AVG(x)::numeric, SUM(x)::numeric
        if not expr_part.endswith("::numeric"):
            # Get the line for context
            line_num = text[:idx].count("\n") + 1
            violations.append(
                f"Line ~{line_num}: ROUND({expr_part[:60]}...) — missing ::numeric cast"
            )

        i = j + 1

    return violations


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
        # Match ROUND(AVG(...)  without ::numeric before the comma
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
