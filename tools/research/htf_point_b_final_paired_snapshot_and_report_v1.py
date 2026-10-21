#!/usr/bin/env python
"""HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1 - frozen matched OOS verdict (Issue #169).

READ-ONLY snapshot capture + deterministic paired analysis. Research only.

Modes
-----
``capture`` (alias ``snapshot``)
    Executes ONLY ``BEGIN ... READ ONLY``, ``SET LOCAL`` and ``SELECT`` statements
    against production PostgreSQL inside a single ``REPEATABLE READ READ ONLY``
    transaction and writes the frozen snapshot (``snapshot_rows.csv`` +
    ``snapshot_meta.json``) into the isolated output directory. Refuses a
    non-empty output directory unless ``--allow-nonempty-dir`` is passed
    explicitly and NEVER overwrites existing snapshot files.
``report``
    Fully offline: consumes the frozen snapshot files and writes the canonical
    MD/JSON/CSV artifacts. Deterministic for a given snapshot, seed and
    bootstrap iteration count. Refuses to overwrite existing artifacts unless
    ``--allow-artifact-overwrite`` is passed explicitly.

Output schema (fixed artifact set, written into ``--out``)
----------------------------------------------------------
``snapshot_rows.csv`` / ``snapshot_meta.json``
    frozen capture: one row per prospective observation with identity, geometry,
    outcome flags and exit labels; provenance and required snapshot fields.
``HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1.csv``
    matched pairs: identity, timing, entry/risk geometry, per-cohort gross R,
    paired delta R, exit paths.
``HTF_POINT_B_FINAL_PAIRED_UNMATCHED_V1.csv``
    every unmatched/excluded event identity with exactly one documented reason.
``HTF_POINT_B_FINAL_PAIRED_DAILY_V1.csv`` / ``..._SYMBOL_V1.csv``
    paired effect per UTC date / per symbol.
``HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1.json`` / ``.md``
    machine-readable summary and human-readable report.
``HTF_POINT_B_FINAL_PAIRED_SOURCE_MANIFEST_V1.json`` / ``SHA256SUMS.txt``
    provenance manifest with SHA-256 of every artifact, plus checksums.

Importing this module has no production side effects: no DB driver import at
module level (``app.*`` is imported lazily inside ``capture`` only), no writes
to research observations/outcomes, no frozen outcome recomputation, no
registry/lifecycle changes.

Gross frozen research geometry only. NOT executable/net economics.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median

SCHEMA_VERSION = "HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1.1"
MANIFEST_SCHEMA_VERSION = "HTF_POINT_B_FINAL_PAIRED_SOURCE_MANIFEST_V1"
ISSUE = 169

POINT_B = "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE"
BASELINE = "HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE"
FREEZE = datetime(2026, 10, 6, 14, tzinfo=timezone.utc)
FROZEN_MAX_HOLD = 240
FROZEN_INTRABAR_POLICY = "STOP_FIRST"
FROZEN_TP_R = 2.0
FROZEN_SL_R = -1.0
RR_TOL = 1e-5

DEFAULT_SEED = 169
DEFAULT_REPS = 10000
CONF_LEVEL = 0.95

SNAPSHOT_ROWS_FILE = "snapshot_rows.csv"
SNAPSHOT_META_FILE = "snapshot_meta.json"

# Snapshot row schema (fixed order; nothing else is materialized).
ROW_FIELDS = [
    "observation_id", "experiment_id", "symbol", "direction", "signal_time",
    "setup_event_id", "frozen_intrabar_policy", "frozen_max_hold",
    "source_signal_id", "source_observation_id",
    "reference_price", "invalidation_price", "target_1",
    "variant_entry", "variant_stop", "variant_target",
    "has_outcome", "is_final", "return_at_240m",
    "tp_hit", "sl_hit", "tp_before_sl", "sl_before_tp", "ambiguous_intrabar",
    "time_to_tp", "time_to_sl", "evaluated_240m_at", "outcome_updated_at",
    "observation_created_at",
]

# Vetted base query contract (research.prospective_observation x prospective_outcome),
# extended only with columns verified against information_schema on 2026-10-21.
QUERY_OBSERVATIONS = """
SELECT p.experiment_id, p.observation_id, p.symbol, p.direction, p.signal_time,
       p.features->>'setup_event_id',
       p.features->>'_frozen_intrabar_policy',
       p.features->>'_frozen_max_hold',
       p.source_signal_id, p.source_observation_id,
       p.reference_price::float8, p.invalidation_price::float8, p.target_1::float8,
       p.variant_entry::float8, p.variant_stop::float8, p.variant_target::float8,
       o.observation_id IS NOT NULL, o.is_final, o.return_at_240m::float8,
       o.tp_hit, o.sl_hit, o.tp_before_sl, o.sl_before_tp, o.ambiguous_intrabar,
       o.time_to_tp::float8, o.time_to_sl::float8,
       o.evaluated_240m_at, o.updated_at, p.created_at
FROM research.prospective_observation p
LEFT JOIN research.prospective_outcome o ON o.observation_id = p.observation_id
WHERE p.experiment_id IN (%s, %s)
ORDER BY p.experiment_id, p.signal_time, p.observation_id
"""

QUERY_REGISTRY = """
SELECT experiment_id, version, scanner_name, direction, experiment_type,
       primary_metric, filter_rule, entry_rule, stop_rule, target_rule,
       position_sizing, fee_assumption, horizons, minimum_n, minimum_symbols,
       discovery_source, paired_with, started_at, status, created_at, updated_at
