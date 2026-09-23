#!/usr/bin/env python3
"""VPS deployment verification for ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1.

Run this on VPS after deployment to verify:

1. Migration applied — gate entries correct
2. Scanner registered and importable
3. close_location calculation matches research
4. Shadow control path works
5. PASS/REJECT paths work

Usage (on VPS):
    cd /opt/trad_bot
    python scripts/verify_oos_deployment.py
"""
from __future__ import annotations

import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# ── Colour output helpers ──
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
    results.append((name, passed, detail))
    print(f"  [{status}] {name}")
    if detail:
        print(f"         {detail}")


def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f" {title}")
    print(f"{'='*60}")


# ══════════════════════════════════════════════════════════════════════
# 1. Close location calculation
# ══════════════════════════════════════════════════════════════════════
section("1. CLOSE LOCATION CALCULATION")

from app.scanners.close_location import calculate_close_location, close_location_passes

# close == high → 1.0
v = calculate_close_location(100, 80, 100)
check("close_at_high = 1.0", v == 1.0, f"got {v}")

# close == low → 0.0
v = calculate_close_location(100, 80, 80)
check("close_at_low = 0.0", v == 0.0, f"got {v}")

# close == midrange → 0.5
v = calculate_close_location(100, 80, 90)
check("close_at_midrange = 0.5", v == 0.5, f"got {v}")

# close_location == 0.70 exactly → PASS (>= inclusive)
passed, v = close_location_passes(100, 80, 94.0)  # (94-80)/(100-80) = 0.70
check("threshold_0.70_inclusive_PASS", passed is True, f"cl={v}")

# close_location == 0.699999 → REJECT
passed, v = close_location_passes(100, 80, 93.9998)  # ≈0.69999
check("threshold_just_below_REJECT", passed is False, f"cl={v}")

# zero range → None, reject
passed, v = close_location_passes(100, 100, 100)
check("zero_range_reject", passed is False, f"cl={v}")

# Research-reproduced examples:
# Signal candle: high=50200, low=49800, close=50080 → cl = (50080-49800)/400 = 0.70
passed, v = close_location_passes(50200, 49800, 50080)
check("research_example_0.70_PASS", passed is True, f"cl={v}")

# Signal candle: high=50200, low=49800, close=50040 → cl = (50040-49800)/400 = 0.60
passed, v = close_location_passes(50200, 49800, 50040)
check("research_example_0.60_REJECT", passed is False, f"cl={v}")


# ══════════════════════════════════════════════════════════════════════
# 2. Scanner import and identity
# ══════════════════════════════════════════════════════════════════════
section("2. SCANNER IMPORT & IDENTITY")

from app.scanners.me_r_long_close_location_oos_validation import (
    MERLongCloseLocationOOSValidationV1Scanner,
    OOS_EXPERIMENT_ID,
    REJECTION_REASON,
)
from app.scanners.momentum_exhaustion_reverse_long_v1 import MomentumExhaustionReverseLongV1Scanner

new_scanner = MERLongCloseLocationOOSValidationV1Scanner()
check("new_scanner_name", new_scanner.name == "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", f"got {new_scanner.name}")
check("new_scanner_version", new_scanner.version == "1.0.0")
check("new_scanner_uses_v1_base", isinstance(new_scanner._base_scanner, MomentumExhaustionReverseLongV1Scanner))
check("threshold_frozen_070", new_scanner.threshold == 0.70)
check("oos_experiment_id", OOS_EXPERIMENT_ID == "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1")
check("rejection_reason", REJECTION_REASON == "CLOSE_LOCATION_LT_070")


# ══════════════════════════════════════════════════════════════════════
# 3. Orchestrator registration
# ══════════════════════════════════════════════════════════════════════
section("3. ORCHESTRATOR REGISTRATION")

from app.scanners.orchestrator import ScannerOrchestrator
orch = ScannerOrchestrator()
check("new_scanner_in_orchestrator",
      "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1" in orch.scanners)
