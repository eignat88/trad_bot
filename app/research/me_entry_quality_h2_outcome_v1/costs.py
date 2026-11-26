"""Issue #298 / #312 — D-3 cost model hooks (fees / funding / slippage).

Research-only, pure: no I/O, no network, no database, no clock reads.

Every cost component is an EXPLICIT hook with a mandatory data-source label
(D-3: "each with its data source").  A cost model without provenance labels
is rejected outright — costs are never assumed silently, and no aggregate
may be produced from an unlabeled cost model.

Conventions (documented, fixed, tested):

* fees and slippage are quoted in basis points PER SIDE of notional and are
  charged on BOTH sides of a round trip
  (:data:`COST_CONVENTION`);
* funding is charged over the actually held interval as
  ``funding_bps_per_interval * (held_ms / funding_interval_ms)`` of
  notional, at the FULL rate (no direction-dependent netting — the honest
  worst-case accrual for a research model);
* all costs are subtracted from the GROSS return of the same leg and are
  applied SYMMETRICALLY to the baseline and the variant cohort (D-7).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

COST_MODEL_VERSION = "H2_OUTCOME_COST_MODEL_1.0.0"

#: Documented cost convention for every result produced by this package.
COST_CONVENTION = (
    "BPS_OF_NOTIONAL_PER_SIDE_BOTH_SIDES__FUNDING_FULL_RATE_OVER_HELD_INTERVAL_V1"
)

# ─── D-3 model enumerations (explicit; no implicit defaults) ─────────────────
FEE_MODEL_FIXED_BPS = "FEE_FIXED_BPS_PER_SIDE"

SLIPPAGE_MODEL_FIXED_BPS = "SLIPPAGE_FIXED_BPS_PER_SIDE"
SLIPPAGE_MODEL_SPREAD_BASED = "SLIPPAGE_SPREAD_BASED_HALF_SPREAD_PER_SIDE"

FUNDING_MODEL_FIXED_PER_INTERVAL = "FUNDING_FIXED_BPS_PER_INTERVAL_ACCRUAL"
FUNDING_MODEL_NONE_EXPLICIT = "FUNDING_NONE_EXPLICIT_ZERO"

FEE_MODELS = (FEE_MODEL_FIXED_BPS,)
SLIPPAGE_MODELS = (SLIPPAGE_MODEL_FIXED_BPS, SLIPPAGE_MODEL_SPREAD_BASED)
FUNDING_MODELS = (FUNDING_MODEL_FIXED_PER_INTERVAL, FUNDING_MODEL_NONE_EXPLICIT)

#: default funding interval (8h) — overridable but always explicit in the
#: serialized policy
DEFAULT_FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000

BPS = 10_000.0


class CostModelError(ValueError):
    """Invalid / unlabeled cost model (fail closed — never guess)."""


def _finite_non_negative(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CostModelError(f"{name}: expected a number")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise CostModelError(f"{name}: expected a finite value >= 0")
    return number


def _label(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CostModelError(f"{name}: a non-empty data-source label is required")
    return value


@dataclass(frozen=True)
class CostModel:
    """D-3 cost model: fees, slippage and funding, each with provenance.

    ``*_data_source`` labels are MANDATORY (e.g. ``"BYBIT_FEE_SCHEDULE_V5"``,
    ``"DERIVED_FROM_OBSERVED_SPREAD_SNAPSHOT_2026-11"``) — the model refuses
    unlabeled components so that no cost number can exist without a
    declared origin.
    """

    fee_model: str
    fee_bps_per_side: float
    fee_data_source: str
    slippage_model: str
    slippage_bps_per_side: float
    slippage_data_source: str
    funding_model: str
    funding_bps_per_interval: float
    funding_interval_ms: int
    funding_data_source: str
    #: required only for SLIPPAGE_MODEL_SPREAD_BASED
    spread_bps: float | None = None

    def __post_init__(self) -> None:
        if self.fee_model not in FEE_MODELS:
            raise CostModelError(f"unknown fee model {self.fee_model!r}")
        if self.slippage_model not in SLIPPAGE_MODELS:
            raise CostModelError(f"unknown slippage model {self.slippage_model!r}")
        if self.funding_model not in FUNDING_MODELS:
            raise CostModelError(f"unknown funding model {self.funding_model!r}")
        object.__setattr__(
            self,
            "fee_bps_per_side",
            _finite_non_negative(self.fee_bps_per_side, "fee_bps_per_side"),
        )
        object.__setattr__(
            self,
            "slippage_bps_per_side",
            _finite_non_negative(
                self.slippage_bps_per_side, "slippage_bps_per_side"
            ),
        )
        object.__setattr__(
            self,
            "funding_bps_per_interval",
            _finite_non_negative(
                self.funding_bps_per_interval, "funding_bps_per_interval"
            ),
        )
        if self.funding_model == FUNDING_MODEL_NONE_EXPLICIT:
            if self.funding_bps_per_interval != 0.0:
                raise CostModelError(
                    "FUNDING_NONE_EXPLICIT_ZERO requires funding_bps_per_interval = 0"
                )
        if (
            isinstance(self.funding_interval_ms, bool)
            or not isinstance(self.funding_interval_ms, int)
            or self.funding_interval_ms < 1
        ):
            raise CostModelError("funding_interval_ms must be a positive integer")
        object.__setattr__(self, "fee_data_source", _label(
            self.fee_data_source, "fee_data_source"
        ))
        object.__setattr__(self, "slippage_data_source", _label(
            self.slippage_data_source, "slippage_data_source"
        ))
        object.__setattr__(self, "funding_data_source", _label(
            self.funding_data_source, "funding_data_source"
        ))
        if self.slippage_model == SLIPPAGE_MODEL_SPREAD_BASED:
            if self.spread_bps is None:
                raise CostModelError(
                    "SLIPPAGE_SPREAD_BASED requires an explicit spread_bps"
                )
            object.__setattr__(
                self,
                "spread_bps",
                _finite_non_negative(self.spread_bps, "spread_bps"),
            )
        elif self.spread_bps is not None:
            raise CostModelError(
                "spread_bps is only defined for SLIPPAGE_SPREAD_BASED"
            )

    # ── serialization (policy fingerprints) ──────────────────────────────────
    def to_dict(self) -> dict[str, object]:
        return {
            "cost_model_version": COST_MODEL_VERSION,
            "cost_convention": COST_CONVENTION,
            "fee_model": self.fee_model,
            "fee_bps_per_side": self.fee_bps_per_side,
            "fee_data_source": self.fee_data_source,
            "slippage_model": self.slippage_model,
            "slippage_bps_per_side": self.slippage_bps_per_side,
            "slippage_data_source": self.slippage_data_source,
            "spread_bps": self.spread_bps,
            "funding_model": self.funding_model,
            "funding_bps_per_interval": self.funding_bps_per_interval,
            "funding_interval_ms": self.funding_interval_ms,
            "funding_data_source": self.funding_data_source,
        }

    # ── computation hooks ────────────────────────────────────────────────────
    def fee_cost_pct(self) -> float:
        """Round-trip fee cost as a fraction of notional (both sides)."""
        return 2.0 * self.fee_bps_per_side / BPS

    def slippage_cost_pct(self) -> float:
        """Round-trip slippage cost as a fraction of notional (both sides).

        Spread-based slippage models HALF the quoted spread per side, i.e.
        one full spread per round trip.
        """
        if self.slippage_model == SLIPPAGE_MODEL_SPREAD_BASED:
            assert self.spread_bps is not None
            return 2.0 * (self.spread_bps / 2.0) / BPS
        return 2.0 * self.slippage_bps_per_side / BPS

    def funding_cost_pct(self, held_ms: int) -> float:
        """Funding cost as a fraction of notional over the held interval."""
        if isinstance(held_ms, bool) or not isinstance(held_ms, int) or held_ms < 0:
            raise CostModelError("held_ms must be a non-negative integer")
        if self.funding_model == FUNDING_MODEL_NONE_EXPLICIT:
            return 0.0
        intervals = held_ms / float(self.funding_interval_ms)
        return self.funding_bps_per_interval / BPS * intervals

    def total_cost_pct(self, held_ms: int) -> float:
        return (
            self.fee_cost_pct()
            + self.slippage_cost_pct()
            + self.funding_cost_pct(held_ms)
        )

    def net_return_pct(self, gross_return_pct: float, held_ms: int) -> float:
        return float(gross_return_pct) - self.total_cost_pct(held_ms)

    def net_return_r(
        self,
        gross_return_r: float,
        held_ms: int,
        *,
        entry_price: float,
        stop_distance: float,
    ) -> float:
        """Net return in R multiples of the leg's stop distance.

        The cost fraction of notional converts to R as
        ``cost_pct * entry_price / stop_distance``.
        """
        if not math.isfinite(float(entry_price)) or entry_price <= 0.0:
            raise CostModelError("entry_price must be finite and > 0")
        if not math.isfinite(float(stop_distance)) or stop_distance <= 0.0:
            raise CostModelError("stop_distance must be finite and > 0")
        cost_r = self.total_cost_pct(held_ms) * float(entry_price) / float(
            stop_distance
        )
        return float(gross_return_r) - cost_r

    def breakdown(self, held_ms: int) -> dict[str, object]:
        """Serializable per-leg cost breakdown with data-source labels."""
        return {
            "cost_convention": COST_CONVENTION,
            "fee_cost_pct": self.fee_cost_pct(),
            "slippage_cost_pct": self.slippage_cost_pct(),
            "funding_cost_pct": self.funding_cost_pct(held_ms),
            "total_cost_pct": self.total_cost_pct(held_ms),
            "held_ms": int(held_ms),
            "fee_data_source": self.fee_data_source,
            "slippage_data_source": self.slippage_data_source,
            "funding_data_source": self.funding_data_source,
            "fee_model": self.fee_model,
            "slippage_model": self.slippage_model,
            "funding_model": self.funding_model,
        }
