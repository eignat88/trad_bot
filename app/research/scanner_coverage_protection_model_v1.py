"""Issue #304: pure offline request-role model; NEVER used for live admission.

The model deliberately does not configure Bybit, alter a limiter or assign a
new runtime retry_priority. It only classifies fully evidenced request roles
and simulates abstract budget slots for comparing candidate policies.

No scheduler, even with preferential ordering, can promise both protective
continuity and 50/50 Scanner coverage when total demand exceeds capacity.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping


class RequestRole(str, Enum):
    PROTECTIVE = "PROTECTIVE"
    SCANNER = "SCANNER"
    ENTRY_ONLY = "ENTRY_ONLY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RequestEvidence:
    service: str
    endpoint: str
    symbol: str = ""
    # The endpoint alone cannot prove intent: tickers cover BOTH entry and
    # open-position symbols and should be treated as protective when mixed.
    open_position_symbols: frozenset[str] = frozenset()
    entry_only_symbols: frozenset[str] = frozenset()
    # A tickers snapshot without a symbol is a mixed, whole-market request.
    # Claiming entry-only on the basis of its response would be unsafe.
    whole_market: bool = False


def classify_request(evidence: RequestEvidence) -> RequestRole:
    """Conservative, evidence-based labelling (not runtime priority routing).

    Unknown or malformed evidence does NOT become deferrable research.
    An open-position dependency wins over an entry-only dependency.
    """
    if not evidence.endpoint.startswith("/v5/market/"):
        return RequestRole.UNKNOWN
    if evidence.service == "scanner":
        # Scanner has other API calls (e.g. universe) not specified here;
        # classify only known context-building candle calls.
        if evidence.endpoint == "/v5/market/kline" and evidence.symbol:
            return RequestRole.SCANNER
        return RequestRole.UNKNOWN
    if evidence.service not in {"v3", "oos2"}:
        return RequestRole.UNKNOWN
    if evidence.endpoint not in {
        "/v5/market/kline", "/v5/market/tickers",
        "/v5/market/funding/history",
    }:
        return RequestRole.UNKNOWN
    if evidence.whole_market:
        if evidence.endpoint != "/v5/market/tickers":
            return RequestRole.UNKNOWN
        if evidence.open_position_symbols:
            return RequestRole.PROTECTIVE
        # A whole-market tickers call cannot be partitioned by symbol:
        # even entry-only needs it, but its role should remain UNKNOWN until
        # a response-safe partition and timing contract are demonstrated.
        return RequestRole.UNKNOWN
    if not evidence.symbol:
        return RequestRole.UNKNOWN
    if evidence.symbol in evidence.open_position_symbols:
        return RequestRole.PROTECTIVE
    if evidence.endpoint in {"/v5/market/kline", "/v5/market/funding/history"}:
        # Those streams are sourced from open_symbols in deployed V3/OOS2.
        # An entry-only classification for them contradicts runtime wiring.
        return RequestRole.UNKNOWN
    if evidence.endpoint == "/v5/market/tickers":
        # The deployed tickers call has NO symbol parameter.
        return RequestRole.UNKNOWN
    return RequestRole.UNKNOWN


@dataclass(frozen=True)
class Demand:
    key: str
    role: RequestRole
    arrived_slot: int
    deadline_slot: int


@dataclass(frozen=True)
class SimulationResult:
    sent: tuple[str, ...]
    not_sent: tuple[str, ...]
    protective_wait_slots: Mapping[str, int]
    by_role_sent: Mapping[str, int]

    @property
    def total(self) -> int:
        return len(self.sent) + len(self.not_sent)


def simulate_slots(
    demands: Iterable[Demand],
    capacity_by_slot: Iterable[int],
    *,
    policy_enabled: bool = False,
) -> SimulationResult:
    """Deterministic *hypothetical* capacity model, not Bybit's actual limiter.

    Legacy model = arrival order; candidate = protective, scanner, entry-only.
    UNKNOWN is always considered protective to avoid experimental deferral.
    Deadline is inclusive (slot <= deadline). Unsent demand remains counted.
    This model does NOT reproduce shared breaker, token buckets, priority
    credit borrowing, retries or HTTP delivery and cannot prove production SLO.
    """
    items = tuple(demands)
    capacities = tuple(capacity_by_slot)
    if any(c < 0 for c in capacities):
        raise ValueError("negative capacity")
    if len({x.key for x in items}) != len(items):
        raise ValueError("duplicate demand key")
    if any(x.arrived_slot < 0 or x.deadline_slot < x.arrived_slot
           for x in items):
        raise ValueError("invalid demand timing")
    if any(not isinstance(x.role, RequestRole) for x in items):
        raise ValueError("invalid request role")

    sent: list[str] = []
    seen: set[str] = set()
    waits: dict[str, int] = {}
    role_count = {role.value: 0 for role in RequestRole}

    def priority(x: Demand, index: int) -> tuple[int, int]:
        if not policy_enabled:
            return (0, index)
        rank = {
            RequestRole.PROTECTIVE: 0,
            RequestRole.UNKNOWN: 0,
            RequestRole.SCANNER: 1,
            RequestRole.ENTRY_ONLY: 2,
        }
        return (rank[x.role], index)

    for slot, cap in enumerate(capacities):
        eligible = [
            (i, x) for i, x in enumerate(items)
            if x.key not in seen
            and x.arrived_slot <= slot <= x.deadline_slot
        ]
        eligible.sort(key=lambda pair: priority(pair[1], pair[0]))
        for _idx, item in eligible[:cap]:
            seen.add(item.key)
            sent.append(item.key)
            role_count[item.role.value] += 1
            if item.role in (RequestRole.PROTECTIVE, RequestRole.UNKNOWN):
                waits[item.key] = slot - item.arrived_slot

    return SimulationResult(
        sent=tuple(sent),
        not_sent=tuple(x.key for x in items if x.key not in seen),
        protective_wait_slots=waits,
        by_role_sent=role_count,
    )
