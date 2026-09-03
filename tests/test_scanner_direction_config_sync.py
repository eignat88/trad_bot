import pytest

from app.scanners.direction_gate import (
    GATE_BLOCKED,
    GATE_ENABLED,
    GATE_REGIME,
    ScannerDirectionGate,
    ScannerDirectionGatePolicy,
)


def _policy(*gates):
    return ScannerDirectionGatePolicy(
        {(gate.scanner_name, gate.direction): gate for gate in gates}, {}
    )


def test_enabled_gate_allows_candidate():
    decision = _policy(
        ScannerDirectionGate("ACTIVE", "LONG", GATE_ENABLED)
    ).evaluate("ACTIVE", "LONG", "RANGE")
    assert decision.allowed is True
    assert decision.status == GATE_ENABLED


def test_blocked_gate_rejects_candidate():
    decision = _policy(
        ScannerDirectionGate("ACTIVE", "SHORT", GATE_BLOCKED, reason="validation")
    ).evaluate("ACTIVE", "SHORT", "RANGE")
    assert decision.allowed is False
    assert decision.reason_code == "DIRECTION_GATE_BLOCKED"


@pytest.mark.parametrize("regime, allowed", [("TREND_UP", True), ("RANGE", False), ("TREND_DOWN", False)])
def test_regime_gate_checks_market_regime(regime, allowed):
    decision = _policy(
        ScannerDirectionGate("TREND_PULLBACK_V2", "LONG", GATE_REGIME, ("TREND_UP",))
    ).evaluate("TREND_PULLBACK_V2", "LONG", regime)
    assert decision.allowed is allowed
    assert decision.status == GATE_REGIME


def test_static_fallback_preserves_current_blocklist_and_regime_policy():
    policy = ScannerDirectionGatePolicy.static_fallback(
        ["TREND_PULLBACK_V2", "BREAKOUT_RETEST"],
        [("BREAKOUT_RETEST", "LONG")],
        {"TREND_PULLBACK_V2": {"LONG": ("TREND_UP",)}},
    )
    assert not policy.evaluate("BREAKOUT_RETEST", "LONG", "TREND_UP").allowed
    assert policy.evaluate("TREND_PULLBACK_V2", "LONG", "TREND_UP").allowed
    assert not policy.evaluate("TREND_PULLBACK_V2", "LONG", "RANGE").allowed


def test_database_failure_uses_static_fallback():
    class FailingRepository:
        def get_scanner_direction_gates(self):
            raise RuntimeError("database unavailable")

    policy = ScannerDirectionGatePolicy.load_for_cycle(
        FailingRepository(),
        scanner_names=["ACTIVE"],
        blocked_combinations=[("ACTIVE", "SHORT")],
        regime_whitelist={},
    )
    assert policy.evaluate("ACTIVE", "LONG", "RANGE").allowed
    assert not policy.evaluate("ACTIVE", "SHORT", "RANGE").allowed


def test_missing_gate_is_fail_closed():
    policy = ScannerDirectionGatePolicy.static_fallback([], [], {})
    decision = policy.evaluate("NEW_SCANNER", "LONG", "TREND_UP")
    assert not decision.allowed
    assert decision.reason_code == "DIRECTION_GATE_UNKNOWN"


def test_runtime_snapshot_is_loaded_once_per_cycle():
    class Repository:
        calls = 0

        def get_scanner_direction_gates(self):
            self.calls += 1
            return [ScannerDirectionGate("ACTIVE", "LONG", GATE_ENABLED)]

    repository = Repository()
    policy = ScannerDirectionGatePolicy.load_for_cycle(
        repository, scanner_names=["ACTIVE"], blocked_combinations=[], regime_whitelist={}
    )
    for _ in range(20):
        assert policy.evaluate("ACTIVE", "LONG", "RANGE").allowed
    assert repository.calls == 1


def test_unknown_status_never_fails_open():
    decision = _policy(
        ScannerDirectionGate("ACTIVE", "LONG", "TYPO")
    ).evaluate("ACTIVE", "LONG", "RANGE")
    assert not decision.allowed
    assert decision.reason_code == "DIRECTION_GATE_UNKNOWN"