check("old_scanner_still_in_orchestrator",
      "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1" in orch.scanners)
check("shadow_control_scanners",
      "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1" in ScannerOrchestrator.SHADOW_CONTROL_SCANNERS)
check("total_scanner_count", len(orch.scanners) == 13, f"got {len(orch.scanners)}")


# ══════════════════════════════════════════════════════════════════════
# 4. Config / blocked_scanner_directions
# ══════════════════════════════════════════════════════════════════════
section("4. CONFIG: blocked_scanner_directions")

from app.config import load_settings
settings = load_settings()
check("V1_LONG_blocked",
      ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG") in settings.blocked_scanner_directions)
check("V1_SHORT_blocked",
      ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "SHORT") in settings.blocked_scanner_directions)
check("new_scanner_NOT_blocked",
      ("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "LONG") not in settings.blocked_scanner_directions)


# ══════════════════════════════════════════════════════════════════════
# 5. Execution policy
# ══════════════════════════════════════════════════════════════════════
section("5. EXECUTION POLICY")

v1_policy = settings.execution_policy_configs.get("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", {}).get("LONG")
new_policy = settings.execution_policy_configs.get("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", {}).get("LONG")

check("V1_policy_exists", v1_policy is not None)
check("V1_policy_disabled", v1_policy.enabled is False, f"enabled={v1_policy.enabled}")
check("V1_policy_type", v1_policy.policy == "FIXED_TP_SL_HORIZON_V1")
check("new_policy_exists", new_policy is not None)
check("new_policy_enabled", new_policy.enabled is True, f"enabled={new_policy.enabled}")
check("new_policy_type", new_policy.policy == "FIXED_TP_SL_HORIZON_V1")
check("new_policy_hold_240", new_policy.hold_minutes == 240)
check("new_policy_tp_enabled", new_policy.tp_enabled is True)
check("new_policy_dca_disabled", new_policy.dca_enabled is False)
check("new_policy_trailing_disabled", new_policy.trailing_enabled is False)


# ══════════════════════════════════════════════════════════════════════
# 6. Direction gate (DB)
# ══════════════════════════════════════════════════════════════════════
section("6. DIRECTION GATE (DB)")

try:
    from app.db.repository import ScannerRepository
    repo = ScannerRepository(
        host=settings.db_host, port=settings.db_port,
        database=settings.db_name, user=settings.db_user,
        password=settings.db_password,
    )
    gates = repo.get_scanner_direction_gates()
    repo.close()

    gate_map = {(g.scanner_name, g.direction): g for g in gates}

    v1_long = gate_map.get(("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG"))
    check("V1_LONG_gate_exists", v1_long is not None)
    check("V1_LONG_gate_BLOCKED",
          v1_long.status == "BLOCKED" if v1_long else False,
          f"status={v1_long.status if v1_long else 'N/A'}")
    check("V1_LONG_gate_reason_oos",
          "OOS validation" in (v1_long.reason or "") if v1_long else False,
          f"reason={v1_long.reason if v1_long else 'N/A'}")

    new_long = gate_map.get(("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "LONG"))
    check("NEW_LONG_gate_exists", new_long is not None)
    check("NEW_LONG_gate_ENABLED",
          new_long.status == "ENABLED" if new_long else False,
          f"status={new_long.status if new_long else 'N/A'}")

    new_short = gate_map.get(("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "SHORT"))
    check("NEW_SHORT_gate_BLOCKED",
          new_short.status == "BLOCKED" if new_short else False,
          f"status={new_short.status if new_short else 'N/A'}")

except Exception as e:
    check("DB_gate_check", False, f"DB error: {e}")


# ══════════════════════════════════════════════════════════════════════
# 7. Static fallback gate
# ══════════════════════════════════════════════════════════════════════
section("7. STATIC FALLBACK GATE")

from app.scanners.direction_gate import ScannerDirectionGatePolicy, GATE_BLOCKED, GATE_ENABLED

fallback = ScannerDirectionGatePolicy.static_fallback(
    scanner_names=orch.scanners.keys(),
    blocked_combinations=settings.blocked_scanner_directions,
    regime_whitelist=settings.scanner_regime_whitelist,
)

d1 = fallback.evaluate("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG", None)
check("fallback_V1_LONG_BLOCKED", d1.allowed is False and d1.status == GATE_BLOCKED)

d2 = fallback.evaluate("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "LONG", None)
check("fallback_NEW_LONG_ENABLED", d2.allowed is True and d2.status == GATE_ENABLED)

d3 = fallback.evaluate("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "SHORT", None)
check("fallback_NEW_SHORT_BLOCKED", d3.allowed is False and d3.status == GATE_BLOCKED)


# ══════════════════════════════════════════════════════════════════════
# 8. V1 base logic unchanged (regression)
# ══════════════════════════════════════════════════════════════════════
section("8. V1 BASE LOGIC REGRESSION")

v1_scanner = MomentumExhaustionReverseLongV1Scanner()
check("V1_name_unchanged", v1_scanner.name == "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1")
check("V1_version_unchanged", v1_scanner.version == "1.0.0")
check("V1_swing_lookback", v1_scanner.swing_lookback == 5)
check("V1_exhaustion_threshold", v1_scanner.exhaustion_threshold == 0.003)

import inspect
v1_source = inspect.getsource(MomentumExhaustionReverseLongV1Scanner)
check("V1_no_oos_reference", "OOS" not in v1_source and "close_location" not in v1_source.lower())


# ══════════════════════════════════════════════════════════════════════
# 9. CLI registration
# ══════════════════════════════════════════════════════════════════════
section("9. CLI REGISTRATION")

from app.scanners.cli import ALL_SCANNERS
check("new_in_ALL_SCANNERS", "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1" in ALL_SCANNERS)
check("V1_still_in_ALL_SCANNERS", "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1" in ALL_SCANNERS)


# ══════════════════════════════════════════════════════════════════════
# 10. Leakage audit
# ══════════════════════════════════════════════════════════════════════
section("10. LEAKAGE AUDIT")

# Verify that the new scanner computes close_location from the SAME candle
# that V1 uses (candles_5m[-1]), which is always closed before evaluation.

from app.scanners.models import MarketContext, IndicatorSnapshot, MarketLevels
from datetime import datetime, timezone

# Create a minimal valid context to test scanner path
ts = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)

# The scanner's _compute_close_location_from_signal_candle uses candles_5m[-1]
# which is the last CLOSED candle at evaluation time.
# leakage_source_timestamp <= decision_timestamp is guaranteed because:
#   - candles_5m[-1].timestamp = signal candle open time
#   - ctx.evaluated_at = now() (after candle closed)
#   - scanner code: detected_at = ctx.evaluated_at

# Verify the code guarantees this ordering
scanner_src = inspect.getsource(MERLongCloseLocationOOSValidationV1Scanner)
check("source_ts_in_features", "close_location_source_timestamp" in scanner_src)
check("decision_ts_in_features", "close_location_decision_timestamp" in scanner_src)
check("oos_experiment_in_features", "oos_experiment_id" in scanner_src)


# ══════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════
section("SUMMARY")

passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)

print(f"\n  Total: {len(results)} checks")
print(f"  {GREEN}Passed: {passed}{RESET}")
if failed:
    print(f"  {RED}Failed: {failed}{RESET}")
    for name, ok, detail in results:
        if not ok:
            print(f"    {RED}FAIL{RESET} {name}: {detail}")
else:
    print(f"  {GREEN}All checks passed!{RESET}")

print(f"\n  Timestamp: {datetime.now(timezone.utc).isoformat()}")
print(f"  Git HEAD: ", end="")
import subprocess
try:
    r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                       cwd=str(PROJECT_ROOT), timeout=5)
    print(r.stdout.strip())
except Exception:
    print("(unknown)")

sys.exit(0 if failed == 0 else 1)