FROM research.prospective_experiment
WHERE experiment_id IN (%s, %s)
ORDER BY experiment_id
"""

QUERY_DIRECTION_STATE = """
SELECT *
FROM research.prospective_experiment_direction_state
WHERE experiment_id IN (%s, %s)
ORDER BY experiment_id
"""

META_QUERIES = {
    "transaction_timestamp": "SELECT transaction_timestamp()::text",
    "db_now": "SELECT now()::text",
    "snapshot_identifier": "SELECT pg_current_snapshot()::text",
    "session": (
        "SELECT current_user, current_database(), "
        "current_setting('transaction_isolation'), "
        "current_setting('transaction_read_only'), "
        "current_setting('server_version')"
    ),
    "role": (
        "SELECT rolname, rolcanlogin, rolcreatedb, rolsuper, rolbypassrls "
        "FROM pg_roles WHERE rolname = current_user"
    ),
}

# Statements the snapshot phase is allowed to execute at all.
_ALLOWED_PREFIXES = (
    "SELECT", "BEGIN", "SET LOCAL", "SHOW", "ROLLBACK",
)


# ------------------------------ helpers ------------------------------

def utc_now_iso() -> str:  # only used for CLI messages, never for artifacts
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_ts(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp is not admissible: {value!r}")
    return dt.astimezone(timezone.utc)


def _parse_float(value) -> float | None:
    if value in (None, ""):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _parse_bool(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"t", "true", "1"}:
        return True
    if s in {"f", "false", "0"}:
        return False
    return None


def _fmt(x) -> str:
    if x is None:
        return ""
    return f"{float(x):.10f}"


def _round(x, nd=10):
    return None if x is None else round(float(x), nd)


# ----------------------- frozen outcome semantics -----------------------

def frozen_gross_r(direction, entry, stop, target, ret,
                   tp_first, sl_first, ambiguous, policy, max_hold):
    """Frozen gross research R under 1R SL / 2R TP / 240m / STOP_FIRST.

    Verified semantics (app/research/prospective_evaluator.py, app/research/evaluator.py):
      * ``return_at_240m`` = (last_close_in_window - entry) / entry * 100
        - raw, direction-agnostic percentage price return at the 240m horizon;
      * TP_FIRST -> +2R, SL_FIRST -> -1R;
      * same-candle TP+SL collision is ``ambiguous_intrabar`` and under the frozen
        ``STOP_FIRST`` policy resolves to the stop: -1R;
      * TIMEOUT -> directional price return normalised by |entry - stop|:
            LONG  R = +ret * entry / (100 * |entry - stop|)
            SHORT R = -ret * entry / (100 * |entry - stop|)

    Returns (gross_r | None, path_label).
    """
    if policy != FROZEN_INTRABAR_POLICY or int(max_hold or 0) != FROZEN_MAX_HOLD:
        return None, "non_frozen_policy"
    if direction not in ("LONG", "SHORT"):
        return None, "invalid_direction"
    if None in (entry, stop, target):
        return None, "missing_geometry"
    entry, stop, target = float(entry), float(stop), float(target)
    if not all(map(math.isfinite, (entry, stop, target))) or entry <= 0:
        return None, "bad_directional_geometry"
    sign = 1 if direction == "LONG" else -1
    if sign * (entry - stop) <= 0 or sign * (target - entry) <= 0:
        return None, "bad_directional_geometry"
    risk = abs(entry - stop)
    if risk <= 0:
        return None, "bad_directional_geometry"
    rr = abs(target - entry) / risk
    if not math.isclose(rr, FROZEN_TP_R, rel_tol=RR_TOL, abs_tol=RR_TOL):
        return None, "target_not_2R"
    if ambiguous is True:
        # Frozen STOP_FIRST resolves a same-candle collision as the stop.
        return FROZEN_SL_R, "AMBIGUOUS_STOP_FIRST"
    if tp_first is True and sl_first is True:
        return None, "inconsistent_first_hit"
    if sl_first is True:
        return FROZEN_SL_R, "SL_FIRST"
    if tp_first is True:
        return FROZEN_TP_R, "TP_FIRST"
    if ret is None:
        return None, "missing_timeout_return"
    r = sign * float(ret) * entry / (100.0 * risk)
    if not math.isfinite(r):
        return None, "missing_timeout_return"
    return r, "TIMEOUT"


# --------------------------- snapshot phase ---------------------------

def _assert_read_only(sql: str) -> None:
    head = " ".join(sql.split())[:40].upper()
    if not head.startswith(_ALLOWED_PREFIXES):
        raise RuntimeError(f"refusing non read-only statement: {head!r}")


def run_capture(worktree: Path, config_root: Path, outdir: Path,
                allow_nonempty: bool = False) -> dict:
    """Single REPEATABLE READ READ ONLY transaction capture into CSV/JSON."""
    if outdir.exists():
        existing = sorted(p.name for p in outdir.iterdir())
        if existing and not allow_nonempty:
            raise RuntimeError(
                f"output directory not empty ({len(existing)} entries): {outdir} "
                "(pass --allow-nonempty-dir for an explicitly approved safe run)")
    else:
        outdir.mkdir(parents=True)
    for name in (SNAPSHOT_ROWS_FILE, SNAPSHOT_META_FILE):
        if (outdir / name).exists():
            raise RuntimeError(f"refusing to overwrite existing artifact: {outdir / name}")

    sys.path.insert(0, str(worktree))
    from app.config.settings import load_settings  # noqa: WPS433 (VPS runtime import)
    from app.db.repository import ScannerRepository  # noqa: WPS433

    cfg = load_settings(path=config_root / "config.yaml", env_file=config_root / ".env")
    # The production configuration lives in /opt/trad_bot; allow explicit override.
    repo = ScannerRepository(cfg.db_host, cfg.db_port, cfg.db_name, cfg.db_user,
                             cfg.db_password, backend="postgres")
    conn = repo._conn
    cur = conn.cursor()
    meta: dict = {"schema_version": SCHEMA_VERSION, "query_identity": {}}
    try:
        sql = "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        _assert_read_only(sql)
        cur.execute(sql)
        try:
            cur.execute("SET LOCAL statement_timeout = '600000'")
            for name, q in META_QUERIES.items():
                _assert_read_only(q)
                cur.execute(q)
                meta[name] = cur.fetchone() if name in ("session", "role") else cur.fetchone()[0]
            meta["session"] = {
                "current_user": meta["session"][0], "current_database": meta["session"][1],
                "transaction_isolation": meta["session"][2],
                "transaction_read_only": meta["session"][3], "server_version": meta["session"][4],
            }
            meta["role"] = {
                "rolname": meta["role"][0], "rolcanlogin": meta["role"][1],
                "rolcreatedb": meta["role"][2], "rolsuper": meta["role"][3],
                "rolbypassrls": meta["role"][4],
            }
            if meta["session"]["transaction_read_only"] != "on":
                raise RuntimeError("READ ONLY transaction not active; aborting snapshot")

            _assert_read_only(QUERY_OBSERVATIONS)
            cur.execute(QUERY_OBSERVATIONS, (POINT_B, BASELINE))
            rows = cur.fetchall()
            meta["query_identity"]["observations"] = _sha256_text(QUERY_OBSERVATIONS.strip())

            registry = None
            try:
                _assert_read_only(QUERY_REGISTRY)
                cur.execute(QUERY_REGISTRY, (POINT_B, BASELINE))
                registry = cur.fetchall()
                meta["query_identity"]["registry"] = _sha256_text(QUERY_REGISTRY.strip())
            except Exception as exc:  # pragma: no cover - defensive
                conn.rollback()
                raise RuntimeError(f"registry query failed: {exc}") from exc

            direction_state = None
            try:
                _assert_read_only(QUERY_DIRECTION_STATE)
                cur.execute(QUERY_DIRECTION_STATE, (POINT_B, BASELINE))
                direction_state = [list(r) for r in cur.fetchall()]
                meta["query_identity"]["direction_state"] = _sha256_text(QUERY_DIRECTION_STATE.strip())
            except Exception as exc:
                conn.rollback()
                direction_state = {"error": str(exc).strip().splitlines()[0]}

            conn.rollback()
        finally:
            cur.close()
    finally:
        conn.close()

    meta["registry_rows"] = [list(r) for r in registry] if registry else []
    meta["direction_state_rows"] = direction_state
    meta["frozen_geometry"] = {
        "freeze_ts": FREEZE.isoformat(),
        "eligibility": "signal_time > freeze_ts",
        "initial_risk": "1R = |reference_price - invalidation_price|",
        "take_profit": "2R (target_1 = entry +/- 2*risk) -> +2.0 R",
        "stop_loss": "1R -> -1.0 R",
        "timeout_minutes": FROZEN_MAX_HOLD,
        "intrabar_ambiguity": FROZEN_INTRABAR_POLICY + " (same-candle collision -> -1.0 R)",
        "timeout_return": (
            "return_at_240m = (last_close_in_(signal,signal+240m] - entry)/entry*100 "
            "(raw %, direction-agnostic); R = sign(direction)*ret*entry/(100*|entry-stop|)"),
    }
    meta["experiment_names"] = [POINT_B, BASELINE]

    # Normalize rows and compute required snapshot fields.
    norm = []
    for r in rows:
        rec = dict(zip(ROW_FIELDS, [
            r[1],  # observation_id
            r[0],  # experiment_id
            r[2], r[3],
            r[4].astimezone(timezone.utc).isoformat() if isinstance(r[4], datetime) else str(r[4]),
            r[5], r[6],
            int(r[7]) if r[7] not in (None, "") else None,
            r[8], r[9],
            r[10], r[11], r[12], r[13], r[14], r[15],
            bool(r[16]),
            bool(r[17]) if r[17] is not None else False,
            r[18],
            bool(r[19]) if r[19] is not None else None,
            bool(r[20]) if r[20] is not None else None,
            bool(r[21]) if r[21] is not None else None,
            bool(r[22]) if r[22] is not None else None,
            bool(r[23]) if r[23] is not None else None,
            r[24], r[25],
            r[26].astimezone(timezone.utc).isoformat() if isinstance(r[26], datetime) else (str(r[26]) if r[26] else ""),
            r[27].astimezone(timezone.utc).isoformat() if isinstance(r[27], datetime) else (str(r[27]) if r[27] else ""),
            r[28].astimezone(timezone.utc).isoformat() if isinstance(r[28], datetime) else (str(r[28]) if r[28] else ""),
        ]))
        norm.append(rec)

    rows_path = outdir / SNAPSHOT_ROWS_FILE
    with rows_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ROW_FIELDS, lineterminator="\n")
        w.writeheader()
        for rec in norm:
            w.writerow(rec)

    asof = _parse_ts(meta["transaction_timestamp"])
    eligible = [x for x in norm if _parse_ts(x["signal_time"]) > FREEZE]
    symbols = sorted({x["symbol"] for x in eligible})
    days = sorted({_parse_ts(x["signal_time"]).date().isoformat() for x in eligible})
    latest_finalized = max(
        (_parse_ts(x["outcome_updated_at"]) for x in eligible if x["is_final"] and x["outcome_updated_at"]),
        default=None)

    per_cohort = {}
    for exp in (POINT_B, BASELINE):
        sub = [x for x in eligible if x["experiment_id"] == exp]
        per_cohort[exp] = {
            "rows_post_freeze": len(sub),
            "with_outcome_row": sum(1 for x in sub if x["has_outcome"]),
            "missing_outcome_row": sum(1 for x in sub if not x["has_outcome"]),
            "finalized": sum(1 for x in sub if x["is_final"]),
            "not_final": sum(1 for x in sub if not x["is_final"]),
            "immature_240m": sum(
                1 for x in sub
                if _parse_ts(x["signal_time"]) + timedelta(minutes=FROZEN_MAX_HOLD) > asof),
            "earliest_signal_time": min((_parse_ts(x["signal_time"]) for x in sub), default=None),
            "latest_signal_time": max((_parse_ts(x["signal_time"]) for x in sub), default=None),
            "symbols_n": len({x["symbol"] for x in sub}),
            "calendar_dates": sorted({_parse_ts(x["signal_time"]).date().isoformat() for x in sub}),
        }

    meta.update({
        "SNAPSHOT_ASOF_UTC": asof.isoformat(),
        "POST_FREEZE_ELIGIBLE_N": len(eligible),
        "FINALIZED_N": sum(1 for x in eligible if x["is_final"]),
        "IMMATURE_N": sum(1 for x in eligible
                         if _parse_ts(x["signal_time"]) + timedelta(minutes=FROZEN_MAX_HOLD) > asof),
        "SYMBOLS_N": len(symbols),
        "OOS_ELAPSED_HOURS": round((asof - FREEZE).total_seconds() / 3600.0, 6),
        "OOS_CALENDAR_DATES": days,
        "EARLIEST_ELIGIBLE": min((_parse_ts(x["signal_time"]) for x in eligible), default=None).isoformat(),
        "LATEST_ELIGIBLE": max((_parse_ts(x["signal_time"]) for x in eligible), default=None).isoformat(),
        "LATEST_FINALIZED_AT": latest_finalized.isoformat() if latest_finalized else None,
        "SOURCE_COMPLETENESS": per_cohort,
        "source_row_counts": {
            "raw_joined_rows": len(norm),
            "post_freeze_eligible_rows": len(eligible),
            **{f"{exp}": v["rows_post_freeze"] for exp, v in per_cohort.items()},
        },
        "snapshot_rows_file": SNAPSHOT_ROWS_FILE,
        "snapshot_rows_sha256": _sha256_file(rows_path),
    })

    meta_path = outdir / SNAPSHOT_META_FILE
    with meta_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(meta, indent=2, ensure_ascii=False, default=str) + "\n")
    return meta


# --------------------------- analysis phase ---------------------------

def load_rows(outdir: Path) -> list[dict]:
    rows = []
    with (outdir / SNAPSHOT_ROWS_FILE).open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        missing = [f for f in ROW_FIELDS if f not in header]
        if missing:
            raise ValueError(
                f"malformed snapshot {SNAPSHOT_ROWS_FILE}: missing required "
                f"columns {missing}")
        for raw in reader:
            rec = dict(raw)
            rec["observation_id"] = int(rec["observation_id"])
            rec["frozen_max_hold"] = int(rec["frozen_max_hold"]) if rec["frozen_max_hold"] else None
            for key in ("reference_price", "invalidation_price", "target_1",
                        "variant_entry", "variant_stop", "variant_target",
                        "return_at_240m", "time_to_tp", "time_to_sl"):
                rec[key] = _parse_float(rec[key])
            for key in ("has_outcome", "is_final", "tp_hit", "sl_hit", "tp_before_sl",
                        "sl_before_tp", "ambiguous_intrabar"):
                rec[key] = _parse_bool(rec[key])
            for key in ("signal_time", "evaluated_240m_at", "outcome_updated_at",
                        "observation_created_at"):
                rec[key] = _parse_ts(rec[key])
            if rec["source_signal_id"]:
                rec["source_signal_id"] = int(rec["source_signal_id"])
            if rec["source_observation_id"]:
                rec["source_observation_id"] = int(rec["source_observation_id"])
            rows.append(rec)
    return rows


def _row_outcome(row: dict) -> tuple[float | None, str]:
    """Frozen gross R plus exit-path label for one observation row."""
    return frozen_gross_r(
        row["direction"], row["reference_price"], row["invalidation_price"], row["target_1"],
        row["return_at_240m"], row["tp_before_sl"], row["sl_before_tp"],
        row["ambiguous_intrabar"], row["frozen_intrabar_policy"], row["frozen_max_hold"])


def match_and_classify(rows: list[dict], asof: datetime):
    """Identity matching + full unmatched/funnel lineage classification.

    Returns (pairs, unmatched, funnel, outcome_audit).
    Fail-closed rules (mirroring tools/research/htf_point_b_final_paired_oos_verdict_v1):
      duplicates, identity mismatches, baseline-after-Point-B, pre-freeze rows.
    Additional distinctions preserved here:
      immature vs mature-but-unfinalized vs invalid frozen geometry/R.
    """
    rejects = Counter()
    outcome_audit = Counter()
    prefreeze_rows = 0
    invalid_identity_rows = 0
    eligible_rows = []

    for row in rows:
        if row["experiment_id"] not in (POINT_B, BASELINE):
            rejects["other_experiment"] += 1
            continue
        if not row["setup_event_id"] or row["direction"] not in ("LONG", "SHORT") or not row["symbol"]:
            invalid_identity_rows += 1
            continue
        if row["signal_time"] <= FREEZE:
            prefreeze_rows += 1
            continue
        # Per-row frozen outcome audit.
        if row["variant_entry"] is not None or row["variant_stop"] is not None or row["variant_target"] is not None:
            outcome_audit["variant_geometry_present"] += 1
        r_val, path = _row_outcome(row)
        if row["is_final"] and r_val is None:
            outcome_audit[f"finalized_r_reject:{path}"] += 1
        if row["is_final"]:
            if row["evaluated_240m_at"] is not None and \
                    row["evaluated_240m_at"] < row["signal_time"] + timedelta(minutes=FROZEN_MAX_HOLD):
                outcome_audit["premature_finalization"] += 1
            if row["ambiguous_intrabar"] is True and row["sl_before_tp"] is not True:
                outcome_audit["ambiguous_without_stop_first"] += 1
            if path == "TIMEOUT" and row["return_at_240m"] is None:
                outcome_audit["timeout_missing_return"] += 1
        elif row["signal_time"] + timedelta(minutes=FROZEN_MAX_HOLD) <= asof:
            outcome_audit["mature_but_not_finalized"] += 1
        else:
            outcome_audit["immature_not_finalized"] += 1
        row["_gross_r"] = r_val if row["is_final"] else None
        row["_path"] = path
        eligible_rows.append(row)

    # Identity groups.
    groups: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(
        lambda: {POINT_B: [], BASELINE: []})
    for row in eligible_rows:
        groups[(row["setup_event_id"], row["direction"])][row["experiment_id"]].append(row)

    # Global collision diagnostics.
    by_event: dict[str, dict] = defaultdict(lambda: {"symbols": set(), "directions": set(),
                                                     "cohorts": set(), "rows": []})
    for row in eligible_rows:
        e = by_event[row["setup_event_id"]]
        e["symbols"].add(row["symbol"])
        e["directions"].add(row["direction"])
        e["cohorts"].add(row["experiment_id"])
        e["rows"].append(row)
    cross_symbol = sorted(eid for eid, e in by_event.items() if len(e["symbols"]) > 1)
    cross_direction = sorted(eid for eid, e in by_event.items() if len(e["directions"]) > 1)

    pairs: list[dict] = []
    unmatched: list[dict] = []
    funnel = Counter()

    for (event_id, direction), cohort in sorted(groups.items()):
        pb_rows, bl_rows = cohort[POINT_B], cohort[BASELINE]
        all_rows = pb_rows + bl_rows
        symbol = sorted({r["symbol"] for r in all_rows})
        base = {
            "event_id": event_id, "direction": direction,
            "symbols": ";".join(symbol),
            "point_b_observation_ids": ";".join(str(r["observation_id"]) for r in pb_rows),
            "baseline_observation_ids": ";".join(str(r["observation_id"]) for r in bl_rows),
            "point_b_signal_times": ";".join(r["signal_time"].isoformat() for r in pb_rows),
            "baseline_signal_times": ";".join(r["signal_time"].isoformat() for r in bl_rows),
        }
        if len(pb_rows) > 1 or len(bl_rows) > 1:
            funnel["invalid_lineage"] += 1
            funnel["duplicate_identity"] += 1
            unmatched.append({**base, "reason": "duplicate_identity",
                              "detail": f"point_b_rows={len(pb_rows)};baseline_rows={len(bl_rows)}"})
            continue
        if not pb_rows:
            funnel["baseline_only"] += 1
            unmatched.append({**base, "reason": "baseline_only_event",
                              "detail": f"baseline_r={_fmt(bl_rows[0]['_gross_r'])};baseline_path={bl_rows[0]['_path']}"})
            continue
        if not bl_rows:
            funnel["point_b_only"] += 1
            unmatched.append({**base, "reason": "point_b_only_event",
                              "detail": f"point_b_r={_fmt(pb_rows[0]['_gross_r'])};point_b_path={pb_rows[0]['_path']}"})
            continue
        pb, bl = pb_rows[0], bl_rows[0]
        if pb["symbol"] != bl["symbol"] or pb["direction"] != bl["direction"]:
            funnel["invalid_lineage"] += 1
            funnel["symbol_or_direction_mismatch"] += 1
            unmatched.append({**base, "reason": "symbol_or_direction_mismatch",
                              "detail": f"pb={pb['symbol']}/{pb['direction']};bl={bl['symbol']}/{bl['direction']}"})
            continue
        if bl["signal_time"] > pb["signal_time"]:
            funnel["invalid_lineage"] += 1
            funnel["baseline_after_point_b"] += 1
            unmatched.append({**base, "reason": "baseline_after_point_b",
                              "detail": f"delay_minutes={(bl['signal_time'] - pb['signal_time']).total_seconds() / 60:.1f}"})
            continue
        funnel["matched_identity"] += 1
        funnel["matched_eligible"] += 1

        mature = (pb["signal_time"] + timedelta(minutes=FROZEN_MAX_HOLD) <= asof
                  and bl["signal_time"] + timedelta(minutes=FROZEN_MAX_HOLD) <= asof)
        if pb["_gross_r"] is not None and bl["_gross_r"] is not None:
            entry_pb = float(pb["reference_price"])
            entry_bl = float(bl["reference_price"])
            risk_pb = abs(entry_pb - float(pb["invalidation_price"]))
            risk_bl = abs(entry_bl - float(bl["invalidation_price"]))
            pairs.append({
                "event_id": event_id, "direction": direction, "symbol": pb["symbol"],
                "day": pb["signal_time"].date().isoformat(),
                "point_b_observation_id": pb["observation_id"],
                "baseline_observation_id": bl["observation_id"],
                "point_b_signal_time": pb["signal_time"].isoformat(),
                "baseline_signal_time": bl["signal_time"].isoformat(),
                "delay_minutes": (pb["signal_time"] - bl["signal_time"]).total_seconds() / 60.0,
                "point_b_r": pb["_gross_r"], "baseline_r": bl["_gross_r"],
                "delta_r": pb["_gross_r"] - bl["_gross_r"],
                "point_b_exit": pb["_path"], "baseline_exit": bl["_path"],
                "point_b_entry_price": entry_pb, "baseline_entry_price": entry_bl,
                "point_b_risk": risk_pb, "baseline_risk": risk_bl,
                "entry_price_diff": entry_pb - entry_bl,
                "risk_diff": risk_pb - risk_bl,
            })
            funnel["matched_finalized"] += 1
            continue
        funnel["matched_excluded"] += 1
        reasons = []
        if not mature:
            funnel["immature"] += 1
            reasons.append("immature")
        for side, r in (("point_b", pb), ("baseline", bl)):
            if not r["is_final"]:
                reasons.append(f"{side}:not_finalized:{r['_path']}")
            elif r["_gross_r"] is None:
                reasons.append(f"{side}:invalid_{r['_path']}")
        if mature and not reasons:
            reasons.append("unspecified")
        unmatched.append({**base, "reason": "matched_excluded",
                          "detail": ";".join(reasons)})

    total_identities = len(groups)
    funnel["source_event_identities"] = total_identities
    funnel["source_event_ids"] = len(by_event)
    funnel["prefreeze_rows"] = prefreeze_rows
    funnel["invalid_identity_rows"] = invalid_identity_rows
    funnel["cross_symbol_event_ids"] = len(cross_symbol)
    funnel["cross_direction_event_ids"] = len(cross_direction)
    funnel["source_observations_point_b"] = sum(1 for r in eligible_rows if r["experiment_id"] == POINT_B)
    funnel["source_observations_baseline"] = sum(1 for r in eligible_rows if r["experiment_id"] == BASELINE)

    accounted = (funnel["matched_finalized"] + funnel["matched_excluded"]
                 + funnel["baseline_only"] + funnel["point_b_only"]
                 + funnel["duplicate_identity"] + funnel["symbol_or_direction_mismatch"]
                 + funnel["baseline_after_point_b"])
    funnel["conservation_ok"] = bool(accounted == total_identities)
    funnel["conservation_accounted"] = accounted

    return pairs, unmatched, funnel, dict(sorted(outcome_audit.items())), {
        "cross_symbol_event_ids": cross_symbol[:50],
        "cross_direction_event_ids": cross_direction[:50],
    }


def _pf(values: list[float]) -> float | None:
    loss = -sum(x for x in values if x < 0)
    return sum(x for x in values if x > 0) / loss if loss > 0 else None


def _percentile_ci(sorted_vals: list[float], reps: int) -> list[float] | None:
    if not sorted_vals:
        return None
    return [sorted_vals[int((reps - 1) * 0.025)], sorted_vals[int((reps - 1) * 0.975)]]


def bootstrap_mean_ci(values: list[float], reps: int, seed: int) -> dict:
    """Event-level paired bootstrap of the mean delta (IID; secondary evidence)."""
    if not values:
        return {"reps": reps, "seed": seed, "ci95": None, "positive_fraction": None}
    rng = random.Random(seed)
    n = len(values)
    out = []
    for _ in range(reps):
        out.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    out.sort()
    return {"resampling_unit": "event", "reps": reps, "seed": seed,
            "ci95": _percentile_ci(out, reps),
            "positive_fraction": sum(v > 0 for v in out) / reps}


def cluster_bootstrap_ci(items: list[dict], key: str, reps: int, seed: int) -> dict:
    """Cluster bootstrap of the pooled mean delta (cluster = key value).

    Resamples whole clusters with replacement and recomputes the pooled mean
    (sum of deltas / sum of cluster sizes) so unequal cluster sizes are kept.
    """
    groups: dict[str, list[float]] = defaultdict(list)
    for r in items:
        groups[r[key]].append(r["delta_r"])
    ordered = [groups[k] for k in sorted(groups)]
    sizes = [len(g) for g in ordered]
    n = len(ordered)
    result = {
        "resampling_unit": key, "clusters": n, "reps": reps, "seed": seed,
        "ci95": None, "positive_fraction": None,
        "kish_effective_clusters": round((sum(sizes) ** 2) / sum(s * s for s in sizes), 4)
        if sizes and sum(s * s for s in sizes) > 0 else None,
    }
    if n < 2:
        return result
    rng = random.Random(seed)
    sums = [sum(g) for g in ordered]
    out = []
    for _ in range(reps):
        picks = [rng.randrange(n) for _ in range(n)]
        denom = sum(sizes[i] for i in picks)
        out.append(sum(sums[i] for i in picks) / denom)
    out.sort()
    result["ci95"] = _percentile_ci(out, reps)
    result["positive_fraction"] = sum(v > 0 for v in out) / reps
    return result


def directional_stats(pairs: list[dict], reps: int, seed: int) -> dict:
    if not pairs:
        return {"matched_finalized_n": 0}
    delta = [p["delta_r"] for p in pairs]
    pb = [p["point_b_r"] for p in pairs]
    bl = [p["baseline_r"] for p in pairs]
    days = sorted({p["day"] for p in pairs})
    symbols = sorted({p["symbol"] for p in pairs})

    day_sums = {d: sum(p["delta_r"] for p in pairs if p["day"] == d) for d in days}
    day_ns = {d: sum(1 for p in pairs if p["day"] == d) for d in days}
    sym_sums = {s: sum(p["delta_r"] for p in pairs if p["symbol"] == s) for s in symbols}
    sym_ns = {s: sum(1 for p in pairs if p["symbol"] == s) for s in symbols}
    total_delta = sum(delta)

    lodo = {}
    for d in days:
        rest = [p["delta_r"] for p in pairs if p["day"] != d]
        lodo[d] = _round(mean(rest)) if rest else None
    lodo_sign_flips = sum(
        1 for d, v in lodo.items()
        if v is not None and total_delta != 0 and (v > 0) != (mean(delta) > 0))
    loso = {}
    for s in symbols:
        rest = [p["delta_r"] for p in pairs if p["symbol"] != s]
        loso[s] = _round(mean(rest)) if rest else None
    loso_sign_flips = sum(
        1 for s, v in loso.items()
        if v is not None and total_delta != 0 and (v > 0) != (mean(delta) > 0))

    cells: dict[tuple[str, str], float] = defaultdict(float)
    for p in pairs:
        cells[(p["day"], p["symbol"])] += p["delta_r"]
    top_cells = sorted(cells.items(), key=lambda kv: -abs(kv[1]))[:5]

    first_day, last_day = days[0], days[-1]
    boundary = {
        "first_day": first_day, "last_day": last_day,
        "first_day_n": day_ns[first_day], "last_day_n": day_ns[last_day],
        "drop_first_day_mean_delta_r": _round(mean([p["delta_r"] for p in pairs if p["day"] != first_day]))
        if len(days) > 1 else None,
        "drop_last_day_mean_delta_r": _round(mean([p["delta_r"] for p in pairs if p["day"] != last_day]))
        if len(days) > 1 else None,
        "drop_both_days_mean_delta_r": _round(mean(
            [p["delta_r"] for p in pairs if p["day"] not in (first_day, last_day)]))
        if len(days) > 2 else None,
        "drop_both_days_n": sum(1 for p in pairs if p["day"] not in (first_day, last_day)),
    }

    stats = {
        "matched_finalized_n": len(pairs),
        "symbols_n": len(symbols),
        "calendar_days_n": len(days),
        "oos_calendar_dates": days,
        "point_b_mean_r": _round(mean(pb)), "point_b_median_r": _round(median(pb)),
        "baseline_mean_r": _round(mean(bl)), "baseline_median_r": _round(median(bl)),
        "point_b_pf": _round(_pf(pb)), "baseline_pf": _round(_pf(bl)),
        "point_b_total_r": _round(sum(pb)), "baseline_total_r": _round(sum(bl)),
        "point_b_positive_r_sum": _round(sum(x for x in pb if x > 0)),
        "point_b_negative_r_sum": _round(sum(x for x in pb if x < 0)),
        "baseline_positive_r_sum": _round(sum(x for x in bl if x > 0)),
        "baseline_negative_r_sum": _round(sum(x for x in bl if x < 0)),
        "point_b_exits": dict(sorted(Counter(p["point_b_exit"] for p in pairs).items())),
        "baseline_exits": dict(sorted(Counter(p["baseline_exit"] for p in pairs).items())),
        "paired_delta_mean_r": _round(mean(delta)),
        "paired_delta_median_r": _round(median(delta)),
        "delta_min_r": _round(min(delta)), "delta_max_r": _round(max(delta)),
        "delta_q25_r": _round(sorted(delta)[len(delta) // 4]),
        "delta_q75_r": _round(sorted(delta)[(3 * len(delta)) // 4]),
        "delta_positive_n": sum(1 for x in delta if x > 0),
        "delta_negative_n": sum(1 for x in delta if x < 0),
        "delta_zero_n": sum(1 for x in delta if x == 0),
        "delta_positive_frac": _round(sum(1 for x in delta if x > 0) / len(delta)),
        "delta_negative_frac": _round(sum(1 for x in delta if x < 0) / len(delta)),
        "delta_zero_frac": _round(sum(1 for x in delta if x == 0) / len(delta)),
        "delay_minutes_mean": _round(mean([p["delay_minutes"] for p in pairs])),
        "delay_minutes_median": _round(median([p["delay_minutes"] for p in pairs])),
        "entry_price_diff_mean": _round(mean([p["entry_price_diff"] for p in pairs])),
        "risk_diff_mean": _round(mean([p["risk_diff"] for p in pairs])),
        "identical_entry_price_n": sum(1 for p in pairs if p["entry_price_diff"] == 0),
    }

    day_ci = cluster_bootstrap_ci(pairs, "day", reps, seed)
    sym_ci = cluster_bootstrap_ci(pairs, "symbol", reps, seed)
    ev_ci = bootstrap_mean_ci(delta, reps, seed)
    day_ci["positive_days"] = sum(1 for v in day_sums.values() if v > 0)
    day_ci["negative_days"] = sum(1 for v in day_sums.values() if v < 0)
    day_ci["zero_days"] = sum(1 for v in day_sums.values() if v == 0)
    day_ci["daily_delta_total_r"] = {d: _round(v) for d, v in day_sums.items()}
    day_ci["daily_n"] = day_ns

    stats["day_cluster"] = day_ci
    stats["symbol_cluster"] = sym_ci
    stats["event_bootstrap"] = ev_ci
    stats["leave_one_day_out_mean_delta_r"] = lodo
    stats["leave_one_day_out_sign_flips"] = lodo_sign_flips
    lodo_vals = [v for v in lodo.values() if v is not None]
    loso_vals = [v for v in loso.values() if v is not None]
    stats["leave_one_day_out_min_r"] = _round(min(lodo_vals)) if lodo_vals else None
    stats["leave_one_day_out_max_r"] = _round(max(lodo_vals)) if lodo_vals else None
    stats["leave_one_symbol_out_sign_flips"] = loso_sign_flips
    stats["leave_one_symbol_out_min_r"] = _round(min(loso_vals)) if loso_vals else None
    stats["leave_one_symbol_out_max_r"] = _round(max(loso_vals)) if loso_vals else None
    stats["max_single_day_total_delta_r"] = _round(max(day_sums.values(), key=abs))
    stats["max_single_day_share_of_total_delta"] = _round(
        max(day_sums.values(), key=abs) / total_delta) if total_delta else None
    stats["max_single_symbol_total_delta_r"] = _round(max(sym_sums.values(), key=abs))
    stats["max_single_symbol_share_of_total_delta"] = _round(
        max(sym_sums.values(), key=abs) / total_delta) if total_delta else None
    stats["max_day_symbol_cell_total_delta_r"] = _round(top_cells[0][1]) if top_cells else None
    stats["max_day_symbol_cell_share_of_total_delta"] = _round(top_cells[0][1] / total_delta) \
        if top_cells and total_delta else None
    stats["top5_day_symbol_cells"] = [
        {"day": d, "symbol": s, "delta_total_r": _round(v)} for (d, s), v in top_cells]
    stats["boundary_sensitivity"] = boundary
    return stats


def maturity_gates(stats_by_dir: dict, asof: datetime) -> dict:
    total_finalized = sum(v.get("matched_finalized_n", 0) for v in stats_by_dir.values())
    long_n = stats_by_dir.get("LONG", {}).get("matched_finalized_n", 0)
    short_n = stats_by_dir.get("SHORT", {}).get("matched_finalized_n", 0)
    symbols = set()
    days = set()
    for v in stats_by_dir.values():
        symbols |= set(v.get("symbols", []))
        days |= set(v.get("oos_calendar_dates", []))
    gates = {
        "TOTAL_FINALIZED": {"value": total_finalized, "threshold": 300, "pass": total_finalized >= 300},
        "LONG_FINALIZED": {"value": long_n, "threshold": 100, "pass": long_n >= 100},
        "SHORT_FINALIZED": {"value": short_n, "threshold": 100, "pass": short_n >= 100},
        "UNIQUE_SYMBOLS": {"value": len(symbols), "threshold": 30, "pass": len(symbols) >= 30},
        "OOS_ELAPSED_DAYS": {"value": round((asof - FREEZE).total_seconds() / 86400.0, 6),
                             "threshold": 14, "pass": asof >= FREEZE + timedelta(days=14)},
    }
    gates["OOS_CALENDAR_DATE_COUNT"] = {"value": len(days), "note": "reported independently; not a substitute for elapsed hours"}
    gates["all_pass"] = all(g["pass"] for g in gates.values() if isinstance(g, dict) and "pass" in g)
    return gates


def decide_verdict(stats: dict, gates_pass: bool, integrity_pass: bool) -> tuple[str, str]:
    if not integrity_pass:
        return "BLOCKED", "unresolved integrity/matching blocker"
    if not stats or not gates_pass:
        return "INCONCLUSIVE", "maturity gate not satisfied or no matched evidence"
    day_ci = stats["day_cluster"]["ci95"]
    sym_ci = stats["symbol_cluster"]["ci95"]
    m = stats["paired_delta_mean_r"]
    if day_ci is None or sym_ci is None:
        return "INCONCLUSIVE", "fewer than two independent clusters in day or symbol dimension"
    if m > 0 and day_ci[0] > 0 and sym_ci[0] > 0:
        return "PROMISING", "positive paired uplift with both cluster CIs strictly above zero"
    if m <= 0 and day_ci[1] < 0 and sym_ci[1] < 0:
        return "NO_EDGE", "robustly non-positive incremental result (both cluster CIs strictly below zero)"
    return "INCONCLUSIVE", "cluster confidence intervals overlap zero or sensitivity weakens the finding"


def _validate_meta(meta: dict) -> datetime:
    if not isinstance(meta, dict):
        raise ValueError("malformed snapshot_meta.json: not a JSON object")
    if not meta.get("SNAPSHOT_ASOF_UTC"):
        raise ValueError("malformed snapshot_meta.json: missing SNAPSHOT_ASOF_UTC")
    try:
        return _parse_ts(meta["SNAPSHOT_ASOF_UTC"])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"malformed snapshot_meta.json: bad SNAPSHOT_ASOF_UTC: {exc}") from exc


def analyze(rows: list[dict], meta: dict, reps: int, seed: int) -> tuple[dict, dict]:
    asof = _validate_meta(meta)
    pairs, unmatched, funnel, outcome_audit, collision_detail = match_and_classify(rows, asof)

    per_dir_funnel = {}
    dir_identities = {}
    for d in ("LONG", "SHORT"):
        sub_pairs = [p for p in pairs if p["direction"] == d]
        sub_unmatched = [u for u in unmatched if u["direction"] == d]
        reasons = Counter(u["reason"] for u in sub_unmatched)
        dir_identities[d] = len(sub_pairs) + len(sub_unmatched)
        per_dir_funnel[d] = {
            "source_event_identities": dir_identities[d],
            "matched_identity": len(sub_pairs) + reasons.get("matched_excluded", 0),
            "matched_finalized": len(sub_pairs),
            "matched_excluded": reasons.get("matched_excluded", 0),
            "immature": sum(1 for u in sub_unmatched
                            if u["reason"] == "matched_excluded" and "immature" in u["detail"]),
            "baseline_only": reasons.get("baseline_only_event", 0),
            "point_b_only": reasons.get("point_b_only_event", 0),
            "invalid_lineage": sum(reasons.get(k, 0) for k in
                                   ("duplicate_identity", "symbol_or_direction_mismatch",
                                    "baseline_after_point_b")),
            "unmatched": len(sub_unmatched),
            "unmatched_reasons": dict(sorted(reasons.items())),
        }

    stats_by_dir = {}
    for d in ("LONG", "SHORT"):
        sub = [p for p in pairs if p["direction"] == d]
        st = directional_stats(sub, reps, seed)
        st["symbols"] = sorted({p["symbol"] for p in sub})
        st["source_event_coverage"] = _round(
            st.get("matched_finalized_n", 0) / dir_identities[d], 6) if dir_identities[d] else None
        stats_by_dir[d] = st

    gates = maturity_gates(stats_by_dir, asof)
    integrity_pass = bool(funnel.get("conservation_ok")) and \
        not any(k.startswith("finalized_r_reject") or k in
                ("premature_finalization", "ambiguous_without_stop_first", "timeout_missing_return")
                for k in outcome_audit)

    verdicts = {}
    for d in ("LONG", "SHORT"):
        v, why = decide_verdict(stats_by_dir.get(d), gates["all_pass"], integrity_pass)
        verdicts[d] = {"verdict": v, "rationale": why}

    overall_words = {("NO_EDGE", "NO_EDGE"): "NOT_SUPPORTED_BOTH_DIRECTIONS",
                     ("NO_EDGE", "INCONCLUSIVE"): "NOT_SUPPORTED_LONG__INCONCLUSIVE_SHORT",
                     ("INCONCLUSIVE", "NO_EDGE"): "INCONCLUSIVE_LONG__NOT_SUPPORTED_SHORT",
                     ("INCONCLUSIVE", "INCONCLUSIVE"): "INCONCLUSIVE_BOTH_DIRECTIONS"}
    key = (verdicts["LONG"]["verdict"], verdicts["SHORT"]["verdict"])
    if "BLOCKED" in key:
        overall = "BLOCKED"
    elif key == ("PROMISING", "PROMISING"):
        overall = "SUPPORTED_BOTH_DIRECTIONS"
    elif "PROMISING" in key:
        overall = ("PROMISING_LONG__NOT_ROBUST_SHORT" if verdicts["SHORT"]["verdict"] != "PROMISING"
                   else "PROMISING_SHORT__NOT_ROBUST_LONG")
    else:
        overall = overall_words.get(key, "MIXED")

    summary = {
        "schema_version": SCHEMA_VERSION,
        "issue": ISSUE,
        "asof_utc": meta["SNAPSHOT_ASOF_UTC"],
        "db_snapshot_asof": meta.get("db_now"),
        "snapshot_identifier": meta.get("snapshot_identifier"),
        "freeze_ts": FREEZE.isoformat(),
        "elapsed_14_days_met": asof >= FREEZE + timedelta(days=14),
        "oos_elapsed_hours": _round((asof - FREEZE).total_seconds() / 3600.0, 6),
        "frozen_geometry": meta.get("frozen_geometry"),
        "snapshot_fields": {k: meta.get(k) for k in (
            "SNAPSHOT_ASOF_UTC", "POST_FREEZE_ELIGIBLE_N", "FINALIZED_N", "IMMATURE_N",
            "SYMBOLS_N", "OOS_ELAPSED_HOURS", "OOS_CALENDAR_DATES", "EARLIEST_ELIGIBLE",
            "LATEST_ELIGIBLE", "LATEST_FINALIZED_AT", "SOURCE_COMPLETENESS")},
        "funnel": dict(sorted(funnel.items())),
        "funnel_by_direction": per_dir_funnel,
        "outcome_audit": outcome_audit,
        "collision_diagnostics": {k: len(v) for k, v in collision_detail.items()},
        "collision_event_id_samples": collision_detail,
        "directions": stats_by_dir,
        "maturity_gates": gates,
        "verdicts": {**verdicts, "OVERALL_INCREMENTAL_POINT_B_HYPOTHESIS": {
            "verdict": overall,
            "per_direction": {d: verdicts[d]["verdict"] for d in ("LONG", "SHORT")},
        }},
        "bootstrap": {"random_seed": seed, "bootstrap_iterations": reps,
                      "confidence_level": CONF_LEVEL,
                      "resampling_units": ["event", "utc_day", "symbol"]},
        "selection_bias": {
            "estimand": "event-conditional paired delta on matched finalized events",
            "conditions_on": "Point-B observation completed AND baseline identity available AND both finalized",
            "does_not_establish": [
                "unconditional strategy superiority",
                "value on events where Point-B never completes",
                "net executable returns (fees/funding/slippage excluded)",
            ],
            "imputation_policy": "none: unobserved Point-B outcomes are never imputed",
        },
        "limitations": [
            "gross research geometry only; no fee/funding/slippage/executable fill model",
            "matched estimand conditions on Point-B completion (selection on treatment availability)",
            "is_final is not independent proof of contiguous candle coverage",
            "fewer than ~15 independent UTC day clusters; overlapping 240m trades are not independent",
            "event-IID bootstrap reported only as secondary evidence",
            "calendar-date count is not a substitute for elapsed hours",
            "entry times, entry prices and risk geometry differ between cohorts; R is normalized, not identical trades",
        ],
    }
    return summary, {"pairs": pairs, "unmatched": unmatched}


def write_artifacts(outdir: Path, summary: dict, details: dict, meta: dict,
                    git_meta: dict, allow_overwrite: bool = False) -> dict:
    artifacts = {}
    _CANONICAL_FILES = [
        "HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1.csv",
        "HTF_POINT_B_FINAL_PAIRED_UNMATCHED_V1.csv",
        "HTF_POINT_B_FINAL_PAIRED_DAILY_V1.csv",
        "HTF_POINT_B_FINAL_PAIRED_SYMBOL_V1.csv",
        "HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1.json",
        "HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1.md",
        "HTF_POINT_B_FINAL_PAIRED_SOURCE_MANIFEST_V1.json",
        "SHA256SUMS.txt",
    ]
    conflicts = [n for n in _CANONICAL_FILES if (outdir / n).exists()]
    if conflicts and not allow_overwrite:
        raise RuntimeError(
            f"refusing to overwrite canonical results {conflicts} in {outdir} "
            "(pass --allow-artifact-overwrite for an explicitly approved rerun)")

    def _w(name: str, text: str) -> None:
        p = outdir / name
        with p.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        artifacts[name] = _sha256_file(p)

    pairs = details["pairs"]
    unmatched = details["unmatched"]

    # 1. paired CSV
    csv_path = outdir / "HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1.csv"
    fields = ["event_id", "direction", "symbol", "day",
              "point_b_observation_id", "baseline_observation_id",
              "point_b_signal_time", "baseline_signal_time", "delay_minutes",
              "point_b_entry_price", "baseline_entry_price", "entry_price_diff",
              "point_b_risk", "baseline_risk", "risk_diff",
              "point_b_r", "baseline_r", "delta_r", "point_b_exit", "baseline_exit"]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for p in sorted(pairs, key=lambda x: (x["direction"], x["day"], x["symbol"], x["event_id"])):
            row = {k: p[k] for k in fields}
            for k in ("delay_minutes", "point_b_entry_price", "baseline_entry_price",
                      "entry_price_diff", "point_b_risk", "baseline_risk", "risk_diff",
                      "point_b_r", "baseline_r", "delta_r"):
                row[k] = _fmt(row[k])
            w.writerow(row)
    artifacts[csv_path.name] = _sha256_file(csv_path)

    # 2. unmatched CSV
    u_path = outdir / "HTF_POINT_B_FINAL_PAIRED_UNMATCHED_V1.csv"
    ufields = ["event_id", "direction", "symbols", "reason", "detail",
               "point_b_observation_ids", "baseline_observation_ids",
               "point_b_signal_times", "baseline_signal_times"]
    with u_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ufields, lineterminator="\n")
        w.writeheader()
        for u in sorted(unmatched, key=lambda x: (x["direction"], x["reason"], x["event_id"])):
            w.writerow({k: u.get(k, "") for k in ufields})
    artifacts[u_path.name] = _sha256_file(u_path)

    # 3. daily CSV
    d_path = outdir / "HTF_POINT_B_FINAL_PAIRED_DAILY_V1.csv"
    dfields = ["direction", "day", "n_pairs", "point_b_total_r", "baseline_total_r",
               "delta_total_r", "delta_mean_r", "delta_median_r",
               "positive_n", "negative_n", "zero_n", "symbols_n"]
    with d_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=dfields, lineterminator="\n")
        w.writeheader()
        for direction in ("LONG", "SHORT"):
            sub = [p for p in pairs if p["direction"] == direction]
            for day in sorted({p["day"] for p in sub}):
                ds = [p for p in sub if p["day"] == day]
                dlt = [p["delta_r"] for p in ds]
                w.writerow({
                    "direction": direction, "day": day, "n_pairs": len(ds),
                    "point_b_total_r": _fmt(sum(p["point_b_r"] for p in ds)),
                    "baseline_total_r": _fmt(sum(p["baseline_r"] for p in ds)),
                    "delta_total_r": _fmt(sum(dlt)),
                    "delta_mean_r": _fmt(mean(dlt)),
                    "delta_median_r": _fmt(median(dlt)),
                    "positive_n": sum(1 for x in dlt if x > 0),
                    "negative_n": sum(1 for x in dlt if x < 0),
                    "zero_n": sum(1 for x in dlt if x == 0),
                    "symbols_n": len({p["symbol"] for p in ds}),
                })
    artifacts[d_path.name] = _sha256_file(d_path)

    # 4. symbol CSV
    s_path = outdir / "HTF_POINT_B_FINAL_PAIRED_SYMBOL_V1.csv"
    sfields = ["direction", "symbol", "n_pairs", "n_days", "point_b_total_r",
               "baseline_total_r", "delta_total_r", "delta_mean_r",
               "share_of_total_delta"]
    with s_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=sfields, lineterminator="\n")
        w.writeheader()
        for direction in ("LONG", "SHORT"):
            sub = [p for p in pairs if p["direction"] == direction]
            total = sum(p["delta_r"] for p in sub)
            for symbol in sorted({p["symbol"] for p in sub}):
                ss = [p for p in sub if p["symbol"] == symbol]
                dsum = sum(p["delta_r"] for p in ss)
                w.writerow({
                    "direction": direction, "symbol": symbol, "n_pairs": len(ss),
                    "n_days": len({p["day"] for p in ss}),
                    "point_b_total_r": _fmt(sum(p["point_b_r"] for p in ss)),
                    "baseline_total_r": _fmt(sum(p["baseline_r"] for p in ss)),
                    "delta_total_r": _fmt(dsum),
                    "delta_mean_r": _fmt(mean([p["delta_r"] for p in ss])),
                    "share_of_total_delta": _fmt(dsum / total) if total else "",
                })
    artifacts[s_path.name] = _sha256_file(s_path)

    # 5. JSON summary
    _w("HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1.json",
       json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n")

    # 6. MD report
    _w("HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1.md",
       render_markdown(summary, artifacts, git_meta))

    # 7. manifest (hashes of every other artifact)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "audit_timestamp_utc": summary["asof_utc"],
        "db_snapshot_asof": summary.get("db_snapshot_asof"),
        "freeze_ts": summary["freeze_ts"],
        "git_sha": git_meta,
        "experiment_names": [POINT_B, BASELINE],
        "frozen_geometry": summary.get("frozen_geometry"),
        "sql_or_query_identity": meta.get("query_identity"),
        "data_scope": {
            "database": meta.get("session", {}).get("current_database"),
            "db_user": meta.get("session", {}).get("current_user"),
            "transaction_isolation": meta.get("session", {}).get("transaction_isolation"),
            "transaction_read_only": meta.get("session", {}).get("transaction_read_only"),
            "snapshot_identifier": meta.get("snapshot_identifier"),
            "snapshot_rows_sha256": meta.get("snapshot_rows_sha256"),
            "eligibility": "signal_time > 2026-10-06T14:00:00Z",
        },
        "source_row_counts": meta.get("source_row_counts"),
        "random_seed": summary["bootstrap"]["random_seed"],
        "bootstrap_iterations": summary["bootstrap"]["bootstrap_iterations"],
        "artifact_sha256": artifacts,
        "limitations": summary["limitations"],
    }
    _w("HTF_POINT_B_FINAL_PAIRED_SOURCE_MANIFEST_V1.json",
       json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n")

    # Deterministic checksum file over every artifact incl. the manifest.
    sums = outdir / "SHA256SUMS.txt"
    lines = [f"{h}  {name}" for name, h in sorted(artifacts.items())]
    with sums.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    artifacts["SHA256SUMS.txt"] = _sha256_file(sums)
    return artifacts


def render_markdown(summary: dict, artifacts: dict, git_meta: dict) -> str:
    L: list[str] = []
    a = L.append
    a("# HTF_POINT_B_FINAL_PAIRED_OOS_VERDICT_V1 - frozen matched OOS verdict (Issue #169)")
    a("")
    a(f"- Schema: `{summary['schema_version']}`")
    a(f"- Snapshot as-of (DB `transaction_timestamp()`): `{summary['asof_utc']}`")
    a(f"- DB `now()`: `{summary.get('db_snapshot_asof')}` / snapshot id: `{summary.get('snapshot_identifier')}`")
    a(f"- Freeze: `{summary['freeze_ts']}`; eligibility `signal_time > freeze_ts`")
    a(f"- OOS elapsed: {summary['oos_elapsed_hours']} h ({round(summary['oos_elapsed_hours'] / 24, 4)} d); 14-day gate: `{summary['elapsed_14_days_met']}`")
    a(f"- Git: {json.dumps(git_meta, ensure_ascii=False)}")
    a("")
    a("## Frozen geometry and gross R formulas (verified against evaluator source)")
    a("")
    a("```text")
    a("entry        = reference_price      (variant_* geometry unused: 0 rows for both cohorts)")
    a("stop         = invalidation_price   (structural, direction-aware)")
    a("target       = target_1             (verified |target-entry| / |entry-stop| == 2.0)")
    a("risk         = |entry - stop|  (positive R denominator; 1R initial risk)")
    a("TP_FIRST     -> R = +2.0")
    a("SL_FIRST     -> R = -1.0")
    a("AMBIGUOUS intrabar collision (same candle TP+SL) under frozen STOP_FIRST -> R = -1.0")
    a("TIMEOUT 240m -> return_at_240m = (last_close_in_(signal,signal+240m] - entry)/entry*100  [raw %]")
    a("             LONG  R = +ret * entry / (100 * risk)")
    a("             SHORT R = -ret * entry / (100 * risk)   [direction normalization applied]")
    a("```")
    a("")
    a("## Snapshot fields")
    a("")
    a("```text")
    for k, v in summary["snapshot_fields"].items():
        if k == "SOURCE_COMPLETENESS":
            a(f"{k}:")
            for exp, cv in (v or {}).items():
                a(f"  {exp}: {json.dumps(cv, ensure_ascii=False, default=str)}")
        elif k == "OOS_CALENDAR_DATES":
            a(f"{k}: n={len(v or [])} -> {', '.join(v or [])}")
        else:
            a(f"{k}: {v}")
    a("```")
    a("")
    a("## Matching funnel / lineage (event identities)")
    a("")
    a("```text")
    for k, v in summary["funnel"].items():
        a(f"{k}: {v}")
    a("```")
    a("")
    a("Per direction: " + json.dumps(summary["funnel_by_direction"], ensure_ascii=False))
    a("")
    a("## Outcome integrity audit")
    a("")
    a("```text")
    for k, v in summary["outcome_audit"].items():
        a(f"{k}: {v}")
    a("```")
    a("")
    a("## Directional paired results")
    a("")
    a("| Metric | LONG | SHORT |")
    a("|---|---:|---:|")
    rows = [
        ("Matched finalized N", "matched_finalized_n"),
        ("Unique symbols", "symbols_n"),
        ("OOS calendar days", "calendar_days_n"),
        ("PB mean gross R", "point_b_mean_r"),
        ("PB median gross R", "point_b_median_r"),
        ("Baseline mean gross R", "baseline_mean_r"),
        ("Baseline median gross R", "baseline_median_r"),
        ("PB gross PF", "point_b_pf"),
        ("Baseline gross PF", "baseline_pf"),
        ("PB total gross R", "point_b_total_r"),
        ("Baseline total gross R", "baseline_total_r"),
        ("Paired delta R (mean)", "paired_delta_mean_r"),
        ("Paired delta R (median)", "paired_delta_median_r"),
        ("Delta positive frac", "delta_positive_frac"),
        ("Delta negative frac", "delta_negative_frac"),
        ("Delta zero frac", "delta_zero_frac"),
    ]
    for label, key in rows:
        a(f"| {label} | {summary['directions']['LONG'].get(key)} | {summary['directions']['SHORT'].get(key)} |")
    a(f"| Day-cluster 95% CI | {summary['directions']['LONG'].get('day_cluster', {}).get('ci95')} | {summary['directions']['SHORT'].get('day_cluster', {}).get('ci95')} |")
    a(f"| Symbol-cluster 95% CI | {summary['directions']['LONG'].get('symbol_cluster', {}).get('ci95')} | {summary['directions']['SHORT'].get('symbol_cluster', {}).get('ci95')} |")
    a(f"| Event-IID bootstrap 95% CI (secondary) | {summary['directions']['LONG'].get('event_bootstrap', {}).get('ci95')} | {summary['directions']['SHORT'].get('event_bootstrap', {}).get('ci95')} |")
    a(f"| LODO mean range | [{summary['directions']['LONG'].get('leave_one_day_out_min_r')}, {summary['directions']['LONG'].get('leave_one_day_out_max_r')}] | [{summary['directions']['SHORT'].get('leave_one_day_out_min_r')}, {summary['directions']['SHORT'].get('leave_one_day_out_max_r')}] |")
    a(f"| LODO sign flips | {summary['directions']['LONG'].get('leave_one_day_out_sign_flips')} | {summary['directions']['SHORT'].get('leave_one_day_out_sign_flips')} |")
    a(f"| LOSO sign flips | {summary['directions']['LONG'].get('leave_one_symbol_out_sign_flips')} | {summary['directions']['SHORT'].get('leave_one_symbol_out_sign_flips')} |")
    a(f"| Max single-day share of total delta | {summary['directions']['LONG'].get('max_single_day_share_of_total_delta')} | {summary['directions']['SHORT'].get('max_single_day_share_of_total_delta')} |")
    a(f"| Max single-symbol share of total delta | {summary['directions']['LONG'].get('max_single_symbol_share_of_total_delta')} | {summary['directions']['SHORT'].get('max_single_symbol_share_of_total_delta')} |")
    a(f"| Kish effective day clusters | {summary['directions']['LONG'].get('day_cluster', {}).get('kish_effective_clusters')} | {summary['directions']['SHORT'].get('day_cluster', {}).get('kish_effective_clusters')} |")
    a(f"| Verdict | **{summary['verdicts']['LONG']['verdict']}** | **{summary['verdicts']['SHORT']['verdict']}** |")
    a("")
    a("TP/SL/TIMEOUT path distributions:")
    a("")
    a("```text")
    for d in ("LONG", "SHORT"):
        s = summary["directions"][d]
        a(f"{d} Point-B exits : {s.get('point_b_exits')}")
        a(f"{d} Baseline exits: {s.get('baseline_exits')}")
    a("```")
    a("")
    a("Daily paired deltas: see `HTF_POINT_B_FINAL_PAIRED_DAILY_V1.csv`; "
      "symbol concentration: `HTF_POINT_B_FINAL_PAIRED_SYMBOL_V1.csv`; "
      "funnel rows: `HTF_POINT_B_FINAL_PAIRED_UNMATCHED_V1.csv`.")
    a("")
    a("## Maturity gates (frozen)")
    a("")
    a("```text")
    for k, v in summary["maturity_gates"].items():
        a(f"{k}: {json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, dict) else v}")
    a("```")
    a("")
    a("## Robustness record")
    a("")
    a("```text")
    a(json.dumps(summary["bootstrap"], ensure_ascii=False))
    a("boundary sensitivity LONG : " + json.dumps(summary["directions"]["LONG"].get("boundary_sensitivity"), ensure_ascii=False))
    a("boundary sensitivity SHORT: " + json.dumps(summary["directions"]["SHORT"].get("boundary_sensitivity"), ensure_ascii=False))
    a("```")
    a("")
    a("## Selection bias / estimand")
    a("")
    a(f"- Estimand: {summary['selection_bias']['estimand']}")
    a(f"- Conditions on: {summary['selection_bias']['conditions_on']}")
    a("- Does NOT establish:")
    for x in summary["selection_bias"]["does_not_establish"]:
        a(f"  - {x}")
    a(f"- Imputation policy: {summary['selection_bias']['imputation_policy']}")
    a("")
    a("## Verdicts")
    a("")
    a(f"- LONG: `{summary['verdicts']['LONG']['verdict']}` - {summary['verdicts']['LONG']['rationale']}")
    a(f"- SHORT: `{summary['verdicts']['SHORT']['verdict']}` - {summary['verdicts']['SHORT']['rationale']}")
    a(f"- OVERALL_INCREMENTAL_POINT_B_HYPOTHESIS: `{summary['verdicts']['OVERALL_INCREMENTAL_POINT_B_HYPOTHESIS']['verdict']}` "
      f"({summary['verdicts']['OVERALL_INCREMENTAL_POINT_B_HYPOTHESIS']['per_direction']})")
    a("")
    a("All conclusions relate to **frozen gross research geometry** only; executable/net economics "
      "were not part of this protocol. No production data, OOS outcomes, freeze boundary, scanner, "
      "paper or live behaviour was modified.")
    a("")
    a("## Limitations")
    a("")
    for x in summary["limitations"]:
        a(f"- {x}")
    a("")
    a("## Artifact SHA-256")
    a("")
    a("```text")
    for name, h in sorted(artifacts.items()):
        a(f"{h}  {name}")
    a("```")
    a("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("capture", aliases=["snapshot"],
                        help="READ-ONLY DB capture into isolated output dir")
    sp.add_argument("--worktree", type=Path, default=Path("/opt/trad_bot_issue169"))
    sp.add_argument("--config-root", type=Path, default=Path("/opt/trad_bot"))
    sp.add_argument("--out", type=Path, required=True)
    sp.add_argument("--allow-nonempty-dir", action="store_true",
                    help="explicit approval to capture into a non-empty output dir "
                         "(snapshot files are still never overwritten)")

    rp = sub.add_parser("report", help="offline deterministic analysis from snapshot files")
    rp.add_argument("--out", type=Path, required=True)
    rp.add_argument("--reps", type=int, default=DEFAULT_REPS)
    rp.add_argument("--seed", type=int, default=DEFAULT_SEED)
    rp.add_argument("--git-research-head", default="")
    rp.add_argument("--git-production-head", default="")
    rp.add_argument("--allow-artifact-overwrite", action="store_true",
                    help="explicit approval to overwrite existing canonical artifacts")

    opts = ap.parse_args(argv)
    if opts.cmd in ("capture", "snapshot"):
        meta = run_capture(opts.worktree, opts.config_root, opts.out,
                           allow_nonempty=opts.allow_nonempty_dir)
        print(json.dumps({k: meta.get(k) for k in (
            "SNAPSHOT_ASOF_UTC", "POST_FREEZE_ELIGIBLE_N", "FINALIZED_N", "IMMATURE_N",
            "SYMBOLS_N", "OOS_ELAPSED_HOURS")}, indent=2, default=str))
        return 0

    rows = load_rows(opts.out)
    meta = json.loads((opts.out / SNAPSHOT_META_FILE).read_text(encoding="utf-8"))
    if opts.reps < 100:
        ap.error("--reps must be >= 100")
    summary, details = analyze(rows, meta, opts.reps, opts.seed)
    git_meta = {"research_head": opts.git_research_head, "production_head": opts.git_production_head}
    artifacts = write_artifacts(opts.out, summary, details, meta, git_meta,
                                allow_overwrite=opts.allow_artifact_overwrite)
    print(json.dumps({
        "asof": summary["asof_utc"],
        "directions": {d: {"n": summary["directions"][d].get("matched_finalized_n"),
                           "delta_mean_r": summary["directions"][d].get("paired_delta_mean_r"),
                           "verdict": summary["verdicts"][d]["verdict"]}
                       for d in ("LONG", "SHORT")},
        "overall": summary["verdicts"]["OVERALL_INCREMENTAL_POINT_B_HYPOTHESIS"],
        "artifacts": artifacts,
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
