"""READ-ONLY SRR historical candle-coverage audit — A01..A15 + PR #265 P2.

Covers ``tools/research/srr_research_outcome_candle_coverage_audit_v1.py``
and ``sql/srr_research_outcome_candle_coverage_audit_v1.sql``:

* A01  ``--print-sql`` works without any CSV (exit 0);
* A02  analysis without ``--outcomes`` fails with a clear error;
* A03  a correct synthetic CSV produces a report;
* A04  wrong CSV columns fail closed (no report written);
* A05  an invalid timestamp raises before any verdict exists;
* A06  ``--guarded-from`` WITHOUT proof never yields COVERAGE_PROVEN;
* A07  a legacy finalized outcome is marked legacy, still UNVERIFIED;
* A08  a late evaluation produces a risk flag, never a proven defect;
* A09  a proven invariant violation yields COVERAGE_DEFECT;
* A10  a clean historical outcome without proof stays COVERAGE_UNVERIFIED;
* A11  identical CSVs classify deterministically;
* A12  the SQL file and the embedded SQL constants stay in sync;
* A13  ``--print-sql`` performs no connection and writes no file;
* A14  JSON and Markdown totals/statuses agree;
* A15  the READ-ONLY guard rejects DML/DDL and the SQL file stays read-only.

PR #265 review findings:

* P2-01  horizon-suffixed flags (``LATE_EVAL_TRUNCATED_15m``) are counted,
         detailed and rendered by the SAME recognition mechanism;
* P2-02  legacy latest-N thresholds apply only to horizons evaluated
         BEFORE ``--guarded-from``; structural checks apply to all periods;
         a rollout timestamp alone is never coverage proof.

Synthetic data, ``tmp_path`` and monkeypatched sockets only: no
production PostgreSQL, no Bybit API, no trading infrastructure.
"""
from __future__ import annotations

import ast
import csv
import json
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.research import (
    srr_research_outcome_candle_coverage_audit_v1 as audit,
)

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[1]
SQL_FILE = REPO_ROOT / "sql" / "srr_research_outcome_candle_coverage_audit_v1.sql"

SIGNAL = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
ROLLOUT = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)


# ── helpers ──────────────────────────────────────────────────────────


def iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat().replace("+00:00", "Z")


def normal_marks(signal_time: datetime) -> dict[str, datetime | None]:
    return {h: signal_time + timedelta(minutes=audit.HORIZON_MINUTES[h])
            for h in audit.HORIZONS}


def make_row(
    signal_id: int = 1,
    *,
    signal_time: datetime = SIGNAL,
    marks: dict[str, datetime | None] | None = None,
    mfe: dict[str, float | None] | None = None,
    is_final: bool = True,
    updated_at: datetime | None = None,
    symbol: str = "BTCUSDT",
) -> audit.OutcomeRow:
    if marks is None:
        marks = normal_marks(signal_time)
    if mfe is None:
        mfe = {h: 0.5 for h in audit.HORIZONS}
    if updated_at is None:
        concrete = [m for m in marks.values() if m is not None]
        updated_at = max(concrete) if concrete else signal_time
    return audit.OutcomeRow(
        signal_id=signal_id,
        experiment_id=audit.EXPERIMENT_ID,
        symbol=symbol,
        signal_time=signal_time,
        is_final=is_final,
        marks=dict(marks),
        mfe=dict(mfe),
        created_at=signal_time,
        updated_at=updated_at,
    )


def csv_dict(
    signal_id: int = 1,
    *,
    signal_time: datetime = SIGNAL,
    marks: dict[str, datetime | None] | None = None,
    mfe: dict[str, float | None] | None = None,
    is_final: bool = True,
    updated_at: datetime | None = None,
    symbol: str = "BTCUSDT",
) -> dict[str, str]:
    if marks is None:
        marks = normal_marks(signal_time)
    if mfe is None:
        mfe = {h: 0.5 for h in audit.HORIZONS}
    if updated_at is None:
        updated_at = signal_time + timedelta(hours=1)
    return {
        "signal_id": str(signal_id),
        "experiment_id": audit.EXPERIMENT_ID,
        "symbol": symbol,
        "signal_time": iso(signal_time),
        "is_final": "true" if is_final else "false",
        **{f"evaluated_{h}_at": iso(marks[h]) if marks.get(h) else ""
           for h in audit.HORIZONS},
        **{f"mfe_{h}": "" if mfe.get(h) is None else str(mfe[h])
           for h in audit.HORIZONS},
        "created_at": iso(signal_time),
        "updated_at": iso(updated_at),
    }


