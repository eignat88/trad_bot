"""Runtime scanner/direction trading gate policy.

The database rows are loaded once at the start of each scan/entry cycle.  The
static Settings values remain a fail-safe fallback and unknown combinations are
never allowed implicitly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Mapping

logger = logging.getLogger(__name__)

GATE_ENABLED = "ENABLED"
GATE_BLOCKED = "BLOCKED"
GATE_REGIME = "REGIME"
_VALID_STATUSES = frozenset({GATE_ENABLED, GATE_BLOCKED, GATE_REGIME})


@dataclass(frozen=True)
class ScannerDirectionGate:
    scanner_name: str
    direction: str
    status: str
    allowed_regimes: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    status: str
    reason_code: str | None = None
    reason: str | None = None
    allowed_regimes: tuple[str, ...] = ()


class ScannerDirectionGatePolicy:
    """In-memory policy snapshot; callers must refresh it per scan cycle."""

    def __init__(
        self,
        gates: Mapping[tuple[str, str], ScannerDirectionGate],
        fallback_gates: Mapping[tuple[str, str], ScannerDirectionGate],
    ) -> None:
        self._gates = dict(gates)
        self._fallback_gates = dict(fallback_gates)

    @classmethod
    def static_fallback(
        cls,
        scanner_names: Iterable[str],
        blocked_combinations: Iterable[tuple[str, str]],
        regime_whitelist: Mapping[str, Mapping[str, Iterable[str]]],
    ) -> "ScannerDirectionGatePolicy":
        blocked = {(scanner.upper(), direction.upper()) for scanner, direction in blocked_combinations}
        whitelist = {
            scanner.upper(): {
                direction.upper(): tuple(regime.upper() for regime in regimes)
                for direction, regimes in directions.items()
            }
            for scanner, directions in regime_whitelist.items()
        }
        gates: dict[tuple[str, str], ScannerDirectionGate] = {}
        for scanner_name in scanner_names:
            scanner_name = scanner_name.upper()
            for direction in ("LONG", "SHORT"):
                key = (scanner_name, direction)
                if key in blocked:
                    gates[key] = ScannerDirectionGate(*key, GATE_BLOCKED, reason="static fallback blocklist")
                elif direction in whitelist.get(scanner_name, {}):
                    gates[key] = ScannerDirectionGate(
                        *key, GATE_REGIME,
                        allowed_regimes=whitelist[scanner_name][direction],
                        reason="static fallback regime policy",
                    )
                else:
                    gates[key] = ScannerDirectionGate(*key, GATE_ENABLED, reason="static fallback enabled")
        return cls({}, gates)

    @classmethod
    def load_for_cycle(
        cls,
        repository: object,
        *,
        scanner_names: Iterable[str],
        blocked_combinations: Iterable[tuple[str, str]],
        regime_whitelist: Mapping[str, Mapping[str, Iterable[str]]],
    ) -> "ScannerDirectionGatePolicy":
        fallback = cls.static_fallback(scanner_names, blocked_combinations, regime_whitelist)
        try:
            loaded = repository.get_scanner_direction_gates()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("scanner direction gates unavailable; using static safety fallback")
            return fallback
        return cls(
            {(gate.scanner_name.upper(), gate.direction.upper()): gate for gate in loaded},
            fallback._fallback_gates,
        )

    def evaluate(self, scanner_name: str, direction: str, market_regime: str | None) -> GateDecision:
        key = (scanner_name.upper(), direction.upper())
        gate = self._gates.get(key) or self._fallback_gates.get(key)
        if gate is None:
            logger.warning("scanner direction has no runtime gate configuration: %s %s", *key)
            return GateDecision(False, "UNKNOWN", "DIRECTION_GATE_UNKNOWN", "missing gate configuration")
        status = gate.status.upper()
        if status == GATE_ENABLED:
            return GateDecision(True, status, reason=gate.reason, allowed_regimes=gate.allowed_regimes)
        if status == GATE_BLOCKED:
            return GateDecision(False, status, "DIRECTION_GATE_BLOCKED", gate.reason, gate.allowed_regimes)
        if status == GATE_REGIME:
            regime = (market_regime or "").upper()
            if regime in gate.allowed_regimes:
                return GateDecision(True, status, reason=gate.reason, allowed_regimes=gate.allowed_regimes)
            return GateDecision(False, status, "DIRECTION_GATE_REGIME", gate.reason, gate.allowed_regimes)
        logger.error("unknown scanner direction gate status=%s for %s %s; blocking", status, *key)
        return GateDecision(False, status, "DIRECTION_GATE_UNKNOWN", gate.reason, gate.allowed_regimes)
