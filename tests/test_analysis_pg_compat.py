"""Tests for PostgreSQL compatibility of ME_REVERSE_LONG_V1_V2 analysis SQL.

Ensures:
1. No ROUND() call receives a double precision argument without explicit ::numeric cast.
2. No SQL references pt.features — features live in dds.scanner_setup.features.
3. SQL that extracts features uses JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id.
4. Safe NULLIF wrapping for JSONB-to-numeric casts.
5. market.candle is used (not dds.market_candle).
6. timeframe = '5' (not '5m').
7. is_closed = true filter present.
8. No look-ahead condition present.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_ROOT / "scripts" / "analysis" / "me_reverse_long_v1_v2"

# All SQL-bearing files to scan
SQL_FILES = sorted(ANALYSIS_DIR.glob("*.sql"))
PY_FILES = sorted(ANALYSIS_DIR.glob("*.py"))


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
        if stripped.startswith("--"):
            continue
        if re.search(r'\bpt\.features\b', line, re.IGNORECASE):
            violations.append(f"Line {i}: {stripped[:80]}")
        if re.search(r'\bpaper_trade\.features\b', line, re.IGNORECASE):
            violations.append(f"Line {i}: {stripped[:80]}")
    return violations


def _find_features_without_join(sql: str) -> list[str]:
    """Check if features are extracted but no JOIN to scanner_setup exists."""
    uses_ss_features = bool(re.search(r'\bss\.features\b', sql, re.IGNORECASE))
    has_join = bool(re.search(r'JOIN\s+dds\.scanner_setup\b', sql, re.IGNORECASE))

    if uses_ss_features and not has_join:
        return ["Uses ss.features but no JOIN dds.scanner_setup found"]
    return []


def _find_unsafe_feature_casts(sql: str) -> list[str]:
    """Find feature JSONB-to-numeric casts without NULLIF protection."""
    violations = []
    pattern = r"\(ss\.features->>'[^']+'\)::numeric"
    for match in re.finditer(pattern, sql):
        start = match.start()
        prefix = sql[max(0, start - 20):start]
        if "NULLIF" not in prefix.upper():
            line_num = sql[:match.start()].count("\n") + 1
            violations.append(f"Line ~{line_num}: {match.group()[:60]} — missing NULLIF wrapper")
    return violations


def _find_dds_market_candle(sql: str) -> list[str]:
    """Find references to dds.market_candle (should be market.candle)."""
    violations = []
    for i, line in enumerate(sql.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        if re.search(r'\bdds\.market_candle\b', line, re.IGNORECASE):
            violations.append(f"Line {i}: {stripped[:80]}")
    return violations


def _find_market_candle(sql: str) -> bool:
    """Check if market.candle is referenced."""
    return bool(re.search(r'\bmarket\.candle\b', sql, re.IGNORECASE))


def _find_timeframe_5m(sql: str) -> list[str]:
    """Find timeframe = '5m' (should be '5')."""
    violations = []
    for i, line in enumerate(sql.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        if re.search(r"timeframe\s*=\s*'5m'", line, re.IGNORECASE):
            violations.append(f"Line {i}: {stripped[:80]}")
    return violations


def _find_is_closed(sql: str) -> bool:
    """Check if is_closed filter is present."""
    return bool(re.search(r'\bis_closed\b', sql, re.IGNORECASE))


def _find_lookahead(sql: str) -> list[str]:
    """Check for potential look-ahead conditions.
    
    Look-ahead: mc.open_time > signal_time (should be <= not >).
    We check that there's no condition allowing candles AFTER signal time.
    """
    violations = []
    for i, line in enumerate(sql.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        # Check for open_time > signal (potential look-ahead)
        if re.search(r'mc\.open_time\s*>\s*sc\.signal_candle_ts', line, re.IGNORECASE):
            violations.append(f"Line {i}: potential look-ahead: {stripped[:80]}")
    return violations


# ============================================================
# ROUND() safety tests
# ============================================================

class TestSQLFilesRoundSafety:
    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_sql_file_round_safety(self, sql_file: Path):
        content = sql_file.read_text(encoding="utf-8")
        violations = _find_unsafe_round(content)
        assert not violations, (
            f"{sql_file.name} has ROUND() without ::numeric cast:\n"
            + "\n".join(violations)
        )


class TestPythonFilesRoundSafety:
    @pytest.mark.parametrize("py_file", PY_FILES, ids=lambda p: p.name)
    def test_python_file_sql_safety(self, py_file: Path):
        sql_strings = _extract_sql_strings(py_file)
        if not sql_strings:
            pytest.skip("No SQL strings found")
        all_violations = []
        for idx, sql in enumerate(sql_strings):
            violations = _find_unsafe_round(sql)
            for v in violations:
                all_violations.append(f"SQL block #{idx + 1}: {v}")
        assert not all_violations, (
            f"{py_file.name} has ROUND() without ::numeric cast:\n"
            + "\n".join(all_violations)
        )


class TestNoDoublePrecisionPatterns:
    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_no裸_avg_in_round(self, sql_file: Path):
        content = sql_file.read_text(encoding="utf-8")
        pattern = r'ROUND\s*\(\s*AVG\s*\([^)]+\)\s*,'
        matches = re.findall(pattern, content, re.IGNORECASE)
        assert not matches, (
            f"{sql_file.name}: Found ROUND(AVG(...), N) without ::numeric cast: {matches}"
        )

    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_no裸_percentile_in_round(self, sql_file: Path):
        content = sql_file.read_text(encoding="utf-8")
        pattern = r'ROUND\s*\(\s*PERCENTILE_CONT\s*\('
        matches = re.findall(pattern, content, re.IGNORECASE)
        assert not matches, (
            f"{sql_file.name}: Found ROUND(PERCENTILE_CONT...) without ::numeric cast: {matches}"
        )


# ============================================================
# pt.features must not exist
# ============================================================

class TestNoPtFeatures:
    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_no_pt_features_in_sql_files(self, sql_file: Path):
        content = sql_file.read_text(encoding="utf-8")
        violations = _find_pt_features_refs(content)
        assert not violations, (
            f"{sql_file.name} references pt.features (column does not exist):\n"
            + "\n".join(violations)
        )

    @pytest.mark.parametrize("py_file", PY_FILES, ids=lambda p: p.name)
    def test_no_pt_features_in_python_files(self, py_file: Path):
        content = py_file.read_text(encoding="utf-8")
        violations = _find_pt_features_refs(content)
        assert not violations, (
            f"{py_file.name} references pt.features (column does not exist):\n"
            + "\n".join(violations)
        )


class TestFeaturesUseScannerSetup:
    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_features_have_join(self, sql_file: Path):
        content = sql_file.read_text(encoding="utf-8")
        violations = _find_features_without_join(content)
        assert not violations, f"{sql_file.name}: {violations}"

    @pytest.mark.parametrize("py_file", PY_FILES, ids=lambda p: p.name)
    def test_python_features_have_join(self, py_file: Path):
        sql_strings = _extract_sql_strings(py_file)
        if not sql_strings:
            pytest.skip("No SQL strings found")
        all_violations = []
        for idx, sql in enumerate(sql_strings):
            violations = _find_features_without_join(sql)
            for v in violations:
                all_violations.append(f"SQL block #{idx + 1}: {v}")
        assert not all_violations, f"{py_file.name} features without JOIN:\n" + "\n".join(all_violations)


class TestSafeFeatureCasts:
    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_nullif_wrapper(self, sql_file: Path):
        content = sql_file.read_text(encoding="utf-8")
        violations = _find_unsafe_feature_casts(content)
        assert not violations, f"{sql_file.name}: Unsafe feature casts:\n" + "\n".join(violations)

    @pytest.mark.parametrize("py_file", PY_FILES, ids=lambda p: p.name)
    def test_python_nullif_wrapper(self, py_file: Path):
        sql_strings = _extract_sql_strings(py_file)
        if not sql_strings:
            pytest.skip("No SQL strings found")
        all_violations = []
        for idx, sql in enumerate(sql_strings):
            violations = _find_unsafe_feature_casts(sql)
            for v in violations:
                all_violations.append(f"SQL block #{idx + 1}: {v}")
        assert not all_violations, f"{py_file.name} unsafe feature casts:\n" + "\n".join(all_violations)


# ============================================================
# market.candle schema tests
# ============================================================

class TestMarketCandleSchema:
    """Verify correct schema usage: market.candle, timeframe='5', is_closed=true."""

    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_no_dds_market_candle(self, sql_file: Path):
        """dds.market_candle does not exist — must use market.candle."""
        content = sql_file.read_text(encoding="utf-8")
        violations = _find_dds_market_candle(content)
        assert not violations, (
            f"{sql_file.name} references dds.market_candle (should be market.candle):\n"
            + "\n".join(violations)
        )

    @pytest.mark.parametrize("py_file", PY_FILES, ids=lambda p: p.name)
    def test_python_no_dds_market_candle(self, py_file: Path):
        content = py_file.read_text(encoding="utf-8")
        violations = _find_dds_market_candle(content)
        assert not violations, (
            f"{py_file.name} references dds.market_candle (should be market.candle):\n"
            + "\n".join(violations)
        )

    @pytest.mark.parametrize("sql_file", SQL_FILES, ids=lambda p: p.name)
    def test_no_timeframe_5m(self, sql_file: Path):
        """timeframe='5m' is wrong — must be timeframe='5'."""
        content = sql_file.read_text(encoding="utf-8")
        violations = _find_timeframe_5m(content)
        assert not violations, (
            f"{sql_file.name} uses timeframe='5m' (should be '5'):\n"
            + "\n".join(violations)
        )

    @pytest.mark.parametrize("py_file", PY_FILES, ids=lambda p: p.name)
    def test_python_no_timeframe_5m(self, py_file: Path):
        content = py_file.read_text(encoding="utf-8")
        violations = _find_timeframe_5m(content)
        assert not violations, (
            f"{py_file.name} uses timeframe='5m' (should be '5'):\n"
            + "\n".join(violations)
        )


class TestCandleFilters:
    """Verify is_closed and no look-ahead conditions."""

    def test_sql_files_have_is_closed(self):
        """SQL files that use market.candle should have is_closed filter."""
        for sql_file in SQL_FILES:
            content = sql_file.read_text(encoding="utf-8")
            uses_candle = _find_market_candle(content)
            if uses_candle:
                has_is_closed = _find_is_closed(content)
                assert has_is_closed, (
                    f"{sql_file.name} uses market.candle but missing is_closed filter"
                )

    def test_python_files_have_is_closed(self):
        """Python files that use market.candle in SQL should have is_closed filter."""
        for py_file in PY_FILES:
            sql_strings = _extract_sql_strings(py_file)
            if not sql_strings:
                continue
            for idx, sql in enumerate(sql_strings):
                uses_candle = _find_market_candle(sql)
                if uses_candle:
                    has_is_closed = _find_is_closed(sql)
                    assert has_is_closed, (
                        f"{py_file.name} SQL block #{idx + 1} uses market.candle but missing is_closed filter"
                    )

    def test_no_lookahead(self):
        """No file should have open_time > signal_time (potential look-ahead)."""
        all_files = list(SQL_FILES) + list(PY_FILES)
        all_violations = []
        for f in all_files:
            content = f.read_text(encoding="utf-8")
            violations = _find_lookahead(content)
            for v in violations:
                all_violations.append(f"{f.name}: {v}")
        assert not all_violations, "Look-ahead violations:\n" + "\n".join(all_violations)