def write_outcomes(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(audit.OUTCOME_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def write_signals(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(audit.SIGNAL_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def normalize(sql: str) -> str:
    return " ".join(re.sub(r"--[^\n]*", " ", sql).split())


def parse_sql_file(text: str) -> tuple[list[str], dict[str, str]]:
    order: list[str] = []
    blocks: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        match = re.match(r"--\s*name:\s*(\w+)\s*$", line.strip())
        if match:
            if current is not None:
                blocks[current] = "\n".join(buffer).strip()
            current = match.group(1)
            order.append(current)
            buffer = []
        elif current is not None and not line.strip().startswith("--"):
            buffer.append(line)
    if current is not None:
        blocks[current] = "\n".join(buffer).strip()
    return order, blocks


def export_columns(sql: str) -> list[str]:
    head = " ".join(sql.split()).replace("SELECT", "", 1).split(" FROM ")[0]
    return [part.strip().rsplit(".", 1)[-1] for part in head.split(",")]


def import_names() -> set[str]:
    source = Path(audit.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


# ════════════════════════════════════════════════════════════════════
# A01 — --print-sql without any CSV
# ════════════════════════════════════════════════════════════════════


def test_a01_print_sql_without_csv_exits_zero(capsys):
    code = audit.main(["--print-sql"])
    out = capsys.readouterr().out

    assert code == 0
    assert out.count("-- SQL_") == len(audit.SQL_STATEMENTS) == 6
    for name in audit.SQL_STATEMENTS:
        assert f"-- {name}" in out
    assert "SELECT" in out
    assert "FILTER (WHERE o.is_final)" in out          # SQL_TOTALS
    assert "finalized_missing_marks" in out            # SQL_FINALIZED_MISSING_MARKS
    assert "early_marks" in out                        # SQL_EARLY_MARKS
    assert "null_mark_values" in out                   # SQL_NULL_MARK_VALUES
    assert "o.evaluated_240m_at" in out                # SQL_OUTCOMES_EXPORT
    assert "ORDER BY signal_time, signal_id" in out    # SQL_SIGNALS_EXPORT
    # every printed statement passes the read-only guard
    for sql in audit.SQL_STATEMENTS.values():
        audit.assert_read_only(sql)


# ════════════════════════════════════════════════════════════════════
# A02 — analysis without --outcomes
# ════════════════════════════════════════════════════════════════════


def test_a02_analysis_without_outcomes_is_a_clear_error(tmp_path, capsys):
    report = tmp_path / "report.md"

    with pytest.raises(SystemExit) as excinfo:
        audit.main(["--report", str(report)])

    assert excinfo.value.code != 0
    err = capsys.readouterr().err
    assert "--outcomes" in err
    assert "print-sql" in err          # tells the operator the way out
    assert not report.exists(), "no report may be written on input error"


# ════════════════════════════════════════════════════════════════════
# A03 — correct synthetic CSV produces a report
# ════════════════════════════════════════════════════════════════════


def test_a03_valid_synthetic_csv_produces_report(tmp_path, capsys):
    outcomes = write_outcomes(tmp_path / "outcomes.csv", [
        csv_dict(1),
        csv_dict(2, signal_time=SIGNAL + timedelta(hours=3)),
    ])
    report = tmp_path / "report.md"
    json_path = tmp_path / "report.json"

    code = audit.main([
        "--outcomes", str(outcomes),
        "--report", str(report),
        "--json", str(json_path),
    ])

    assert code == 0
    text = report.read_text(encoding="utf-8")
    assert text.startswith("# SRR Research Outcome")
    assert "| outcomes | 2 |" in text
    assert "| finalized | 2 |" in text
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["totals"]["outcomes"] == 2
    assert payload["mode"] == "READ_ONLY_OFFLINE"
    out = capsys.readouterr().out
    assert "outcomes=2" in out
    assert "coverage_proven=0" in out


# ════════════════════════════════════════════════════════════════════
# A04 — wrong CSV columns fail closed
# ════════════════════════════════════════════════════════════════════


def test_a04_wrong_csv_columns_fail_closed(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("signal_id,symbol\n1,BTCUSDT\n", encoding="utf-8")
    report = tmp_path / "report.md"

    with pytest.raises(ValueError, match="missing required columns"):
        audit.main(["--outcomes", str(bad), "--report", str(report)])

    assert not report.exists(), "fail-closed: no report from a bad export"


# ════════════════════════════════════════════════════════════════════
# A05 — invalid timestamp: error, no false verdict
# ════════════════════════════════════════════════════════════════════


def test_a05_invalid_timestamp_errors_without_verdict(tmp_path):
    row = csv_dict(1)
    row["signal_time"] = "not-a-date"
    outcomes = write_outcomes(tmp_path / "outcomes.csv", [row])
    report = tmp_path / "report.md"

    with pytest.raises(ValueError):
        audit.main(["--outcomes", str(outcomes), "--report", str(report)])

    assert not report.exists(), "an unparseable input never yields a verdict"


# ════════════════════════════════════════════════════════════════════
# A06 — guarded_from without proof stays UNVERIFIED
# ════════════════════════════════════════════════════════════════════


def test_a06_guarded_from_alone_never_proves_coverage():
    rows = [make_row(1), make_row(2, signal_time=SIGNAL + timedelta(hours=2))]

    # Rollout timestamp set, no per-record proof: nothing is proven.
    result = audit.classify(rows, guarded_from=ROLLOUT,
                            generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    assert result.totals["coverage_proven_guarded"] == 0
    assert all(v.coverage_status == audit.COVERAGE_UNVERIFIED
               for v in result.verdicts)
    assert result.params["guard_proof_ids"] == []
    assert result.params["legacy_threshold_scope"] == (
        "pre_rollout_horizons_only")

    # The ONLY route to COVERAGE_PROVEN is explicit per-record evidence.
    proven = audit.classify(rows, guarded_from=ROLLOUT, guard_proof=[1],
                            generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    by_id = {v.signal_id: v.coverage_status for v in proven.verdicts}
    assert by_id[1] == audit.COVERAGE_PROVEN_BY_GUARDED_EVALUATOR
    assert by_id[2] == audit.COVERAGE_UNVERIFIED
    assert proven.totals["coverage_proven_guarded"] == 1


def test_a06_cli_guard_proof_ids_is_explicit(tmp_path, capsys):
    outcomes = write_outcomes(tmp_path / "outcomes.csv", [csv_dict(1)])
    json_path = tmp_path / "report.json"

    audit.main([
        "--outcomes", str(outcomes),
        "--guarded-from", iso(ROLLOUT),
        "--json", str(json_path),
    ])
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["totals"]["coverage_proven_guarded"] == 0

    audit.main([
        "--outcomes", str(outcomes),
        "--guarded-from", iso(ROLLOUT),
        "--guard-proof-ids", "1",
        "--json", str(json_path),
    ])
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["totals"]["coverage_proven_guarded"] == 1
    assert payload["params"]["guard_proof_ids"] == [1]


# ════════════════════════════════════════════════════════════════════
# A07 — legacy finalized outcome
# ════════════════════════════════════════════════════════════════════


def test_a07_legacy_finalized_outcome_is_marked_legacy():
    row = make_row(7, updated_at=SIGNAL + timedelta(hours=5))

    result = audit.classify([row], guarded_from=ROLLOUT,
                            generated_at=datetime(2026, 11, 1, tzinfo=UTC))

    verdict = result.verdicts[0]
    assert verdict.has_flag(audit.FLAG_FINALIZED_BY_LEGACY)
    assert verdict.coverage_status == audit.COVERAGE_UNVERIFIED
    assert result.totals["finalized_by_legacy"] == 1
    assert result.totals["coverage_proven_guarded"] == 0


# ════════════════════════════════════════════════════════════════════
# A08 — late evaluation: risk flag, never a proven defect
# ════════════════════════════════════════════════════════════════════


def test_a08_late_evaluation_produces_risk_flag_not_defect():
    signal_time = SIGNAL
    late = signal_time + timedelta(minutes=4975)   # risk zone, < truncated
    marks = {h: late for h in audit.HORIZONS}
    row = make_row(3, signal_time=signal_time, marks=marks, updated_at=late)

    result = audit.classify([row],
                            generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    verdict = result.verdicts[0]

    assert "LATE_EVAL_RISK_ZONE_15m" in verdict.flags
    assert "LATE_EVAL_RISK_ZONE_240m" in verdict.flags
    assert verdict.late_horizons == list(audit.HORIZONS)
    # A lag is a RISK FACTOR — it never proves an MFE/MAE or TP/SL error.
    assert verdict.coverage_status == audit.COVERAGE_UNVERIFIED
    assert verdict.reconstruction_required is True   # review trigger only
    assert result.totals["late_eval_risk"] == 1
    assert result.totals["potentially_incomplete"] == 1

    # beyond the truncation threshold -> the stronger indicator
    deeper = signal_time + timedelta(minutes=6000)
    marks2 = {h: deeper for h in audit.HORIZONS}
    result2 = audit.classify(
        [make_row(4, signal_time=signal_time, marks=marks2, updated_at=deeper)],
        generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    flags2 = result2.verdicts[0].flags
    assert "LATE_EVAL_TRUNCATED_15m" in flags2
    assert not any(f.startswith("LATE_EVAL_RISK_ZONE") for f in flags2)
    assert result2.totals["late_eval_truncated"] == 1

    # terminology in the rendered report (Defect 4)
    md = audit.render_markdown(result)
    section3 = md.split("## 3.")[1].split("## 4.")[0]
    assert "RISK FACTOR" in section3
    assert "does NOT prove historical candle completeness" in section3
    assert "had hit the 1000-bar cap" not in md   # no fact-claim of damage
    assert "batch" in md.split("## 7.")[1]        # grouping limitation


# ════════════════════════════════════════════════════════════════════
# A09 — proven invariant violation -> COVERAGE_DEFECT
# ════════════════════════════════════════════════════════════════════


def test_a09_proven_invariant_violation_is_coverage_defect():
    # (a) finalized row missing a horizon mark
    marks = normal_marks(SIGNAL)
    marks["240m"] = None
    missing = make_row(11, marks=marks)
    result_a = audit.classify([missing],
                              generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    assert result_a.verdicts[0].coverage_status == audit.COVERAGE_DEFECT
    assert result_a.verdicts[0].has_flag(audit.FLAG_FINALIZED_MISSING_MARK)

    # (b) a mark whose MFE stayed NULL: the frozen formula found no candle
    no_mfe = make_row(12, mfe={**{h: 0.5 for h in audit.HORIZONS},
                               "60m": None})
    result_b = audit.classify([no_mfe],
                              generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    verdict_b = result_b.verdicts[0]
    assert verdict_b.coverage_status == audit.COVERAGE_DEFECT
    assert verdict_b.has_flag(audit.FLAG_NULL_MARK_VALUE)
    assert verdict_b.has_flag("NULL_MARK_VALUE_60m")   # horizon form too
    assert result_b.totals["null_mark_values"] == 1


# ════════════════════════════════════════════════════════════════════
# A10 — clean historical outcome without proof -> UNVERIFIED
# ════════════════════════════════════════════════════════════════════


def test_a10_clean_historical_outcome_without_proof_is_unverified():
    row = make_row(5)

    result = audit.classify([row],
                            generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    verdict = result.verdicts[0]

    assert verdict.flags == [audit.FLAG_FINALIZED_BY_LEGACY]
    assert verdict.coverage_status == audit.COVERAGE_UNVERIFIED
    assert verdict.reconstruction_required is False
    assert result.totals["coverage_unverified"] == 1
    assert result.totals["coverage_defect"] == 0
    assert result.totals["coverage_proven_guarded"] == 0


# ════════════════════════════════════════════════════════════════════
# A11 — determinism on identical CSVs
# ════════════════════════════════════════════════════════════════════


def test_a11_identical_inputs_classify_deterministically(tmp_path):
    outcomes = write_outcomes(tmp_path / "outcomes.csv", [
        csv_dict(1),
        csv_dict(2, signal_time=SIGNAL + timedelta(minutes=4975),
                 marks={h: SIGNAL + timedelta(minutes=4975)
                        for h in audit.HORIZONS},
                 updated_at=SIGNAL + timedelta(minutes=4975)),
    ])
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    argv = ["--outcomes", str(outcomes), "--guarded-from", iso(ROLLOUT)]

    audit.main([*argv, "--json", str(first)])
    audit.main([*argv, "--json", str(second)])

    a = json.loads(first.read_text(encoding="utf-8"))
    b = json.loads(second.read_text(encoding="utf-8"))
    a.pop("generated_at")
    b.pop("generated_at")
    assert a == b
    # and the classification itself is stable under re-analysis
    rows = audit.load_outcomes_csv(outcomes)
    r1 = audit.classify(rows, guarded_from=ROLLOUT,
                        generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    r2 = audit.classify(rows, guarded_from=ROLLOUT,
                        generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    assert audit.result_to_json(r1) == audit.result_to_json(r2)


# ════════════════════════════════════════════════════════════════════
# A12 — SQL file ↔ embedded SQL consistency
# ════════════════════════════════════════════════════════════════════


def test_a12_sql_file_matches_embedded_sql_constants():
    assert SQL_FILE.exists(), "the documented export file must exist"
    text = SQL_FILE.read_text(encoding="utf-8")
    order, blocks = parse_sql_file(text)

    # same six statements, same order
    assert order == list(audit.SQL_STATEMENTS)

    for name in ("SQL_TOTALS", "SQL_FINALIZED_MISSING_MARKS",
                 "SQL_EARLY_MARKS", "SQL_NULL_MARK_VALUES"):
        block = blocks[name]
        if block.endswith(";"):
            block = block[:-1]
        assert normalize(block) == normalize(audit.SQL_STATEMENTS[name]), name
        audit.assert_read_only(block)

    # the export blocks are client-side \copy commands wrapping the same
    # SELECTs that the Python module embeds
    for name, filename in (
        ("SQL_OUTCOMES_EXPORT", "srr_coverage_outcomes.csv"),
        ("SQL_SIGNALS_EXPORT", "srr_coverage_signals.csv"),
    ):
        match = re.search(
            r"\\copy\s*\((.*)\)\s*TO\s+'([^']+)'\s+WITH\s+CSV\s+HEADER",
            blocks[name], re.DOTALL)
        assert match, f"{name}: documented \\copy export command missing"
        inner, target = match.group(1), match.group(2)
        assert target == filename
        assert normalize(inner) == normalize(audit.SQL_STATEMENTS[name])
        audit.assert_read_only(inner)

    # CSV column names and order agree with the Python constants
    assert export_columns(audit.SQL_OUTCOMES_EXPORT) == list(
        audit.OUTCOME_COLUMNS)
    assert export_columns(audit.SQL_SIGNALS_EXPORT) == list(
        audit.SIGNAL_COLUMNS)
    # the file is explicitly documented as client-side / never auto-run
    assert "NEVER store" in text or "never store" in text
    assert "NEVER reads" in text or "never reads" in text
    assert "experiment_id = 'SRR_LONG_OUTCOME_V1'" in text


# ════════════════════════════════════════════════════════════════════
# A13 — --print-sql: no connections, no file writes
# ════════════════════════════════════════════════════════════════════


def test_a13_print_sql_opens_no_connection_and_writes_no_file(
        tmp_path, monkeypatch, capsys):
    def blocked(*_args, **_kwargs):
        raise AssertionError("network access attempted during --print-sql")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    code = audit.main(["--print-sql"])

    assert code == 0
    assert list(tmp_path.iterdir()) == [], "--print-sql must not create files"
    assert capsys.readouterr().out.count("-- SQL_") == 6
    # the module never imports a DB driver, an HTTP client or sockets
    assert import_names().isdisjoint(
        {"psycopg2", "asyncpg", "requests", "urllib", "socket", "http",
         "pybit", "aiohttp", "asyncio"})


# ════════════════════════════════════════════════════════════════════
# A14 — JSON and Markdown totals/statuses agree
# ════════════════════════════════════════════════════════════════════


def test_a14_json_and_markdown_totals_agree(tmp_path):
    outcomes = write_outcomes(tmp_path / "outcomes.csv", [
        csv_dict(1),
        csv_dict(2, signal_time=SIGNAL + timedelta(minutes=6000),
                 marks={h: SIGNAL + timedelta(minutes=6000)
                        for h in audit.HORIZONS},
                 updated_at=SIGNAL + timedelta(minutes=6000)),
        csv_dict(3, signal_time=SIGNAL + timedelta(hours=5),
                 mfe={**{h: 0.5 for h in audit.HORIZONS}, "120m": None},
                 updated_at=SIGNAL + timedelta(hours=6)),
    ])
    signals = write_signals(tmp_path / "signals.csv", [
        {"signal_id": "1", "experiment_id": audit.EXPERIMENT_ID,
         "symbol": "BTCUSDT", "signal_time": iso(SIGNAL)},
        {"signal_id": "99", "experiment_id": audit.EXPERIMENT_ID,
         "symbol": "BTCUSDT",
         "signal_time": iso(SIGNAL + timedelta(days=40))},  # no outcome
    ])
    report = tmp_path / "report.md"
    json_path = tmp_path / "report.json"

    audit.main([
        "--outcomes", str(outcomes),
        "--signals", str(signals),
        "--report", str(report),
        "--json", str(json_path),
    ])

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    totals = payload["totals"]
    md = report.read_text(encoding="utf-8")

    def md_count(section: str) -> int:
        chunk = md.split(section)[1].split("## ")[0]
        return int(re.search(r"Count: \*\*(\d+)\*\*", chunk).group(1))

    # snapshot table agrees
    assert f"| outcomes | {totals['outcomes']} |" in md
    assert f"| finalized | {totals['finalized']} |" in md
    assert f"| signals without outcome | " \
           f"{totals['signals_without_outcome']} |" in md
    # section counts agree with the JSON totals
    assert md_count("## 3.") == totals["potentially_incomplete"]
    assert md_count("## 4.") == totals["reconstruction_required"]
    assert md_count("## 5.") == totals["coverage_unverified"]
    # statuses are from the fixed vocabulary and sum up
    statuses = [row["coverage_status"] for row in payload["rows"]]
    vocabulary = {audit.COVERAGE_UNVERIFIED, audit.COVERAGE_DEFECT,
                  audit.COVERAGE_PROVEN_BY_GUARDED_EVALUATOR}
    assert set(statuses) <= vocabulary
    assert statuses.count(audit.COVERAGE_UNVERIFIED) == \
        totals["coverage_unverified"]
    assert statuses.count(audit.COVERAGE_DEFECT) == totals["coverage_defect"]
    assert statuses.count(audit.COVERAGE_PROVEN_BY_GUARDED_EVALUATOR) == \
        totals["coverage_proven_guarded"]


# ════════════════════════════════════════════════════════════════════
# A15 — READ-ONLY guard
# ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("statement", [
    "UPDATE dds.srr_research_outcome SET is_final = true",
    "INSERT INTO dds.srr_research_outcome (signal_id) VALUES (1)",
    "DELETE FROM dds.srr_research_outcome",
    "DROP TABLE dds.srr_research_outcome",
    "ALTER TABLE dds.srr_research_outcome ADD COLUMN x int",
    "CREATE TABLE x (id int)",
    "TRUNCATE dds.srr_research_outcome",
    "COPY dds.srr_research_outcome TO '/tmp/x.csv'",
    "WITH x AS (SELECT 1) DELETE FROM dds.srr_research_outcome",
])
def test_a15_read_only_guard_rejects_dml_ddl(statement):
    with pytest.raises(audit.ReadOnlyViolation):
        audit.assert_read_only(statement)


def test_a15_read_only_guard_accepts_select_and_comments():
    assert audit.assert_read_only("SELECT 1")
    assert audit.assert_read_only("WITH x AS (SELECT 1) SELECT * FROM x")
    # DML keywords inside comments are stripped first
    assert audit.assert_read_only("SELECT 1 -- UPDATE coming never")


def test_a15_sql_file_contains_no_dml_ddl():
    text = SQL_FILE.read_text(encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", text)
    for keyword in ("INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE",
                    "DROP", "ALTER", "CREATE", "GRANT", "REVOKE",
                    "VACUUM", "CALL"):
        assert not re.search(rf"\b{keyword}\b", code, re.I), keyword
    # COPY appears only as the psql client meta-command \copy … TO '<file>'
    for match in re.finditer(r"\bCOPY\b", code, re.I):
        assert match.start() > 0 and code[match.start() - 1] == "\\", (
            "server-side COPY is forbidden; only client-side \\copy")
    copy_commands = list(re.finditer(
        r"\\copy\b(.*?)WITH\s+CSV\s+HEADER", code, re.S | re.I))
    assert len(copy_commands) == 2
    for command in copy_commands:
        segment = command.group(1)
        # the meta-command itself must be `) TO '<client file>'` — the
        # inner SELECT's FROM is part of the query, not the direction
        assert re.search(r"\)\s*TO\s*'", segment), (
            "\\copy must EXPORT to a client file")
        assert not re.search(r"\)\s*FROM\s*'", segment), (
            "\\copy must never import into PostgreSQL")


# ════════════════════════════════════════════════════════════════════
# P2-01 — horizon-suffixed flags: one recognition mechanism everywhere
# ════════════════════════════════════════════════════════════════════


def test_p2_01_horizon_suffixed_flag_counts_and_details_match():
    """Regression for https://github.com/eignat88/trad_bot/pull/265
    #discussion_r4484487856: a row carrying ONLY horizon-suffixed flags
    (``LATE_EVAL_TRUNCATED_15m``) must be counted, detailed and rendered."""
    signal_time = SIGNAL
    late = signal_time + timedelta(minutes=6000)
    row = make_row(777, signal_time=signal_time,
                   marks={h: late for h in audit.HORIZONS},
                   updated_at=late)

    result = audit.classify([row],
                            generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    flags = result.verdicts[0].flags
    assert "LATE_EVAL_TRUNCATED_15m" in flags
    # base constants never literally appear — the matcher must bridge that
    assert audit.FLAG_LATE_EVAL_TRUNCATED not in flags

    # counter
    assert result.totals["potentially_incomplete"] == 1
    assert result.totals["late_eval_truncated"] == 1
    # detail (the same mechanism as the counter)
    detail = result.potentially_incomplete
    assert [v.signal_id for v in detail] == [777]
    # markdown §3 renders the detail row and never "None detected"
    md = audit.render_markdown(result)
    section3 = md.split("## 3.")[1].split("## 4.")[0]
    assert "None detected" not in section3
    assert "| 777 |" in section3
    assert "Count: **1**" in section3
    # JSON rows expose the same flag
    payload = audit.result_to_json(result)
    assert payload["rows"][0]["flags"] == flags


def test_p2_01_flag_matcher_requires_a_valid_horizon():
    assert audit.match_flag("LATE_EVAL_TRUNCATED_15m",
                            audit.FLAG_LATE_EVAL_TRUNCATED)
    assert audit.match_flag(audit.FLAG_LATE_EVAL_TRUNCATED,
                            audit.FLAG_LATE_EVAL_TRUNCATED)
    # an arbitrary suffix is NOT a match (no blind startswith)
    assert not audit.match_flag("LATE_EVAL_TRUNCATED_TOMORROW",
                                audit.FLAG_LATE_EVAL_TRUNCATED)
    assert not audit.match_flag("NULL_MARK_VALUE_15m",
                                audit.FLAG_LATE_EVAL_TRUNCATED)
    assert audit.flag_horizon("NULL_MARK_VALUE_240m") == "240m"
    assert audit.flag_horizon(audit.FLAG_FINALIZED_MISSING_MARK) is None


# ════════════════════════════════════════════════════════════════════
# P2-02 — legacy thresholds scoped to pre-rollout horizons
# ════════════════════════════════════════════════════════════════════


def test_p2_02a_pre_rollout_late_horizon_gets_legacy_truncation():
    signal_time = SIGNAL
    mark = signal_time + timedelta(minutes=6000)   # way past rollout? no:
    # rollout is AFTER this mark:
    assert mark < ROLLOUT
    row = make_row(21, signal_time=signal_time,
                   marks={h: mark for h in audit.HORIZONS},
                   updated_at=mark)

    result = audit.classify([row], guarded_from=ROLLOUT,
                            generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    verdict = result.verdicts[0]

    assert verdict.late_horizons == list(audit.HORIZONS)
    assert "LATE_EVAL_TRUNCATED_15m" in verdict.flags
    assert result.totals["late_eval_truncated"] == 1
    assert result.totals["potentially_incomplete"] == 1


def test_p2_02b_post_rollout_horizon_has_no_legacy_flag():
    """Same >6000-minute signal age, but the horizon was evaluated AFTER
    the rollout: the coverage guard validated it, so signal age alone must
    not produce a legacy truncation flag."""
    signal_time = SIGNAL
    rollout = signal_time + timedelta(minutes=5900)
    mark = signal_time + timedelta(minutes=6000)   # >= rollout, age 6000
    assert mark >= rollout
    row = make_row(22, signal_time=signal_time,
                   marks={h: mark for h in audit.HORIZONS},
                   updated_at=mark)

    result = audit.classify([row], guarded_from=rollout,
                            generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    verdict = result.verdicts[0]

    assert verdict.late_horizons == []
    assert not any(audit.match_flag(f, audit.FLAG_LATE_EVAL_TRUNCATED)
                   or audit.match_flag(f, audit.FLAG_LATE_EVAL_RISK)
                   for f in verdict.flags)
    assert result.totals["late_eval_truncated"] == 0
    assert result.totals["late_eval_risk"] == 0
    assert result.totals["potentially_incomplete"] == 0
    assert verdict.reconstruction_required is False
    # post-rollout finalization is not attributed to the legacy evaluator
    assert not verdict.has_flag(audit.FLAG_FINALIZED_BY_LEGACY)
    # …and still no coverage proof from the rollout timestamp alone
    assert verdict.coverage_status == audit.COVERAGE_UNVERIFIED


def test_p2_02c_post_rollout_null_mfe_is_still_a_structural_defect():
    """Structural checks (missing/NULL marks) apply to ALL periods."""
    signal_time = SIGNAL
    rollout = signal_time + timedelta(minutes=5900)
    mark = signal_time + timedelta(minutes=6000)
    row = make_row(23, signal_time=signal_time,
                   marks={h: mark for h in audit.HORIZONS},
                   mfe={**{h: 0.5 for h in audit.HORIZONS}, "15m": None},
                   updated_at=mark)

    result = audit.classify([row], guarded_from=rollout,
                            generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    verdict = result.verdicts[0]

    assert verdict.has_flag("NULL_MARK_VALUE_15m")
    assert verdict.coverage_status == audit.COVERAGE_DEFECT
    assert result.totals["null_mark_values"] == 1
    assert result.totals["coverage_defect"] == 1


def test_p2_02d_horizons_straddling_rollout_are_independent():
    """15m/30m evaluated pre-rollout (late) while 240m is evaluated
    post-rollout: only the pre-rollout horizons get legacy flags."""
    signal_time = SIGNAL
    pre_mark = signal_time + timedelta(minutes=6000)
    post_mark = signal_time + timedelta(minutes=6100)
    rollout = signal_time + timedelta(minutes=6050)
    marks = {
        "15m": pre_mark, "30m": pre_mark,
        "60m": post_mark, "120m": post_mark, "240m": post_mark,
    }
    row = make_row(24, signal_time=signal_time, marks=marks,
                   updated_at=post_mark)

    result = audit.classify([row], guarded_from=rollout,
                            generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    verdict = result.verdicts[0]

    assert verdict.late_horizons == ["15m", "30m"]
    assert "LATE_EVAL_TRUNCATED_15m" in verdict.flags
    assert "LATE_EVAL_TRUNCATED_30m" in verdict.flags
    for horizon in ("60m", "120m", "240m"):
        assert not any(f.endswith(f"_{horizon}") and
                       audit.match_flag(f, audit.FLAG_LATE_EVAL_TRUNCATED)
                       for f in verdict.flags), horizon
    assert result.totals["late_eval_truncated"] == 1   # per ROW, not flag
    assert result.totals["potentially_incomplete"] == 1


def test_p2_02e_without_guarded_from_legacy_behavior_is_preserved():
    """No rollout timestamp: every horizon is treated as legacy (the
    historical behavior), yet coverage is never claimed as proven."""
    signal_time = SIGNAL
    mark = signal_time + timedelta(minutes=6000)
    row = make_row(25, signal_time=signal_time,
                   marks={h: mark for h in audit.HORIZONS},
                   updated_at=mark)

    result = audit.classify([row],
                            generated_at=datetime(2026, 11, 1, tzinfo=UTC))
    verdict = result.verdicts[0]

    assert verdict.late_horizons == list(audit.HORIZONS)
    assert result.totals["late_eval_truncated"] == 1
    assert verdict.coverage_status == audit.COVERAGE_UNVERIFIED
    assert result.totals["coverage_proven_guarded"] == 0
    assert result.params["legacy_threshold_scope"] == (
        "all_horizons_no_rollout_timestamp")
