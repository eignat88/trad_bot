"""Pre-activation hardening contracts (Issue #259 — F1 / F3 / F4).

Static, offline contract tests (no PostgreSQL, no network, no orders):

* Migration 083 — the coverage ledger is purely additive, its disposition
  enum stays in sync with the module, and no existing object is touched.
* F4 forward protocol — doc §12 is a strict superset of the frozen
  registry ``validation_criteria`` and explicitly forbids deriving
  executable Net R from the frozen Net R.
* Runbook — every runtime flag is documented with its safe configuration.
"""
import json
import re
from pathlib import Path

import app.research.srr_short_fill_feasibility_v1 as mod

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "sql" / "migrations" / "083_srr_short_fill_coverage_v1.sql"
DOC = (ROOT / "docs" / "research" /
       "SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1.md")
RUNBOOK = (ROOT / "docs" / "research" /
           "SRR_SHORT_FILL_ACTIVATION_RUNBOOK_V1.md")
REGISTRY = ROOT / "app" / "research" / "prospective_registry.json"


def migration_sql() -> str:
    assert MIGRATION.exists(), "migration 083 missing"
    return MIGRATION.read_text(encoding="utf-8")


def code_body() -> str:
    """Migration text without ``--`` comment lines."""
    return "\n".join(line for line in migration_sql().splitlines()
                     if not line.strip().startswith("--"))


def table_body() -> str:
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS research\.srr_short_fill_coverage_v1 "
        r"\((.*?)\n\);",
        migration_sql(), re.S)
    assert match is not None, "coverage table definition missing"
    return match.group(1)


def doc_norm() -> str:
    """Doc text with normalized whitespace (immune to line reflow)."""
    return " ".join(DOC.read_text(encoding="utf-8").split())


def registry_entry() -> dict:
    """The frozen source prospective-validation entry the contour inherits.

    The contour itself is intentionally NOT in any registry (it is a
    research sub-question); its frozen criteria live on the source
    experiment that owns the prospective observations.
    """
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if (node.get("experiment_id") == mod.SOURCE_EXPERIMENT_ID
                    and "validation_criteria" in node):
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    raise AssertionError("frozen source validation entry not found")


# ── migration 083 (F3 coverage ledger) ──────────────────────────────────


