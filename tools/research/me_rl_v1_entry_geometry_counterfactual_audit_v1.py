"""Read-only ME_RL_V1 historical entry-geometry counterfactual audit.

This tool intentionally performs no PostgreSQL writes, no scanner changes,
and no production recovery.  It produces local CSV/JSON deliverables from
either a frozen observation snapshot or a read-only local source snapshot.

The evaluator is fail-closed for every model:
  - Model A uses recorded entry/SL/TP and excludes invalid LONG geometry.
  - Model B uses a verified closed 5m signal candle close.
  - Incomplete candle paths are classified INCOMPLETE and never finalized.
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

CANDLE_MS = 300_000
HORIZON_MINUTES = 240
FEE_RATE = 0.00055
SLIPPAGE_RATE = 0.0005
ELEVATED_SLIPPAGE_RATE = 0.0025
TAKER_FEE_RATE = 0.00055
STOP_FIRST = "STOP_FIRST"
TIMEOUT = "TIMEOUT"
TP_FIRST = "TP_FIRST"
SL_FIRST = "SL_FIRST"
INTRABAR_AMBIGUOUS = "INTRABAR_AMBIGUOUS"
INCOMPLETE = "INCOMPLETE"
NO_ELIGIBLE_CANDLE = "NO_ELIGIBLE_CANDLE"
ENTRY_SOURCE_UNVERIFIED = "ENTRY_SOURCE_UNVERIFIED"
INVALID_RECORDED_LONG_GEOMETRY = "INVALID_RECORDED_LONG_GEOMETRY"
MODEL_A = "V1_AS_RECORDED"
MODEL_B = "V1_CLOSE_ANCHORED_CF"


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def validate(self) -> str | None:
        values = (self.open, self.high, self.low, self.close)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            return "INVALID_NON_POSITIVE_OHLC"
        if self.timestamp % CANDLE_MS != 0:
            return "INVALID_5M_ALIGNMENT"
        if self.high < max(self.open, self.close):
            return "INVALID_HIGH_OHLC"
        if self.low > min(self.open, self.close):
            return "INVALID_LOW_OHLC"
        if self.low > self.high:
            return "INVALID_LOW_GT_HIGH"
        return None


@dataclass(frozen=True)
class Observation:
    observation_id: Any
    experiment_id: str
    scanner_name: str
    symbol: str
    direction: str
    setup_id: str | None
    signal_time: datetime
    signal_candle_open_time: int
    reference_price: float
    entry_zone_low: float | None
    entry_zone_high: float | None
    invalidation_price: float | None
    target_1: float | None
    target_2: float | None
    features: Mapping[str, Any]
    market_regime: str | None


@dataclass(frozen=True)
class Outcome:
    model: str
    observation_id: Any
    setup_id: str | None
    symbol: str
    signal_time: str
    signal_candle_open_time: int
    signal_close: float | None
    entry_price: float | None
    stop_price: float | None
    target_price: float | None
    risk_distance_pct: float | None
    reward_distance_pct: float | None
    reward_risk: float | None
    entry_mismatch_pct: float | None
    status: str
    exit_type: str | None
    gross_r: float | None
    fee_adjusted_r: float | None
    fully_net_r: float | None
    elevated_net_r: float | None
    mfe_r: float | None
    mae_r: float | None
    timeout_minutes: int
    reason_code: str | None
    coverage_complete: bool


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _signal_ms(value: datetime) -> int:
    return int(_utc(value).timestamp() * 1000)


def validate_candles(candles: Sequence[Candle], *, enforce_alignment: bool = True) -> list[str]:
    reasons: list[str] = []
    previous: int | None = None
    seen: set[int] = set()
    for candle in candles:
        reason = candle.validate()
        if reason == "INVALID_5M_ALIGNMENT" and not enforce_alignment:
            reason = None
        if reason:
            reasons.append(reason)
        if candle.timestamp in seen:
            reasons.append("DUPLICATE_CANDLE")
        seen.add(candle.timestamp)
        if previous is not None and candle.timestamp <= previous:
            reasons.append("NON_MONOTONIC_CANDLES")
        previous = candle.timestamp
    return reasons


def _future_window(
    candles: Sequence[Candle],
    signal_candle_open_ms: int,
) -> tuple[list[Candle], bool, str | None]:
    """Return strictly post-signal candles and complete 240m coverage flag."""
    expected_opens = [
        signal_candle_open_ms + CANDLE_MS + offset * CANDLE_MS
        for offset in range(HORIZON_MINUTES // 5)
    ]
    relevant = [
        candle for candle in candles
        if signal_candle_open_ms <= candle.timestamp <= expected_opens[-1]
    ]

    integrity_reasons = validate_candles(relevant)
    if integrity_reasons:
        return [], False, "SOURCE_INVALID_" + integrity_reasons[0]

    by_open = {candle.timestamp: candle for candle in relevant}
    selected: list[Candle] = []
    missing: list[int] = []

    for open_ms in expected_opens:
        candle = by_open.get(open_ms)
        if candle is None:
            missing.append(open_ms)
            continue
        selected.append(candle)

    if missing:
        return selected, False, f"MISSING_CANDLES_{len(missing)}"

    return selected, True, None


def _signal_close(
    candles: Sequence[Candle],
    signal_candle_open_ms: int,
) -> tuple[Candle | None, str | None]:
    for candle in candles:
        if candle.timestamp == signal_candle_open_ms:
            if candle.validate() is not None and candle.validate() != "INVALID_5M_ALIGNMENT":
                return None, "SIGNAL_CANDLE_OHLC_INVALID"
            return candle, None
    return None, "SIGNAL_CANDLE_NOT_CLOSED_OR_UNVERIFIED"


def _geometry(
    entry: float,
    stop: float,
    target: float,
    signal_close: float | None,
) -> dict[str, Any]:
    risk = entry - stop
    reward = target - entry
    return {
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "risk_distance_pct": risk / entry * 100 if entry > 0 else None,
        "reward_distance_pct": reward / entry * 100 if entry > 0 else None,
        "reward_risk": reward / risk if risk > 0 else None,
        "entry_mismatch_pct": (entry - signal_close) / signal_close * 100
        if signal_close and signal_close > 0 else None,
    }


def _costs(
    gross_return: float,
    risk_distance: float,
    *,
    slippage_rate: float = SLIPPAGE_RATE,
) -> dict[str, float]:
    cost_return = (FEE_RATE + slippage_rate) * 2
    cost_r = cost_return / (risk_distance / 100) if risk_distance > 0 else math.inf
    return {
        "fee_adjusted_r": gross_return / risk_distance - 2 * FEE_RATE / (risk_distance / 100),
        "fully_net_r": gross_return / risk_distance - cost_r,
        "elevated_net_r": gross_return / risk_distance - (
            (FEE_RATE + ELEVATED_SLIPPAGE_RATE) * 2
        ) / (risk_distance / 100),
    }


def _evaluate_path(
    *,
    model: str,
    obs: Observation,
    geometry: Mapping[str, Any],
    path: Sequence[Candle],
    complete: bool,
    reason_code: str | None,
) -> Outcome:
    entry = float(geometry["entry_price"])
    stop = float(geometry["stop_price"])
    target = float(geometry["target_price"])
    risk_distance = (entry - stop) / entry * 100
    base = dict(
        model=model,
        observation_id=obs.observation_id,
        setup_id=obs.setup_id,
        symbol=obs.symbol,
        signal_time=obs.signal_time.astimezone(timezone.utc).isoformat(),
        signal_candle_open_time=obs.signal_candle_open_time,
        signal_close=geometry.get("signal_close"),
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        risk_distance_pct=risk_distance,
        reward_distance_pct=geometry["reward_distance_pct"],
        reward_risk=geometry["reward_risk"],
        entry_mismatch_pct=geometry["entry_mismatch_pct"],
        status="INCOMPLETE",
        exit_type=None,
        gross_r=None,
        fee_adjusted_r=None,
        fully_net_r=None,
        elevated_net_r=None,
        mfe_r=None,
        mae_r=None,
        timeout_minutes=HORIZON_MINUTES,
        reason_code=reason_code,
        coverage_complete=complete,
    )
    if not complete:
        return Outcome(**base)

    highs = [candle.high for candle in path]
    lows = [candle.low for candle in path]
    mfe = (max(highs) - entry) / (entry - stop) if highs else None
    mae = (entry - min(lows)) / (entry - stop) if lows else None
    base.update(mfe_r=mfe, mae_r=mae)

    exit_type = TIMEOUT
    gross_return = (path[-1].close - entry) / entry * 100

    for candle in path:
        tp_hit = candle.high >= target
        sl_hit = candle.low <= stop

        if tp_hit and sl_hit:
            exit_type = INTRABAR_AMBIGUOUS
            gross_return = -risk_distance
            break

        if sl_hit:
            exit_type = SL_FIRST
            gross_return = -risk_distance
            break

        if tp_hit:
            exit_type = TP_FIRST
            gross_return = (target - entry) / entry * 100
            break

    costs = _costs(gross_return, risk_distance)
    base.update(
        status="FINALIZED",
        exit_type=exit_type,
        gross_r=gross_return / risk_distance,
        **costs,
    )
    return Outcome(**base)


def evaluate_model_a(
    obs: Observation,
    candles: Sequence[Candle],
) -> Outcome:
    entry = _finite(obs.reference_price)
    stop = _finite(obs.invalidation_price)
    target = _finite(obs.target_1)
    if entry is None or stop is None or target is None:
        return _base_invalid(obs, "MISSING_RECORDED_PRICE")
    if stop >= entry or target <= entry:
        return _base_invalid(obs, INVALID_RECORDED_LONG_GEOMETRY)
    signal_close_candle, signal_reason = _signal_close(candles, obs.signal_candle_open_time)
    if signal_close_candle is not None and signal_close_candle.timestamp + CANDLE_MS > _signal_ms(obs.signal_time):
        signal_close_candle = None
        signal_reason = "SIGNAL_CANDLE_NOT_CLOSED_BEFORE_SIGNAL"
    path, complete, path_reason = _future_window(candles, obs.signal_candle_open_time)
    geometry = _geometry(entry, stop, target, signal_close_candle.close if signal_close_candle else None)
    geometry["signal_close"] = signal_close_candle.close if signal_close_candle else None
    return _evaluate_path(
        model=MODEL_A,
        obs=obs,
        geometry=geometry,
        path=path,
        complete=complete,
        reason_code=path_reason or signal_reason,
    )


def evaluate_model_b(
    obs: Observation,
    candles: Sequence[Candle],
) -> Outcome:
    signal_close_candle, signal_reason = _signal_close(candles, obs.signal_candle_open_time)
    if signal_close_candle is None:
        return _base_invalid(obs, ENTRY_SOURCE_UNVERIFIED)
    if signal_close_candle.timestamp + CANDLE_MS > _signal_ms(obs.signal_time):
        return _base_invalid(obs, "SIGNAL_CANDLE_NOT_CLOSED_BEFORE_SIGNAL")
    entry = signal_close_candle.close
    geometry = _geometry(entry, entry * 0.975, entry * 1.03, entry)
    geometry["signal_close"] = entry
    path, complete, path_reason = _future_window(candles, obs.signal_candle_open_time)
    return _evaluate_path(
        model=MODEL_B,
        obs=obs,
        geometry=geometry,
        path=path,
        complete=complete,
        reason_code=path_reason,
    )


def _base_invalid(obs: Observation, reason: str) -> Outcome:
    return Outcome(
        model="", observation_id=obs.observation_id, setup_id=obs.setup_id,
        symbol=obs.symbol,
        signal_time=obs.signal_time.astimezone(timezone.utc).isoformat(),
        signal_candle_open_time=obs.signal_candle_open_time, signal_close=None,
        entry_price=None, stop_price=None, target_price=None,
        risk_distance_pct=None, reward_distance_pct=None, reward_risk=None,
        entry_mismatch_pct=None, status="INVALID", exit_type=None,
        gross_r=None, fee_adjusted_r=None, fully_net_r=None,
        elevated_net_r=None, mfe_r=None, mae_r=None,
        timeout_minutes=HORIZON_MINUTES, reason_code=reason,
        coverage_complete=False,
    )


def summarize(outcomes: Sequence[Outcome], seed: int = 20261009) -> dict[str, Any]:
    finalized = [o for o in outcomes if o.status == "FINALIZED"]
    values = [o.fully_net_r for o in finalized if o.fully_net_r is not None]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value <= 0]
    gross = [o.gross_r for o in finalized if o.gross_r is not None]
    fee = [o.fee_adjusted_r for o in finalized if o.fee_adjusted_r is not None]
    elevated = [o.elevated_net_r for o in finalized if o.elevated_net_r is not None]
    symbols = {o.symbol for o in finalized}
    days = {o.signal_time[:10] for o in finalized}
    return {
        "raw_n": len(outcomes),
        "complete_n": sum(o.coverage_complete for o in outcomes),
        "eligible_n": sum(o.status == "FINALIZED" for o in outcomes),
        "finalized_n": len(finalized),
        "invalid_n": sum(o.status == "INVALID" for o in outcomes),
        "missing_coverage_n": sum(
            o.status == "INCOMPLETE" for o in outcomes
        ),
        "symbols": len(symbols),
        "independent_signal_days": len(days),
        "exit_types": dict(Counter(o.exit_type for o in finalized)),
        "win_rate": len(wins) / len(values) if values else None,
        "gross_e_r": statistics.fmean(gross) if gross else None,
        "fee_adjusted_e_r": statistics.fmean(fee) if fee else None,
        "net_e_r": statistics.fmean(values) if values else None,
        "elevated_net_e_r": statistics.fmean(elevated) if elevated else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if wins and losses else None,
        "mean_mfe_r": statistics.fmean(
            [o.mfe_r for o in finalized if o.mfe_r is not None]
        ) if any(o.mfe_r is not None for o in finalized) else None,
        "mean_mae_r": statistics.fmean(
            [o.mae_r for o in finalized if o.mae_r is not None]
        ) if any(o.mae_r is not None for o in finalized) else None,
        "ci95_net_e_r": bootstrap_mean_ci(values, seed=seed) if values else None,
        "reason_codes": dict(Counter(
            o.reason_code for o in outcomes if o.reason_code
        )),
    }


def bootstrap_mean_ci(values: Sequence[float], *, seed: int = 20261009, reps: int = 10_000) -> list[float] | None:
    if not values:
        return None
    rng = random.Random(seed)
    sample = []
    n = len(values)
    for _ in range(reps):
        sample.append(statistics.fmean(rng.choice(values) for _ in range(n)))
    sample.sort()
    return [sample[int(reps * 0.025)], sample[int(reps * 0.975) - 1]]


def load_observations(path: Path) -> list[Observation]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    observations: list[Observation] = []
    for row in rows:
        signal_time = datetime.fromisoformat(row["signal_time"])
        observations.append(Observation(
            observation_id=row.get("observation_id"),
            experiment_id=row["experiment_id"],
            scanner_name=row["scanner_name"],
            symbol=row["symbol"],
            direction=row["direction"],
            setup_id=row.get("setup_id") or None,
            signal_time=signal_time,
            signal_candle_open_time=int(row.get("signal_candle_open_time") or 0),
            reference_price=float(row["reference_price"]),
            entry_zone_low=_finite(row.get("entry_zone_low")),
            entry_zone_high=_finite(row.get("entry_zone_high")),
            invalidation_price=_finite(row.get("invalidation_price")),
            target_1=_finite(row.get("target_1")),
            target_2=_finite(row.get("target_2")),
            features=json.loads(row.get("features") or "{}"),
            market_regime=row.get("market_regime") or None,
        ))
    return observations


def load_candles_by_setup(path: Path) -> dict[str, list[Candle]]:
    by_setup: dict[str, list[Candle]] = defaultdict(list)
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                if not row.get("setup_id") or not row.get("open_ms"):
                    continue
                by_setup[row["setup_id"]].append(Candle(
                    timestamp=int(float(row["open_ms"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                ))
            except (TypeError, ValueError, KeyError):
                continue
    return by_setup


def write_outcomes(path: Path, outcomes: Sequence[Outcome]) -> None:
    fields = list(asdict(outcomes[0]).keys()) if outcomes else []
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for outcome in outcomes:
            writer.writerow(asdict(outcome))


def main() -> None:
    raise SystemExit("This audit tool is intended to be imported by the local audit runner.")
