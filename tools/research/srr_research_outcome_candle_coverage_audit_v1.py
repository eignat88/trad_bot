"""READ-ONLY SRR research outcome candle-coverage audit (ISSUE244).

Deterministic OFFLINE analysis over a saved READ-ONLY production export of
``dds.srr_research_outcome`` (+ optionally ``dds.srr_research_signal``).  It
does not connect to a database, fetch candles, rewrite outcomes, backfill,
recalculate MFE/MAE or TP/SL, or make trading decisions.

Purpose (parent #244 / ``OOS_GENERIC_CANDLE_COVERAGE_FAIL_CLOSED_V1``): the
legacy SRR evaluator finalized outcomes WITHOUT ever proving candle-path
coverage.  This audit classifies every historical outcome into:

* outcomes finalized by the legacy evaluator;
* potentially incomplete candle windows (the legacy ``min(..., 1000)``
  latest-N fetch could not have covered the evaluation window);
* outcomes requiring a SEPARATE reconstruction decision (never automatic);
* unverified outcomes with the explicit ``COVERAGE_UNVERIFIED`` label.

No row is ever modified by this tool: the classification lives in the report
and the optional JSON export only.  The optional
``COVERAGE_PROVEN_BY_GUARDED_EVALUATOR`` label can only appear for rows whose
marks are at/after ``--guarded-from`` (the guarded evaluator rollout), which
requires a separate operator GO — it is 0 until then.

Export (read-only, from the repository root, see
``sql/srr_research_outcome_candle_coverage_audit_v1.sql``)::

    psql "$CONN" -c "\\copy (SELECT ... ) TO 'srr_coverage_outcomes.csv'
          WITH CSV HEADER"
    psql "$CONN" -c "\\copy (SELECT ... ) TO 'srr_coverage_signals.csv'
          WITH CSV HEADER"

Analyze::

    python -m tools.research.srr_research_outcome_candle_coverage_audit_v1 \
        --outcomes srr_coverage_outcomes.csv \
        [--signals srr_coverage_signals.csv] \
        [--report reports/SRR_RESEARCH_OUTCOME_CANDLE_COVERAGE_AUDIT_V1.md] \
        [--json exports/srr_research_outcome_candle_coverage_audit_v1.json]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

EXPERIMENT_ID = "SRR_LONG_OUTCOME_V1"

HORIZON_MINUTES: dict[str, int] = {
    "15m": 15, "30m": 30, "60m": 60, "120m": 120, "240m": 240,
}
HORIZONS = tuple(HORIZON_MINUTES)

# Legacy fetch: min((end_ms - start_ms) // 300_000 + 10, 1000) 5m bars.
LEGACY_FETCH_CAP = 1000
CANDLE_INTERVAL_MIN = 5

# age_min >= 4950  -> the legacy request formula exceeds the 1000-bar cap
#                     (the +10 margin may still have covered the window).
# age_min >= 5000  -> the 1000 newest bars cannot contain the first required
#                     candle (signal+5m): the window head was definitely cut.
LATE_EVAL_RISK_MIN = 4950        # 82h30m
LATE_EVAL_TRUNCATED_MIN = 5000   # 83h20m

# ── coverage labels (stable report vocabulary; NO DB column) ───────
COVERAGE_UNVERIFIED = "COVERAGE_UNVERIFIED"
COVERAGE_DEFECT = "COVERAGE_DEFECT"
COVERAGE_PROVEN_BY_GUARDED_EVALUATOR = "COVERAGE_PROVEN_BY_GUARDED_EVALUATOR"

FLAG_FINALIZED_BY_LEGACY = "FINALIZED_BY_LEGACY_EVALUATOR"
FLAG_FINALIZED_MISSING_MARK = "FINALIZED_MISSING_MARK"
FLAG_NULL_MARK_VALUE = "NULL_MARK_VALUE"
FLAG_EARLY_MARK = "EARLY_MARK"
FLAG_LATE_EVAL_RISK = "LATE_EVAL_RISK_ZONE"
FLAG_LATE_EVAL_TRUNCATED = "LATE_EVAL_TRUNCATED"

DEFECT_FLAGS = frozenset({
    FLAG_FINALIZED_MISSING_MARK,
    FLAG_NULL_MARK_VALUE,
    FLAG_EARLY_MARK,
    FLAG_LATE_EVAL_RISK,
    FLAG_LATE_EVAL_TRUNCATED,
})

OUTCOME_COLUMNS = (
    "signal_id", "experiment_id", "symbol", "signal_time", "is_final",
    "evaluated_15m_at", "evaluated_30m_at", "evaluated_60m_at",
    "evaluated_120m_at", "evaluated_240m_at",
    "mfe_15m", "mfe_30m", "mfe_60m", "mfe_120m", "mfe_240m",
    "created_at", "updated_at",
)
SIGNAL_COLUMNS = ("signal_id", "experiment_id", "symbol", "signal_time")

# ── read-only SQL (mirrored in sql/srr_research_outcome_candle_coverage_audit_v1.sql)

SQL_TOTALS = """
SELECT
    (SELECT COUNT(*)
       FROM dds.srr_research_signal
      WHERE experiment_id = 'SRR_LONG_OUTCOME_V1')        AS signals,
    COUNT(*)                                              AS outcomes,
    COUNT(*) FILTER (WHERE o.is_final)                    AS finalized,
    COUNT(*) FILTER (WHERE NOT o.is_final)                AS non_final,
    COUNT(DISTINCT o.symbol)                              AS symbols