def test_migration_083_is_next_and_unique():
    numbers = sorted(int(p.name.split("_")[0]) for p in
                     (ROOT / "sql" / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
    assert numbers.count(83) == 1
    # 084-086 belong to the inverse-short follow-up PRs, 087 to the
    # ME_LONG_V2_INVERSE_SHORT_V2 stale-entry-guard PR, 088 to the
    # ME_LONG_V2_INVERSE_SHORT_OOS2_V1 ladder contour (Issue #276) and 089
    # to its round-6 additive funding-obligation table (Issue #276,
    # Codex r4555591322); no number is ever reused and the branch's newest
    # migration carries the highest number.
    assert numbers[-1] == 89
    assert MIGRATION.name == "083_srr_short_fill_coverage_v1.sql"


def test_migration_083_is_transactional_and_additive_only():
    sql = migration_sql()
    assert sql.lstrip().startswith("--")   # header comment block
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "CREATE SCHEMA IF NOT EXISTS research;" in sql
    assert sql.count("CREATE TABLE IF NOT EXISTS") == 1
    assert sql.count("CREATE INDEX IF NOT EXISTS") == 2   # audit indexes
    body = code_body().lower()
    # Nothing existing may be altered, dropped or truncated — migration 082
    # objects and prospective_* sources stay byte-identical.
    assert "alter table" not in body
    assert "drop " not in body
    assert "truncate" not in body
    assert "delete from" not in body
    assert "update research." not in body
    assert "insert into research.prospective" not in body
    assert "insert into research.srr_short_fill_feasibility" not in body
    assert "insert into research.srr_short_fill_activation" not in body


def test_coverage_table_identity_matches_the_module():
    body = table_body()
    assert re.search(r"observation_id bigint PRIMARY KEY", body)
    assert ("REFERENCES research.prospective_observation(observation_id)"
            in body)   # FK to the independent prospective source
    assert (f"CHECK (experiment_id='{mod.EXPERIMENT_ID}')" in body)
    assert "recorded_at timestamptz NOT NULL DEFAULT NOW()" in body

    disposition = re.search(
        r"disposition text NOT NULL\s+CHECK \(disposition IN\s*\(([^)]*)\)\)",
        body, re.S)
    assert disposition is not None
    migration_dispositions = set(
        re.findall(r"'([A-Z_]+)'", disposition.group(1)))
    assert migration_dispositions == set(mod.COVERAGE_DISPOSITIONS)

    # The module writes exactly this table with first-write-wins semantics.
    module_sql = (ROOT / "app" / "research" /
                  "srr_short_fill_feasibility_v1.py").read_text(
        encoding="utf-8")
    assert re.search(
        r"INSERT INTO research\.srr_short_fill_coverage_v1\s*\((.*?)\)\s*"
        r"VALUES\s*(\(.*?\))",
        module_sql, re.S) is not None
    assert "ON CONFLICT (observation_id) DO NOTHING" in module_sql


def test_migration_083_has_no_trading_surface():
    body = code_body().lower()
    for forbidden in ("paper", "live", "order_id", "insert into public",
                      "update public", "dds.", "scanner_setup",
                      "prospective_outcome", "bybit", "http", "websocket"):
        assert forbidden not in body, f"unexpected surface: {forbidden}"


# ── F4: forward protocol vs frozen registry ─────────────────────────────


def test_f4_is_a_strict_superset_of_frozen_validation_criteria():
    text = doc_norm()
    entry = registry_entry()
    criteria = entry["validation_criteria"]
    for criterion in criteria["normal_cost_requires"]:
        assert criterion in text, f"frozen criterion missing from §12: {criterion}"
    assert criteria["elevated_cost_treatment"] in text
    assert criteria["before_sample_gates"] in text
    assert entry["minimum_n"] == 300
    assert f"minimum_n = {entry['minimum_n']}" in text
    assert f"minimum_symbols = {entry['minimum_symbols']}" in text
    assert f"minimum_oos_days = {entry['minimum_oos_days']}" in text


def test_f4_requires_stage2_recomputation_and_forbids_the_shortcut():
    text = doc_norm()
    forbidden = ("FORBIDDEN: computing executable Net R by subtracting "
                 "extra costs from the frozen Net R")
    # Stated twice on purpose (§12.2 and §12.6).
    assert text.count(forbidden) >= 2
    assert "execution-aligned trajectory recomputation" in text
    assert "+0.064721R" in text       # frozen result stays untouched
    # Mandatory elevated-cost sensitivity (frozen treatment).
    assert "execution fragility" in text
    assert "are not represented as cost robustness" in text


def test_f4_decision_rule_covers_every_required_dimension():
    text = doc_norm()
    # verdicts
    assert "### 12.3 GO" in DOC.read_text(encoding="utf-8")
    assert "### 12.4 NO-GO" in DOC.read_text(encoding="utf-8")
    assert "### 12.5 INCONCLUSIVE" in DOC.read_text(encoding="utf-8")
    # statistical uncertainty: both cluster bootstraps, fixed effort
    assert "day-cluster bootstrap 95% CI lower bound > 0" in text
    assert "symbol-cluster bootstrap 95% CI lower bound > 0" in text
    assert "10,000 resamples" in text
    # concentration
    assert "leave-one-day-out" in text and "leave-one-symbol-out" in text
    assert "50% of gross positive PnL" in text
    # coverage and quote quality
    assert "coverage ≥ 70%" in text
    assert "eligible share ≥ 60%" in text
    assert "API-budget denials < 5%" in text
    assert "latency-budget denials < 5%" in text
    assert "unaccounted" in text
    # PENDING never inflates anything
    assert "PENDING` rows never count as captures" in text
    # sample floors with the frozen floors stated
    assert "n ≥ `minimum_n = 300`" in text
    assert "distinct symbols ≥ `minimum_symbols = 30`" in text
    assert "OOS days ≥ `minimum_oos_days = 14`" in text


def test_runbook_documents_every_runtime_flag_and_the_status_line():
    runbook = RUNBOOK.read_text(encoding="utf-8")
    for env in (mod.ENABLE_FLAG, mod.QUOTE_DELAY_LIMIT_ENV,
                mod.MAX_PER_CYCLE_ENV, mod.MAX_PER_RUN_ENV,
                mod.CYCLE_LATENCY_ENV, mod.CAPTURE_LATENCY_ENV,
                mod.EXPECTED_ACTIVATION_ENV):
        assert f"`{env}`" in runbook, f"runbook misses {env}"
    assert ("MERGED_BASELINE / HARDENING_PENDING / CAPTURE_DISABLED / "
            "PAPER_LIVE_NO_GO") in runbook
    assert "Bybit calls: +0" in runbook
    assert "083_srr_short_fill_coverage_v1.sql" in runbook


def test_f1_defaults_are_safe_and_documented():
    # Positive windows, capture fits inside the cycle, hard caps bounded.
    assert 0 < mod.DEFAULT_MAX_CAPTURE_MS <= mod.DEFAULT_MAX_CYCLE_LATENCY_MS
    assert mod.HARD_MAX_CAPTURE_MS <= mod.HARD_MAX_CYCLE_LATENCY_MS <= 60000
    text = doc_norm()
    assert mod.CYCLE_LATENCY_ENV in text
    assert mod.CAPTURE_LATENCY_ENV in text
    assert "latency:TIMEOUT" in text
    assert "worker" in text
    # The cycle charge covers the FULL capture incl. DB finalization.
    assert "and the DB finalization" in text


# ── Codex review follow-ups (fix commit) ────────────────────────────────


def test_coverage_sql_never_counts_pending_in_numerator():
    """F3: PENDING is excluded from EVERY coverage numerator (docs)."""
    text = doc_norm()
    # The old unfiltered numerator is gone for good.
    assert "COUNT(e.observation_id) AS captured," not in text
    # captured_finalized and coverage_pct filter PENDING out explicitly...
    assert text.count(
        "FILTER (WHERE e.feasibility_status <> 'PENDING')") >= 2
    # ...and the eligible numerator states the exclusion too.
    assert ("FILTER (WHERE eligible_for_execution_stats AND "
            "feasibility_status <> 'PENDING') AS eligible") in text
    # PENDING always has its own bucket/column.
    assert "AS pending_separate" in text
    assert "AS stuck_pending" in text
    assert "THEN 'PENDING'" in text
    assert "PENDING is NEVER part of any numerator" in text
    assert ("PENDING is its own bucket and is never merged into ELIGIBLE"
            in text)
    # The runbook carries no numerator of its own that could count PENDING.
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "COUNT(e.observation_id)" not in runbook
    assert "PENDING" in runbook


def test_f4_verdicts_are_mutually_exclusive():
    """Codex F4: straddling CI => INCONCLUSIVE, never NO-GO; one verdict."""
    text = doc_norm()
    raw = DOC.read_text(encoding="utf-8")
    # Verdict precedence contract, verbatim from the doc.
    assert ("mutually exclusive, decided in order, first match wins"
            in text)
    assert "§12.5 INCONCLUSIVE is evaluated FIRST" in text
    assert "an uncertain result is INCONCLUSIVE, never NO-GO" in text
    assert ("§12.4 NO-GO is evaluated only when §12.5 did not match"
            in text)
    assert ("§12.3 GO is evaluated only when neither §12.5 nor §12.4 "
            "matched") in text
    assert "A given sample therefore has exactly one verdict" in text
    # NO-GO is gated behind §12.5 and its CI condition excludes straddles.
    assert "Applies only when §12.5 did not match" in text
    assert "can never reach this branch" in text
    assert "Evaluated FIRST — before §12.4 (NO-GO) and §12.3 (GO)" in text
    # The old ambiguous condition (straddle also matching NO-GO) is gone.
    assert "95% CI lower bound ≤ 0" not in text
    for heading in ("### 12.3 GO", "### 12.4 NO-GO", "### 12.5 INCONCLUSIVE"):
        assert heading in raw

    # Scenario matrix: the documented precedence, encoded literally —
    # every input state maps to EXACTLY ONE verdict.
    def classify(ci_lower, ci_upper, *, complete=True, floors_ok=True,
                 criteria_ok=True):
        # 1) INCONCLUSIVE first: incomplete evidence or straddling CI.
        if not complete or not floors_ok or ci_lower <= 0 < ci_upper:
            return "INCONCLUSIVE"
        # 2) NO-GO only with complete, statistically resolved evidence.
        if ci_upper <= 0 or not criteria_ok:
            return "NO-GO"
        # 3) GO only when neither branch above matched.
        return "GO"

    assert classify(-0.10, 0.30) == "INCONCLUSIVE"  # straddle, point <= 0
    assert classify(-0.05, 0.02) == "INCONCLUSIVE"  # straddle around 0
    assert classify(-0.40, -0.10) == "NO-GO"        # CI entirely <= 0
    assert classify(0.10, 0.40) == "GO"
    assert classify(0.10, 0.40, floors_ok=False) == "INCONCLUSIVE"
    assert classify(0.10, 0.40, complete=False) == "INCONCLUSIVE"
    assert classify(0.10, 0.40, criteria_ok=False) == "NO-GO"
    # A straddling CI with a negative point estimate can never be NO-GO;
    # only a CI fully at/below zero reaches the NO-GO branch.
    assert classify(-0.30, -0.01) == "NO-GO"   # upper <= 0: resolved negative
    assert classify(-0.30, 0.01) == "INCONCLUSIVE"  # straddle wins