FROM dds.srr_research_outcome o
WHERE o.experiment_id = 'SRR_LONG_OUTCOME_V1'
"""

SQL_FINALIZED_MISSING_MARKS = """
SELECT COUNT(*) AS finalized_missing_marks
FROM dds.srr_research_outcome o
WHERE o.experiment_id = 'SRR_LONG_OUTCOME_V1'
  AND o.is_final
  AND (o.evaluated_15m_at IS NULL
       OR o.evaluated_30m_at IS NULL
       OR o.evaluated_60m_at IS NULL
       OR o.evaluated_120m_at IS NULL
       OR o.evaluated_240m_at IS NULL)
"""

SQL_EARLY_MARKS = """
SELECT COUNT(*) AS early_marks
FROM dds.srr_research_outcome o
JOIN dds.srr_research_signal s ON s.signal_id = o.signal_id
WHERE o.experiment_id = 'SRR_LONG_OUTCOME_V1'
  AND ((o.evaluated_15m_at  IS NOT NULL AND o.evaluated_15m_at  < s.signal_time + interval '15 minutes')
    OR (o.evaluated_30m_at  IS NOT NULL AND o.evaluated_30m_at  < s.signal_time + interval '30 minutes')
    OR (o.evaluated_60m_at  IS NOT NULL AND o.evaluated_60m_at  < s.signal_time + interval '60 minutes')
    OR (o.evaluated_120m_at IS NOT NULL AND o.evaluated_120m_at < s.signal_time + interval '120 minutes')
    OR (o.evaluated_240m_at IS NOT NULL AND o.evaluated_240m_at < s.signal_time + interval '240 minutes'))
"""

SQL_NULL_MARK_VALUES = """
SELECT COUNT(*) AS null_mark_values
FROM dds.srr_research_outcome o
WHERE o.experiment_id = 'SRR_LONG_OUTCOME_V1'
  AND ((o.evaluated_15m_at  IS NOT NULL AND o.mfe_15m  IS NULL)
    OR (o.evaluated_30m_at  IS NOT NULL AND o.mfe_30m  IS NULL)
    OR (o.evaluated_60m_at  IS NOT NULL AND o.mfe_60m  IS NULL)
    OR (o.evaluated_120m_at IS NOT NULL AND o.mfe_120m IS NULL)
    OR (o.evaluated_240m_at IS NOT NULL AND o.mfe_240m IS NULL))
"""

SQL_OUTCOMES_EXPORT = """
SELECT
    o.signal_id,
    o.experiment_id,
    o.symbol,
    s.signal_time,
    o.is_final,
    o.evaluated_15m_at,
    o.evaluated_30m_at,
    o.evaluated_60m_at,
    o.evaluated_120m_at,
    o.evaluated_240m_at,
    o.mfe_15m,
    o.mfe_30m,
    o.mfe_60m,
    o.mfe_120m,
    o.mfe_240m,
    o.created_at,
    o.updated_at
FROM dds.srr_research_outcome o
JOIN dds.srr_research_signal s ON s.signal_id = o.signal_id
WHERE o.experiment_id = 'SRR_LONG_OUTCOME_V1'
ORDER BY s.signal_time, o.signal_id
"""

SQL_SIGNALS_EXPORT = """
SELECT signal_id, experiment_id, symbol, signal_time
FROM dds.srr_research_signal
WHERE experiment_id = 'SRR_LONG_OUTCOME_V1'
ORDER BY signal_time, signal_id
"""

SQL_STATEMENTS = {
    "SQL_TOTALS": SQL_TOTALS,
    "SQL_FINALIZED_MISSING_MARKS": SQL_FINALIZED_MISSING_MARKS,
    "SQL_EARLY_MARKS": SQL_EARLY_MARKS,
    "SQL_NULL_MARK_VALUES": SQL_NULL_MARK_VALUES,
    "SQL_OUTCOMES_EXPORT": SQL_OUTCOMES_EXPORT,
    "SQL_SIGNALS_EXPORT": SQL_SIGNALS_EXPORT,
}

_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|"
    r"COPY|CALL|DO|SET|VACUUM|REINDEX|CLUSTER|COMMENT|LOCK)\b",
    re.IGNORECASE,
)


class ReadOnlyViolation(RuntimeError):
    """A non-read-only statement was rejected before execution."""


def assert_read_only(sql: str) -> str:
    """Fail-closed guard: only plain SELECT/WITH statements are allowed.

    Comments are stripped first; the statement must start with SELECT/WITH and
    must not contain any DDL/DML/utility keyword as a whole word.  Every query
    this audit runs passes through this function — the audit can never write.
    """
    text = re.sub(r"--[^\n]*", " ", sql)
    text = " ".join(text.split())
    if not text.upper().startswith(("SELECT", "WITH")):
        raise ReadOnlyViolation(
            f"read-only audit refuses non-SELECT statement: {text[:120]}")
    match = _FORBIDDEN_SQL.search(text)
    if match:
        raise ReadOnlyViolation(
            f"read-only audit refuses statement containing {match.group(1)!r}: "
            f"{text[:120]}")
    return text


# ── parsing ────────────────────────────────────────────────────────


def _ts(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text or text.upper() in {"NULL", "NONE", "\\N"}:
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _num(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text.upper() in {"NULL", "NONE", "\\N"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"t", "true", "1", "yes"}


@dataclass(frozen=True)
class OutcomeRow:
    signal_id: int
    experiment_id: str
    symbol: str
    signal_time: datetime
    is_final: bool
    marks: dict[str, datetime | None]
    mfe: dict[str, float | None]
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class SignalRow:
    signal_id: int
    experiment_id: str
    symbol: str
    signal_time: datetime


def load_outcomes_csv(path: Path) -> list[OutcomeRow]:
    rows: list[OutcomeRow] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in ("signal_id", "signal_time")
                   if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(
                f"{path}: missing required columns {missing}; export the file "
                "with sql/srr_research_outcome_candle_coverage_audit_v1.sql")
        for raw in reader:
            signal_time = _ts(raw.get("signal_time"))
            if signal_time is None:
                raise ValueError(f"{path}: row without signal_time: {raw}")
            rows.append(OutcomeRow(
                signal_id=int(raw["signal_id"]),
                experiment_id=str(raw.get("experiment_id") or EXPERIMENT_ID),
                symbol=str(raw.get("symbol") or ""),
                signal_time=signal_time,
                is_final=_bool(raw.get("is_final")),
                marks={
                    h: _ts(raw.get(f"evaluated_{h}_at")) for h in HORIZONS
                },
                mfe={h: _num(raw.get(f"mfe_{h}")) for h in HORIZONS},
                created_at=_ts(raw.get("created_at")),
                updated_at=_ts(raw.get("updated_at")),
            ))
    return rows


def load_signals_csv(path: Path) -> list[SignalRow]:
    rows: list[SignalRow] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            signal_time = _ts(raw.get("signal_time"))
            if signal_time is None:
                raise ValueError(f"{path}: row without signal_time: {raw}")
            rows.append(SignalRow(
                signal_id=int(raw["signal_id"]),
                experiment_id=str(raw.get("experiment_id") or EXPERIMENT_ID),
                symbol=str(raw.get("symbol") or ""),
                signal_time=signal_time,
            ))
    return rows


# ── classification (pure, unit-tested) ─────────────────────────────


@dataclass
class RowVerdict:
    row: OutcomeRow
    coverage_status: str = COVERAGE_UNVERIFIED
    flags: list[str] = field(default_factory=list)
    late_horizons: list[str] = field(default_factory=list)
    max_age_minutes: float | None = None
    reconstruction_required: bool = False

    @property
    def signal_id(self) -> int:
        return self.row.signal_id

    @property
    def symbol(self) -> str:
        return self.row.symbol

    def has_flag(self, flag: str) -> bool:
        return flag in self.flags


@dataclass
class AuditResult:
    generated_at: str
    experiment_id: str
    params: dict[str, Any]
    totals: dict[str, Any]
    verdicts: list[RowVerdict]
    inputs: dict[str, str] = field(default_factory=dict)

    def _count_where(self, predicate) -> int:
        return sum(1 for v in self.verdicts if predicate(v))

    @property
    def finalized_by_legacy(self) -> list[RowVerdict]:
        return [v for v in self.verdicts
                if v.has_flag(FLAG_FINALIZED_BY_LEGACY)]

    @property
    def potentially_incomplete(self) -> list[RowVerdict]:
        return [v for v in self.verdicts
                if any(f in {FLAG_LATE_EVAL_RISK, FLAG_LATE_EVAL_TRUNCATED,
                             FLAG_NULL_MARK_VALUE} for f in v.flags)]

    @property
    def reconstruction_required(self) -> list[RowVerdict]:
        return [v for v in self.verdicts if v.reconstruction_required]

    @property
    def coverage_unverified(self) -> list[RowVerdict]:
        return [v for v in self.verdicts
                if v.coverage_status == COVERAGE_UNVERIFIED]

    @property
    def coverage_proven(self) -> list[RowVerdict]:
        return [v for v in self.verdicts
                if v.coverage_status == COVERAGE_PROVEN_BY_GUARDED_EVALUATOR]


def classify(
    rows: Iterable[OutcomeRow],
    *,
    signals: Iterable[SignalRow] | None = None,
    guarded_from: datetime | None = None,
    risk_min: float = LATE_EVAL_RISK_MIN,
    truncated_min: float = LATE_EVAL_TRUNCATED_MIN,
    generated_at: datetime | None = None,
    inputs: dict[str, str] | None = None,
) -> AuditResult:
    """Classify historical outcomes.  Pure: no I/O, no DB, no writes."""
    rows = list(rows)
    verdicts: list[RowVerdict] = []

    for row in rows:
        verdict = RowVerdict(row=row)
        ages: list[float] = []

        # ── structural checks (provable from the export alone) ──
        if row.is_final and any(row.marks[h] is None for h in HORIZONS):
            verdict.flags.append(FLAG_FINALIZED_MISSING_MARK)
        for horizon in HORIZONS:
            mark = row.marks[horizon]
            if mark is None:
                continue
            cutoff = row.signal_time + timedelta(
                minutes=HORIZON_MINUTES[horizon])
            if mark < cutoff:
                verdict.flags.append(f"{FLAG_EARLY_MARK}_{horizon}")
            if row.mfe[horizon] is None:
                # A horizon mark whose MFE stayed NULL means the frozen
                # formula found NO candle in the window: proof of an
                # incomplete path at evaluation time.
                verdict.flags.append(f"{FLAG_NULL_MARK_VALUE}_{horizon}")
            age_min = (mark - row.signal_time).total_seconds() / 60.0
            ages.append(age_min)
            if age_min >= truncated_min:
                verdict.flags.append(f"{FLAG_LATE_EVAL_TRUNCATED}_{horizon}")
            elif age_min >= risk_min:
                verdict.flags.append(f"{FLAG_LATE_EVAL_RISK}_{horizon}")
        verdict.max_age_minutes = max(ages) if ages else None
        verdict.late_horizons = sorted({
            f.rsplit("_", 1)[-1]
            for f in verdict.flags
            if f.startswith((FLAG_LATE_EVAL_RISK, FLAG_LATE_EVAL_TRUNCATED))
        })

        # ── rollout attribution ──
        marks = [row.marks[h] for h in HORIZONS if row.marks[h] is not None]
        guarded = (
            bool(marks)
            and guarded_from is not None
            and all(mark >= guarded_from for mark in marks)
        )
        if row.is_final:
            if guarded and guarded_from is not None and (
                row.updated_at is None or row.updated_at >= guarded_from
            ):
                pass  # finalized by the guarded evaluator — proven below
            else:
                verdict.flags.append(FLAG_FINALIZED_BY_LEGACY)

        has_defect = any(f in DEFECT_FLAGS or f.rsplit("_", 1)[0] in {
            FLAG_EARLY_MARK, FLAG_NULL_MARK_VALUE,
            FLAG_LATE_EVAL_RISK, FLAG_LATE_EVAL_TRUNCATED,
        } for f in verdict.flags)
        verdict.reconstruction_required = has_defect

        if has_defect:
            verdict.coverage_status = COVERAGE_DEFECT
        elif not marks:
            verdict.coverage_status = COVERAGE_UNVERIFIED
        elif guarded:
            verdict.coverage_status = COVERAGE_PROVEN_BY_GUARDED_EVALUATOR
        else:
            verdict.coverage_status = COVERAGE_UNVERIFIED
        verdicts.append(verdict)

    # ── totals ──
    signal_list = list(signals or [])
    finalized = sum(1 for v in verdicts if v.row.is_final)
    totals: dict[str, Any] = {
        "signals": len(signal_list) if signal_list else None,
        "outcomes": len(verdicts),
        "finalized": finalized,
        "non_final": len(verdicts) - finalized,
        "symbols": len({v.row.symbol for v in verdicts}),
        "finalized_missing_marks": sum(
            1 for v in verdicts
            if v.has_flag(FLAG_FINALIZED_MISSING_MARK)),
        "early_marks": sum(
            1 for v in verdicts
            if any(f.startswith(f"{FLAG_EARLY_MARK}_") for f in v.flags)),
        "null_mark_values": sum(
            1 for v in verdicts
            if any(f.startswith(f"{FLAG_NULL_MARK_VALUE}_")
                   for f in v.flags)),
        "late_eval_risk": sum(
            1 for v in verdicts
            if any(f.startswith(f"{FLAG_LATE_EVAL_RISK}_") for f in v.flags)),
        "late_eval_truncated": sum(
            1 for v in verdicts
            if any(f.startswith(f"{FLAG_LATE_EVAL_TRUNCATED}_")
                   for f in v.flags)),
        "finalized_by_legacy": sum(
            1 for v in verdicts if v.has_flag(FLAG_FINALIZED_BY_LEGACY)),
        "potentially_incomplete": sum(
            1 for v in verdicts
            if any(f in {FLAG_LATE_EVAL_RISK, FLAG_LATE_EVAL_TRUNCATED,
                         FLAG_NULL_MARK_VALUE} or
                   f.startswith((f"{FLAG_LATE_EVAL_RISK}_",
                                 f"{FLAG_LATE_EVAL_TRUNCATED}_",
                                 f"{FLAG_NULL_MARK_VALUE}_"))
                   for f in v.flags)),
        "reconstruction_required": sum(
            1 for v in verdicts if v.reconstruction_required),
        "coverage_unverified": sum(
            1 for v in verdicts
            if v.coverage_status == COVERAGE_UNVERIFIED),
        "coverage_defect": sum(
            1 for v in verdicts
            if v.coverage_status == COVERAGE_DEFECT),
        "coverage_proven_guarded": sum(
            1 for v in verdicts
            if v.coverage_status == COVERAGE_PROVEN_BY_GUARDED_EVALUATOR),
    }
    if signal_list:
        outcome_ids = {v.row.signal_id for v in verdicts}
        totals["signals_without_outcome"] = sum(
            1 for s in signal_list if s.signal_id not in outcome_ids)

    now = generated_at or datetime.now(timezone.utc)
    return AuditResult(
        generated_at=now.isoformat(timespec="seconds"),
        experiment_id=EXPERIMENT_ID,
        params={
            "legacy_fetch_cap": LEGACY_FETCH_CAP,
            "candle_interval_min": CANDLE_INTERVAL_MIN,
            "late_eval_risk_min": risk_min,
            "late_eval_truncated_min": truncated_min,
            "guarded_from": guarded_from.isoformat() if guarded_from else None,
        },
        totals=totals,
        verdicts=verdicts,
        inputs=dict(inputs or {}),
    )


# ── report rendering ───────────────────────────────────────────────


def _iso(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z") if value else ""


def _fmt_age(minutes: float | None) -> str:
    if minutes is None:
        return ""
    return f"{minutes:.0f}m ({minutes / 60:.1f}h)"


def _detail_table(verdicts: list[RowVerdict], *, max_rows: int) -> list[str]:
    lines = [
        "| signal_id | symbol | signal_time (UTC) | is_final | max age at "
        "eval | flags | coverage_status |",
        "|---|---|---|---|---|---|---|",
    ]
    for verdict in verdicts[:max_rows]:
        row = verdict.row
        lines.append(
            f"| {row.signal_id} | {row.symbol} | {_iso(row.signal_time)} | "
            f"{'TRUE' if row.is_final else 'FALSE'} | "
            f"{_fmt_age(verdict.max_age_minutes)} | "
            f"{', '.join(verdict.flags) or '—'} | "
            f"{verdict.coverage_status} |")
    if len(verdicts) > max_rows:
        lines.append(f"| … | | | | | **{len(verdicts) - max_rows} more** | |")
    return lines


def render_markdown(result: AuditResult, *, max_rows: int = 40) -> str:
    t = result.totals
    p = result.params
    lines: list[str] = []
    add = lines.append

    add("# SRR Research Outcome — Candle Coverage Audit (READ-ONLY)")
    add("")
    add(f"- **Experiment:** `{result.experiment_id}`")
    add(f"- **Generated:** {result.generated_at}")
    add("- **Mode:** READ-ONLY offline analysis of a saved export "
        "(no DB connection, no DML, no backfill, no recalculation, "
        "no change to historical outcomes, OOS freeze untouched)")
    for name, digest in sorted(result.inputs.items()):
        add(f"- **Input:** `{name}` sha256=`{digest}`")
    add(f"- **Legacy fetch cap:** {p['legacy_fetch_cap']} candles × "
        f"{p['candle_interval_min']}m")
    add(f"- **Late-evaluation thresholds:** risk ≥ {p['late_eval_risk_min']}m, "
        f"truncated ≥ {p['late_eval_truncated_min']}m")
    add(f"- **guarded_from:** {p['guarded_from'] or 'not set (guarded "
        "evaluator not rolled out)'}")
    add("")

    add("## 1. Snapshot totals")
    add("")
    add("| metric | value |")
    add("|---|---|")
    add(f"| signals | {t.get('signals') if t.get('signals') is not None else 'n/a (no --signals export)'} |")
    if t.get("signals_without_outcome") is not None:
        add(f"| signals without outcome | {t['signals_without_outcome']} |")
    add(f"| outcomes | {t['outcomes']} |")
    add(f"| finalized | {t['finalized']} |")
    add(f"| non-final | {t['non_final']} |")
    add(f"| unique symbols | {t['symbols']} |")
    add(f"| finalized with missing horizon marks | {t['finalized_missing_marks']} |")
    add(f"| early horizon marks | {t['early_marks']} |")
    add(f"| horizon marks with NULL MFE (no candle in window) | {t['null_mark_values']} |")
    add(f"| late-evaluated (risk zone) | {t['late_eval_risk']} |")
    add(f"| late-evaluated (truncated) | {t['late_eval_truncated']} |")
    add("")

    add("## 2. Finalized by the legacy evaluator")
    add("")
    add(f"Count: **{t['finalized_by_legacy']}** of {t['finalized']} finalized "
        "rows (the guarded evaluator writes nothing until the separate "
        "operator GO; every pre-GO finalization is legacy).")
    add("")
    legacy = [v for v in result.finalized_by_legacy]
    if legacy:
        lines.extend(_detail_table(legacy, max_rows=max_rows))
    add("")

    add("## 3. Potentially incomplete candle windows")
    add("")
    add(f"Count: **{t['potentially_incomplete']}**.  These rows were "
        "evaluated when the legacy `min((end−start)/5m + 10, 1000)` latest-N "
        "fetch had hit the 1000-bar cap (evaluated ≥ "
        f"{p['late_eval_risk_min']} min after the signal), so the oldest part "
        "of the window may have been cut; `NULL_MARK_VALUE` rows are proven "
        "incomplete paths (the frozen formula found no candle at all).")
    add("")
    incomplete = result.potentially_incomplete
    if incomplete:
        lines.extend(_detail_table(incomplete, max_rows=max_rows))
    else:
        add("_None detected from the export._")
    add("")

    add("## 4. Outcomes requiring a SEPARATE reconstruction")
    add("")
    add(f"Count: **{t['reconstruction_required']}**.  Every defect-flagged "
        "row (finalized missing marks, early marks, NULL mark values, "
        "late-evaluated windows).  These rows must NOT be repaired, "
        "backfilled or recalculated automatically: reconstruction is a "
        "separate, explicitly approved change with its own tooling and "
        "review.  Rows here are excluded from clean OOS by decision, not by "
        "silent modification.")
    add("")
    required = result.reconstruction_required
    if required:
        lines.extend(_detail_table(required, max_rows=max_rows))
    else:
        add("_None detected from the export._")
    add("")

    add("## 5. COVERAGE_UNVERIFIED outcomes")
    add("")
    add(f"Count: **{t['coverage_unverified']}** "
        f"(+ {t['coverage_defect']} rows labelled `{COVERAGE_DEFECT}` — also "
        "unverified, with an additional proven/likely defect).")
    add("")
    add("The legacy evaluator stored no coverage evidence, and the DB holds "
        "no candle history: any row whose marks were written before the "
        "guarded evaluator rollout is explicitly `COVERAGE_UNVERIFIED` — the "
        "audit can neither confirm nor refute its candle path. "
        "`COVERAGE_UNVERIFIED` outcomes are **not automatically included in "
        "clean OOS**; inclusion is a separate decision recorded outside this "
        "report. Rows labelled "
        f"`{COVERAGE_PROVEN_BY_GUARDED_EVALUATOR}` "
        f"**{t['coverage_proven_guarded']}** (only possible after "
        "`--guarded-from` is set to a real rollout GO timestamp).")
    add("")
    unverified_ids = [v.signal_id for v in result.coverage_unverified]
    if unverified_ids:
        preview = ", ".join(str(i) for i in unverified_ids[:max_rows])
        more = (f" … {len(unverified_ids) - max_rows} more"
                if len(unverified_ids) > max_rows else "")
        add(f"signal_ids: {preview}{more}")
        add("")

    add("## 6. Read-only SQL used")
    add("")
    for name, sql in SQL_STATEMENTS.items():
        add(f"### {name}")
        add("")
        add("```sql")
        add(assert_read_only(sql))
        add("```")
        add("")

    add("## 7. Guarantees and limitations")
    add("")
    add("- **No writes:** every statement above passes "
        "`assert_read_only()`; the tool has no DB connection at all — it "
        "only reads CSV exports.")
    add("- **No history changes:** no backfill, no recalculation of MFE/MAE "
        "or TP/SL, no `is_final` changes, no freeze/OOS registry changes.")
    add("- **No schema change:** the labels (`COVERAGE_UNVERIFIED`, "
        "`COVERAGE_DEFECT`, flags) exist only in this report and the JSON "
        "export; persisting them needs a separately agreed schema + its own "
        "migration PR.")
    add("- **Limitation:** the export contains no candle history, so "
        "mid-window provider gaps or symbols whose Bybit history starts "
        "after `signal_time` cannot be proven or refuted — that is exactly "
        "why every legacy row stays `COVERAGE_UNVERIFIED`.")
    add("- **Limitation:** `age at evaluation` is derived from "
        "`evaluated_*_at − signal_time`; clock steps on the VPS could skew "
        "it, so late-evaluated rows are labelled POTENTIALLY incomplete, "
        "never proven.")
    add("")
    return "\n".join(lines)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def result_to_json(result: AuditResult) -> dict[str, Any]:
    return {
        "generated_at": result.generated_at,
        "experiment_id": result.experiment_id,
        "mode": "READ_ONLY_OFFLINE",
        "params": result.params,
        "inputs": result.inputs,
        "totals": result.totals,
        "rows": [
            {
                "signal_id": v.row.signal_id,
                "symbol": v.row.symbol,
                "signal_time": _iso(v.row.signal_time),
                "is_final": v.row.is_final,
                "marks": {h: _iso(v.row.marks[h]) for h in HORIZONS},
                "mfe": v.row.mfe,
                "updated_at": _iso(v.row.updated_at),
                "coverage_status": v.coverage_status,
                "flags": v.flags,
                "max_age_minutes": v.max_age_minutes,
                "reconstruction_required": v.reconstruction_required,
            }
            for v in result.verdicts
        ],
    }


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="READ-ONLY SRR outcome candle-coverage audit "
                    "(offline over export CSVs; never touches a database)")
    parser.add_argument("--outcomes", required=True,
                        help="CSV exported with SQL_OUTCOMES_EXPORT")
    parser.add_argument("--signals", default=None,
                        help="optional CSV exported with SQL_SIGNALS_EXPORT")
    parser.add_argument("--report", default=None,
                        help="markdown report output path")
    parser.add_argument("--json", default=None,
                        help="optional full JSON classification output path")
    parser.add_argument("--guarded-from", default=None,
                        help="ISO timestamp of the guarded-evaluator rollout "
                             "GO (marks at/after it can be proven)")
    parser.add_argument("--late-risk-min", type=float,
                        default=LATE_EVAL_RISK_MIN)
    parser.add_argument("--late-truncated-min", type=float,
                        default=LATE_EVAL_TRUNCATED_MIN)
    parser.add_argument("--max-detail", type=int, default=40)
    parser.add_argument("--print-sql", action="store_true",
                        help="print the read-only SQL and exit (no inputs)")
    args = parser.parse_args(argv)

    if args.print_sql:
        for name, sql in SQL_STATEMENTS.items():
            print(f"-- {name}")
            print(assert_read_only(sql))
            print()
        return 0

    outcomes_path = Path(args.outcomes)
    if not outcomes_path.exists():
        parser.error(f"outcomes export not found: {outcomes_path}")
    guarded_from = _ts(args.guarded_from) if args.guarded_from else None

    inputs = {outcomes_path.name: sha256_file(outcomes_path)}
    signals = None
    if args.signals:
        signals_path = Path(args.signals)
        if not signals_path.exists():
            parser.error(f"signals export not found: {signals_path}")
        inputs[signals_path.name] = sha256_file(signals_path)
        signals = load_signals_csv(signals_path)

    rows = load_outcomes_csv(outcomes_path)
    result = classify(
        rows,
        signals=signals,
        guarded_from=guarded_from,
        risk_min=args.late_risk_min,
        truncated_min=args.late_truncated_min,
        inputs=inputs,
    )

    markdown = render_markdown(result, max_rows=args.max_detail)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(markdown, encoding="utf-8")
        print(f"report: {report_path}")
    else:
        print(markdown)

    if args.json:
        json_path = Path(args.json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(result_to_json(result), indent=2, ensure_ascii=False),
            encoding="utf-8")
        print(f"json: {json_path}")

    t = result.totals
    print(
        f"outcomes={t['outcomes']} finalized={t['finalized']} "
        f"legacy_finalized={t['finalized_by_legacy']} "
        f"potentially_incomplete={t['potentially_incomplete']} "
        f"reconstruction_required={t['reconstruction_required']} "
        f"coverage_unverified={t['coverage_unverified']} "
        f"coverage_proven={t['coverage_proven_guarded']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
