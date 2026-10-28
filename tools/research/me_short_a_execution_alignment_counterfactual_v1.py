"""Issue #206: offline, read-only, signal-aligned ME SHORT A counterfactual.

Inputs are externally exported CSV files. Never queries or modifies production
DB. Only fully CLOSED 5-minute bars whose open is >= the first complete
post-signal bar are used. This deliberately excludes the signal's partially
observed candle: incomplete intrabar sequencing cannot be made reliable from
OHLC alone.

Protocol ``ISSUE_206_ME_SHORT_A_V2`` (corrective review of PR #211):

1. Candle integrity is fail-closed. Every row is validated (strict 5-minute
   grid, OHLC bounds ``low <= open/close <= high``, parseable values) and every
   ``(symbol, open_time)`` slot is checked for duplicates/conflicts. Identical
   duplicates are collapsed and counted; conflicting rows NEVER resolve by
   "first record wins" -- a needed slot with conflicting rows yields the
   dedicated ``CONFLICTING_CANDLE`` status. Unclosed, future and corrupt rows
   are excluded; the hole they leave is then classified like any other missing
   data (never imputed).
2. Horizon/timeout statuses are strictly separated:
   ``MISSING_CANDLE`` (gap inside the provided history),
   ``SOURCE_INCOMPLETE_COVERAGE`` (the export does not cover enough to
   evaluate), ``TIMEOUT_CLOSE_UNAVAILABLE`` (history complete but a
   mid-candle timeout mark cannot be resolved from 5m OHLC) and
   ``FINAL`` (confirmed TP/SL, or a timeout correctly computed from the close
   of the bar ending exactly at the cutoff). An unclosed bar is never treated
   as closed and no outcome is finalized on an incomplete path.
3. The modeled entry price ``estimated_fill`` is strictly separated from real
   confirmed execution. ``assess_execution_feasibility`` re-checks quote/order
   book timestamp and freshness (incl. assumed latency), spread, available
   depth, notional/qty/exchange minima and the arithmetic needed to recalculate
   the modeled fill. Missing/stale/corrupt evidence yields
   ``EXECUTION_FEASIBILITY_UNKNOWN`` -- never an automatically executable
   trade. Known constraint violations yield ``EXECUTION_INFEASIBLE``.
4. Analytics: frozen vs executable are compared pairwise on one canonical
   ``event_key`` (byte-identical to the Phase-1/lineage identity); normal and
   elevated frozen cost scenarios are reported side by side; the cost
   decomposition (reference-to-bid drift, market impact, spread, fees, exit
   slippage) is explicit and non-additive by construction; a symbol/day
   coverage report and a symbol-day cluster bootstrap (correlation of repeated
   signals preserved) accompany the results. Pairs where either side lacks the
   required data are excluded, never merged.

Frozen Geometry A semantics are unchanged: entry/stop/target stay absolute,
both-in-one-bar stays STOP_FIRST (conservative), the whole signal candle stays
excluded, the horizon stays 240 minutes.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

D = Decimal

PROTOCOL = "ISSUE_206_ME_SHORT_A_V2"

# Frozen cost constants (ME_SHORT_GEOM_A_EXECUTION_V1 registry). Never retune.
FEE_PER_SIDE = D("0.00055")
NORMAL_COST = D("0.0021")
ELEVATED_COST = D("0.0031")
HOLD_MINUTES = 240
CANDLE_MINUTES = 5

# Book freshness bounds (frozen in ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1).
BOOK_STALENESS_LIMIT_MS = 10000
BOOK_FUTURE_SKEW_MS = 1000

# Outcome statuses.
FINAL = "FINAL"
MISSING_CANDLE = "MISSING_CANDLE"
SOURCE_INCOMPLETE_COVERAGE = "SOURCE_INCOMPLETE_COVERAGE"
TIMEOUT_CLOSE_UNAVAILABLE = "TIMEOUT_CLOSE_UNAVAILABLE"
CONFLICTING_CANDLE = "CONFLICTING_CANDLE"
INVALID_GEOMETRY = "INVALID_GEOMETRY"
MISSING_ENTRY = "MISSING_ENTRY"

# Outcome paths.
PATH_TP = "TP"
PATH_SL = "SL"
PATH_STOP_FIRST = "AMBIGUOUS_STOP_FIRST"
PATH_TIMEOUT = "TIMEOUT"

# Execution feasibility statuses (modeled != confirmed; offline work can never
# confirm a real fill).
EXECUTION_MODELED = "EXECUTION_FEASIBILITY_MODELED"
EXECUTION_UNKNOWN = "EXECUTION_FEASIBILITY_UNKNOWN"
EXECUTION_INFEASIBLE = "EXECUTION_INFEASIBLE"

# Row classification inside the candle intake.
ROW_OK = "OK"
ROW_CORRUPT = "CORRUPT"
ROW_UNCLOSED = "UNCLOSED"
ROW_FUTURE = "FUTURE"

# Evidence fields required to recalculate the modeled fill (fail-closed: any
# missing field makes the executable side EXECUTION_FEASIBILITY_UNKNOWN).
REQUIRED_EXECUTION_FIELDS = (
    "bid", "ask", "book_time_ms", "depth_bid_10bps_usdt",
    "requested_notional_usdt", "requested_qty", "executable_qty",
    "min_order_qty", "qty_step", "min_notional",
)

ASSUMPTIONS = {
    "hold_minutes": HOLD_MINUTES,
    "cost_rate_normal": str(NORMAL_COST),
    "cost_rate_elevated": str(ELEVATED_COST),
    "first_partial_candle": "EXCLUDED",
    "signal_candle": "EXCLUDED",
    "candles_crossing_timeout": "EXCLUDED_FROM_HIT_DETECTION",
    "intrabar_ambiguity": "STOP_FIRST",
    "exit_slippage": "NOT_MODELED",
    "confirmed_execution": "NEVER_AVAILABLE_OFFLINE",
    "assumed_latency_default_ms": 0,
    "book_staleness_limit_ms": BOOK_STALENESS_LIMIT_MS,
    "book_future_skew_limit_ms": BOOK_FUTURE_SKEW_MS,
    "overlap_portfolio": "NOT_MODELED",
}

COST_MODEL = {
    "fee_per_side": str(FEE_PER_SIDE),
    "fees_roundtrip_pct": str(2 * FEE_PER_SIDE),
    "cost_rate_normal_pct": str(NORMAL_COST),
    "cost_rate_elevated_pct": str(ELEVATED_COST),
    "reference_to_bid_drift": "EMBEDDED_IN_EXECUTABLE_ENTRY_PRICE; never added to cost_rate",
    "market_impact": "EMBEDDED_IN_EXECUTABLE_ENTRY_PRICE (bid->weighted fill book walk); never added to cost_rate",
    "spread": "covered once inside cost_rate (exit crosses spread)",
    "fees": "covered once inside cost_rate",
    "exit_slippage": "NOT_MODELED (unknown, not zero); covered by the elevated allowance only as a scenario",
    "double_count_policy": "drift and impact live in the executable entry price; spread+fees+exit allowance live in cost_rate; no term is subtracted twice",
}


# ─── small helpers ───────────────────────────────────────────────────────────


def ts(value) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).strip().replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must include timezone")
    return result.astimezone(timezone.utc)


def _decimal(value):
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed


def _closed_flag(value):
    """True/False for a parseable is_closed value, None when unparseable."""
    text = str(value).strip().lower() if value is not None else ""
    if text in ("true", "t", "1", "yes"):
        return True
    if text in ("false", "f", "0", "no"):
        return False
    return None


def _grid_floor(moment: datetime) -> datetime:
    """Largest 5-minute grid open at or before ``moment`` (exact, no floats)."""
    moment = ts(moment)
    minutes = moment.hour * 60 + moment.minute
    floored = minutes - minutes % CANDLE_MINUTES
    return moment.replace(hour=floored // 60, minute=floored % 60,
                          second=0, microsecond=0)


def _on_grid(moment: datetime) -> bool:
    moment = ts(moment)
    return moment.second == 0 and moment.microsecond == 0 and moment.minute % CANDLE_MINUTES == 0


def _epoch_ms(moment: datetime) -> int:
    return int(ts(moment).timestamp() * 1000)


def _identity_price(value) -> Decimal:
    """Canonical price representation of the frozen event identity.

    ``Decimal(str(float(value)))`` -- byte-identical to ``canonical_price`` in
    ``app.research.me_short_a_forward_outcome_runtime_v1`` (the writer/reader
    identity contract fixed in #212): the scanner hashes its in-memory
    ``float`` prices, so every spelling of one number (``"342.10"``,
    ``Decimal("342.1")``, ``342.1``) must hash as ``342.1``.
    """
    return Decimal(str(float(value)))


def event_key(symbol, signal_time, reference_price, stop_price, target_price) -> str:
    """Deterministic event identity, byte-identical to the frozen Phase-1 key.

    ``sha256(symbol|SHORT|<utc isoformat signal_time>|<ref>|<stop>|<target>)``
    with prices in the canonical representation above; as frozen in migration
    077 / ``app.research.me_short_a_source_intake_v1.compute_event_key``
    (guarded by a parity regression test).
    """
    payload = (f"{symbol}|SHORT|{ts(signal_time).isoformat()}"
               f"|{_identity_price(reference_price)}"
               f"|{_identity_price(stop_price)}"
               f"|{_identity_price(target_price)}")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ─── candle intake / integrity ───────────────────────────────────────────────


def classify_candle_row(row: dict, *, as_of: datetime | None = None):
    """Validate one raw candle row. Returns ``(classification, parsed, reason)``.

    ``parsed`` is a normalized dict for OK rows, else the partial raw values.
    Nothing is imputed: unparseable/corrupt rows are excluded and reported.
    """
    reason = None
    try:
        opened = ts(row.get("open_time"))
    except (ValueError, TypeError):
        return ROW_CORRUPT, row, "TIMESTAMP_INVALID"
    partial = {"open_ms": _epoch_ms(opened), "open_time": opened}
    if not _on_grid(opened):
        return ROW_CORRUPT, partial, "OFF_GRID_OPEN_TIME"
    opened_ms = partial["open_ms"]
    values = {}
    for name in ("open", "high", "low", "close"):
        parsed = _decimal(row.get(name))
        if parsed is None or parsed <= 0:
            return ROW_CORRUPT, partial, "PRICE_INVALID"
        values[name] = parsed
    if not (values["low"] <= min(values["open"], values["close"])
            and values["high"] >= max(values["open"], values["close"])
            and values["low"] <= values["high"]):
        return ROW_CORRUPT, {**partial, **values}, "OHLC_INVALID"
    closed = _closed_flag(row.get("is_closed"))
    if closed is None:
        return ROW_CORRUPT, {**partial, **values}, "IS_CLOSED_INVALID"
    if not closed:
        return ROW_UNCLOSED, {"open_ms": opened_ms, "open_time": opened, **values}, "UNCLOSED_CANDLE"
    if as_of is not None and opened_ms + CANDLE_MINUTES * 60000 > _epoch_ms(as_of):
        return ROW_FUTURE, {"open_ms": opened_ms, "open_time": opened, **values}, "FUTURE_OR_UNCLOSED_CANDLE"
    parsed = {"open_ms": opened_ms, "open_time": opened, **values}
    return ROW_OK, parsed, reason


class SymbolCandles:
    """Indexed, fail-closed candle set for one symbol.

    ``usable`` holds exactly one validated row per grid slot. Slots with
    conflicting rows are recorded in ``conflicts`` and are NEVER resolved by
    taking the first record. Excluded rows are counted by reason so the
    integrity report can show what was dropped and why.
    """

    def __init__(self, rows, symbol: str, *, as_of: datetime | None = None):
        self.symbol = symbol
        self.counts = {"rows_total": 0, "usable_slots": 0,
                       "duplicate_identical_rows": 0, "conflicting_slots": 0,
                       "corrupt_rows": 0, "unclosed_rows": 0, "future_rows": 0}
        self.exclusion_reasons: dict[str, int] = {}
        self.excluded_by_slot: dict[int, dict[str, int]] = {}
        slots: dict[int, list] = {}
        for raw in rows:
            if str(raw.get("symbol", "")).strip() != symbol:
                continue
            self.counts["rows_total"] += 1
            classification, parsed, reason = classify_candle_row(raw, as_of=as_of)
            if classification == ROW_CORRUPT:
                self.counts["corrupt_rows"] += 1
                self.exclusion_reasons[reason] = self.exclusion_reasons.get(reason, 0) + 1
                open_ms = parsed.get("open_ms")
                if open_ms is None:
                    continue
            else:
                open_ms = parsed["open_ms"]
                if classification != ROW_OK:
                    key = "unclosed_rows" if classification == ROW_UNCLOSED else "future_rows"
                    self.counts[key] += 1
                    self.exclusion_reasons[reason] = self.exclusion_reasons.get(reason, 0) + 1
            slots.setdefault(open_ms, []).append((classification, parsed, reason))
        self.usable: dict[int, dict] = {}
        self.conflicts: dict[int, list] = {}
        for open_ms, items in sorted(slots.items()):
            priced = [(c, p, r) for c, p, r in items if "open" in p]
            tuples = {(p["open"], p["high"], p["low"], p["close"]) for _, p, _ in priced}
            ok_items = [p for c, p, r in items if c == ROW_OK]
            excluded = [(c, r) for c, p, r in items if c != ROW_OK]
            for classification, reason in excluded:
                bucket = self.excluded_by_slot.setdefault(open_ms, {})
                bucket[reason] = bucket.get(reason, 0) + 1
            if len(tuples) > 1:
                # Conflicting OHLC records for one slot: never pick a winner.
                self.conflicts[open_ms] = [p for _, p, _ in priced]
                self.counts["conflicting_slots"] += 1
            elif ok_items:
                self.usable[open_ms] = ok_items[0]
                self.counts["duplicate_identical_rows"] += max(0, len(ok_items) - 1)
        self.counts["usable_slots"] = len(self.usable)


def build_integrity(candles: list[dict], symbols, *, as_of: datetime | None = None) -> dict:
    by_symbol = {}
    totals = {"rows_total": 0, "usable_slots": 0, "duplicate_identical_rows": 0,
              "conflicting_slots": 0, "corrupt_rows": 0, "unclosed_rows": 0, "future_rows": 0}
    for symbol in symbols:
        index = SymbolCandles(candles, symbol, as_of=as_of)
        by_symbol[symbol] = {**index.counts,
                             "exclusion_reasons": dict(sorted(index.exclusion_reasons.items()))}
        for key in totals:
            totals[key] += index.counts[key]
    return {"by_symbol": by_symbol, "totals": totals}


# ─── outcome evaluation ──────────────────────────────────────────────────────


def _net_block(entry: Decimal, exit_price: Decimal, stop: Decimal,
               cost_rate: Decimal) -> dict:
    risk = stop - entry
    gross = (entry - exit_price) / risk
    net = gross - cost_rate * entry / risk
    pnl_pct = (entry - exit_price) / entry * 100 - cost_rate * 100
    return {"gross_r": gross, "net_r": net, "net_pnl_pct": pnl_pct}


def evaluate(signal: dict, candles: list[dict], *, entry_key: str,
             cost_rate: Decimal = NORMAL_COST,
             as_of: datetime | None = None,
             index: SymbolCandles | None = None) -> dict:
    """Resolve one arm of one signal on validated closed 5m candles.

    Returns the outcome for the modeled entry ``signal[entry_key]``. The result
    never claims confirmed execution and never finalizes on an incomplete path.
    """
    symbol = signal["symbol"]
    start = ts(signal["signal_time"])
    end = start + timedelta(minutes=HOLD_MINUTES)
    entry = _decimal(signal.get(entry_key))
    if entry is None:
        return {"status": MISSING_ENTRY, "model": entry_key}
    stop = _decimal(signal.get("stop_price"))
    target = _decimal(signal.get("target_price"))
    if stop is None or target is None or not (entry > target > 0 and stop > entry):
        return {"status": INVALID_GEOMETRY, "model": entry_key}

    if index is None:
        index = SymbolCandles(candles, symbol, as_of=as_of)

    first_open = _grid_floor(start) + timedelta(minutes=CANDLE_MINUTES)
    on_grid = _on_grid(start)
    hit_last_open = _grid_floor(end - timedelta(minutes=CANDLE_MINUTES))
    # Coverage is required through the bar that contains the cutoff mark: the
    # bar ending exactly at an on-grid cutoff, or the straddling bar otherwise.
    required_last_open = (end - timedelta(minutes=CANDLE_MINUTES)) if on_grid \
        else _grid_floor(end)

    cursor = first_open
    while cursor <= required_last_open:
        cursor_ms = _epoch_ms(cursor)
        if cursor_ms in index.conflicts:
            return {"status": CONFLICTING_CANDLE, "model": entry_key,
                    "reason": "CONFLICTING_CANDLE",
                    "conflict_open": cursor.isoformat(),
                    "conflicting_rows": len(index.conflicts[cursor_ms])}
        candle = index.usable.get(cursor_ms)
        if candle is None:
            later = any(cursor_ms < open_ms <= _epoch_ms(required_last_open)
                        for open_ms in index.usable)
            if later:
                return {"status": MISSING_CANDLE, "model": entry_key,
                        "reason": "GAP_IN_HISTORY",
                        "expected_open": cursor.isoformat(),
                        "excluded_rows_at_slot": dict(sorted(
                            index.excluded_by_slot.get(cursor_ms, {}).items()))}
            slot_reasons = index.excluded_by_slot.get(cursor_ms, {})
            if cursor == required_last_open and slot_reasons.get("UNCLOSED_CANDLE"):
                reason = "CUTOFF_CANDLE_UNCLOSED"
            elif index.counts["usable_slots"] == 0:
                reason = "NO_CANDLES"
            else:
                reason = "SOURCE_ENDS_BEFORE_HORIZON"
            return {"status": SOURCE_INCOMPLETE_COVERAGE, "model": entry_key,
                    "reason": reason, "expected_open": cursor.isoformat(),
                    "excluded_rows_at_slot": dict(sorted(slot_reasons.items())),
                    "covered_until": (cursor + timedelta(minutes=CANDLE_MINUTES)).isoformat(),
                    "deadline": end.isoformat()}
        if cursor <= hit_last_open:
            high, low = candle["high"], candle["low"]
            hit_stop, hit_tp = high >= stop, low <= target
            if hit_stop or hit_tp:
                path = PATH_STOP_FIRST if hit_stop and hit_tp else (PATH_SL if hit_stop else PATH_TP)
                exit_price = stop if hit_stop else target
                result = {"status": FINAL, "model": entry_key, "path": path,
                          "exit_candle": cursor.isoformat(), "entry": str(entry),
                          "exit": str(exit_price)}
                for label, rate in (("normal", NORMAL_COST), ("elevated", ELEVATED_COST)):
                    block = _net_block(entry, exit_price, stop, rate)
                    result[f"net_r_{label}"] = str(block["net_r"])
                    result[f"net_pnl_pct_{label}"] = str(block["net_pnl_pct"])
                primary = _net_block(entry, exit_price, stop, cost_rate)
                result["gross_r"] = str(primary["gross_r"])
                result["net_r"] = str(primary["net_r"])
                result["net_pnl_pct"] = str(primary["net_pnl_pct"])
                return result
        cursor += timedelta(minutes=CANDLE_MINUTES)

    if on_grid:
        # Timeout mark coincides with the close of the bar ending at the cutoff:
        # a timeout close CAN be computed from fully closed 5m data.
        exit_price = index.usable[_epoch_ms(hit_last_open)]["close"]
        result = {"status": FINAL, "model": entry_key, "path": PATH_TIMEOUT,
                  "exit_candle": hit_last_open.isoformat(), "entry": str(entry),
                  "exit": str(exit_price)}
        for label, rate in (("normal", NORMAL_COST), ("elevated", ELEVATED_COST)):
            block = _net_block(entry, exit_price, stop, rate)
            result[f"net_r_{label}"] = str(block["net_r"])
            result[f"net_pnl_pct_{label}"] = str(block["net_pnl_pct"])
        primary = _net_block(entry, exit_price, stop, cost_rate)
        result["gross_r"] = str(primary["gross_r"])
        result["net_r"] = str(primary["net_r"])
        result["net_pnl_pct"] = str(primary["net_pnl_pct"])
        return result
    # A mid-candle timeout mark cannot be resolved from 5m OHLC even when the
    # history is complete: the exact cutoff close is unobservable.
    return {"status": TIMEOUT_CLOSE_UNAVAILABLE, "model": entry_key,
            "reason": "TIMEOUT_MARK_MID_CANDLE",
            "covered_until": (required_last_open + timedelta(minutes=CANDLE_MINUTES)).isoformat(),
            "deadline": end.isoformat()}


# ─── execution feasibility ───────────────────────────────────────────────────


def _round_down_to_step(value: Decimal, step: Decimal | None):
    if step is None or step <= 0:
        return None
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def assess_execution_feasibility(signal: dict, *, as_of: datetime | None = None) -> dict:
    """Separate the modeled ``estimated_fill`` from confirmed execution.

    Re-checks the quote/order book evidence exported from Phase 1 and fails
    closed: missing/stale/corrupt evidence is ``EXECUTION_FEASIBILITY_UNKNOWN``
    (never an executable trade); known constraint violations are
    ``EXECUTION_INFEASIBLE``. Passing checks are ``EXECUTION_FEASIBILITY_MODELED``
    -- model only, because offline data can never confirm a real fill.
    """
    def unknown(*reasons):
        return {"status": EXECUTION_UNKNOWN, "confirmed_execution": False,
                "reasons": list(reasons), "evidence": evidence}

    def infeasible(*reasons):
        return {"status": EXECUTION_INFEASIBLE, "confirmed_execution": False,
                "reasons": list(reasons), "evidence": evidence}

    missing = [name for name in REQUIRED_EXECUTION_FIELDS
               if _decimal(signal.get(name)) is None]
    fill = _decimal(signal.get("estimated_fill"))
    if fill is None:
        missing.append("estimated_fill")
    latency = _decimal(signal.get("assumed_latency_ms"))
    evidence = {"assumed_latency_ms": str(latency) if latency is not None else "0",
                "assumed_latency_source": "PROVIDED" if latency is not None else "DEFAULT_ZERO",
                "missing_fields": sorted(set(missing))}
    if missing:
        return unknown("MISSING_EXECUTION_EVIDENCE")

    bid = _decimal(signal["bid"])
    ask = _decimal(signal["ask"])
    book_time_ms = _decimal(signal["book_time_ms"])
    depth = _decimal(signal["depth_bid_10bps_usdt"])
    notional = _decimal(signal["requested_notional_usdt"])
    requested_qty = _decimal(signal["requested_qty"])
    executable_qty = _decimal(signal["executable_qty"])
    min_qty = _decimal(signal["min_order_qty"])
    qty_step = _decimal(signal["qty_step"])
    min_notional = _decimal(signal["min_notional"])

    if bid is None or ask is None or bid <= 0 or ask < bid:
        return unknown("INVALID_QUOTE")
    if notional is None or notional <= 0 or min_qty is None or min_qty < 0 \
            or qty_step is None or qty_step <= 0 or min_notional is None or min_notional < 0:
        return unknown("INVALID_CONSTRAINTS")

    # Quote/orderbook timestamp + freshness, including assumed latency.
    signal_ms = _epoch_ms(ts(signal["signal_time"]))
    age_ms = D(signal_ms - int(book_time_ms)) + (latency if latency is not None else D(0))
    evidence["book_age_ms_at_signal_plus_latency"] = str(age_ms)
    if as_of is not None and int(book_time_ms) > _epoch_ms(as_of) + BOOK_FUTURE_SKEW_MS:
        return unknown("FUTURE_BOOK_SNAPSHOT")
    if int(book_time_ms) > signal_ms + BOOK_FUTURE_SKEW_MS:
        return unknown("FUTURE_BOOK_SNAPSHOT")
    if age_ms > D(BOOK_STALENESS_LIMIT_MS):
        return unknown("STALE_BOOK_SNAPSHOT")

    if fill <= 0 or fill > bid:
        return unknown("FILL_INCONSISTENT_WITH_BOOK")

    # Recalculate the quantity-first modeled order (fill feasibility semantics).
    reference = _decimal(signal.get("reference_price"))
    expected_qty = notional / reference if reference else None
    tolerance = max(qty_step, (expected_qty or D(0)) * D("1e-9"))
    if expected_qty is None or abs(requested_qty - expected_qty) > tolerance:
        return unknown("REQUESTED_QTY_INCONSISTENT")
    expected_executable = _round_down_to_step(requested_qty, qty_step)
    if expected_executable is None or expected_executable != executable_qty:
        return unknown("EXECUTABLE_QTY_INCONSISTENT")
    if executable_qty <= 0:
        return infeasible("BELOW_EXCHANGE_MIN:executable_qty_rounds_to_zero")
    if executable_qty < min_qty:
        return infeasible("BELOW_EXCHANGE_MIN:min_order_qty")
    order_value = executable_qty * fill
    evidence["estimated_order_value_usdt"] = str(order_value)
    if order_value < min_notional:
        return infeasible("BELOW_EXCHANGE_MIN:min_notional")
    if depth < order_value:
        return infeasible("INSUFFICIENT_DEPTH")

    mid = (ask + bid) / 2
    spread_pct = (ask - bid) / mid
    cost_pct = (reference - fill) / reference + spread_pct + 2 * FEE_PER_SIDE
    cost_class = ("NORMAL" if cost_pct <= NORMAL_COST
                  else "ELEVATED" if cost_pct <= ELEVATED_COST else "ABOVE_ELEVATED")
    evidence.update({
        "bid": str(bid), "ask": str(ask), "book_time_ms": str(int(book_time_ms)),
        "spread_bps": str(spread_pct * 10000),
        "reference_to_bid_bps": str((reference - bid) / reference * 10000),
        "market_impact_bps": str((bid - fill) / bid * 10000),
        "entry_slippage_bps": str((reference - fill) / reference * 10000),
        "estimated_fill": str(fill), "executable_qty": str(executable_qty),
        "depth_bid_10bps_usdt": str(depth),
        "quote_side_roundtrip_cost_pct": str(cost_pct),
        "quote_side_cost_class": cost_class,
    })
    return {"status": EXECUTION_MODELED, "confirmed_execution": False,
            "reasons": ["MODELED_CHECKS_PASS_NOT_A_REAL_FILL"], "evidence": evidence}


# ─── cost decomposition (explicit, never double-counted) ─────────────────────


def cost_decomposition(signal: dict, feasibility: dict) -> dict:
    """Five economic terms, separated; each term's accounting is declared.

    Reference-to-bid drift and market impact are already embedded in the
    executable entry price and are therefore NOT additive to ``cost_rate``;
    spread, fees and the exit-slippage allowance live once inside the frozen
    ``cost_rate`` scenarios. Missing inputs are reported unknown, never zero.
    """
    def from_evidence(key):
        value = (feasibility.get("evidence") or {}).get(key)
        return value if value is not None else None

    return {
        "reference_to_bid_drift_bps": from_evidence("reference_to_bid_bps"),
        "reference_to_bid_drift_accounting": "EMBEDDED_IN_EXECUTABLE_ENTRY_NOT_ADDITIVE",
        "market_impact_bps": from_evidence("market_impact_bps"),
        "market_impact_accounting": "EMBEDDED_IN_EXECUTABLE_ENTRY_NOT_ADDITIVE",
        "spread_bps": from_evidence("spread_bps"),
        "spread_accounting": "PART_OF_COST_RATE_ONCE",
        "fees_roundtrip_pct": str(2 * FEE_PER_SIDE),
        "fees_accounting": "PART_OF_COST_RATE_ONCE",
        "exit_slippage": "NOT_MODELED_UNKNOWN_NOT_ZERO",
        "cost_rate_normal_pct": str(NORMAL_COST),
        "cost_rate_elevated_pct": str(ELEVATED_COST),
        "double_count_policy": COST_MODEL["double_count_policy"],
    }


# ─── audit / paired / coverage / bootstrap ───────────────────────────────────


def audit(signals: list[dict], candles: list[dict], *,
          as_of: datetime | None = None) -> list[dict]:
    rows = []
    indexes: dict[str, SymbolCandles] = {}
    for sig in signals:
        symbol = sig["symbol"]
        if symbol not in indexes:
            indexes[symbol] = SymbolCandles(candles, symbol, as_of=as_of)
        index = indexes[symbol]
        try:
            key = event_key(symbol, sig["signal_time"], sig["reference_price"],
                            sig["stop_price"], sig["target_price"])
        except (ValueError, InvalidOperation, KeyError, TypeError):
            # The frozen identity needs every geometry field; a row missing one
            # of them simply has no identity and is excluded from pairing.
            key = None
        row = {"event_key": key,
               "symbol": symbol, "signal_time": sig["signal_time"]}
        for label, key in (("frozen", "reference_price"), ("executable", "estimated_fill")):
            if not sig.get(key):
                row[label] = {"status": MISSING_ENTRY, "model": key}
            else:
                row[label] = evaluate(sig, candles, entry_key=key, as_of=as_of, index=index)
        feasibility = assess_execution_feasibility(sig, as_of=as_of)
        row["execution_feasibility"] = feasibility
        row["cost_decomposition"] = cost_decomposition(sig, feasibility)
        rows.append(row)
    return rows


def paired_comparison(rows: list[dict]) -> dict:
    """Frozen vs executable pairs on one ``event_key``.

    A pair is only combined when BOTH sides carry the required data (resolved
    ``FINAL`` outcomes). Anything else is excluded with an explicit reason --
    results are never merged across a missing side.
    """
    pairs = []
    excluded_reasons: dict[str, int] = {}
    for row in rows:
        frozen, executable = row["frozen"], row["executable"]
        pair = {"event_key": row["event_key"], "symbol": row["symbol"],
                "signal_time": row["signal_time"],
                "frozen_status": frozen["status"], "executable_status": executable["status"],
                "execution_feasibility": row["execution_feasibility"]["status"]}
        for label in ("normal", "elevated"):
            pair[f"frozen_net_r_{label}"] = frozen.get(f"net_r_{label}")
            pair[f"executable_net_r_{label}"] = executable.get(f"net_r_{label}")
            if frozen["status"] == FINAL and executable["status"] == FINAL:
                pair[f"delta_net_r_{label}"] = str(
                    D(executable[f"net_r_{label}"]) - D(frozen[f"net_r_{label}"]))
            else:
                pair[f"delta_net_r_{label}"] = None
        missing = [label for label, arm in (("FROZEN", frozen), ("EXECUTABLE", executable))
                   if arm["status"] != FINAL]
        if missing:
            pair["comparable"] = False
            pair["excluded_reason"] = "+".join(
                f"{label}_DATA_MISSING:{arm['status']}"
                for label, arm in (("FROZEN", frozen), ("EXECUTABLE", executable))
                if arm["status"] != FINAL)
            excluded_reasons[pair["excluded_reason"]] = excluded_reasons.get(pair["excluded_reason"], 0) + 1
        else:
            pair["comparable"] = True
            pair["excluded_reason"] = None
        pairs.append(pair)
    comparable = [p for p in pairs if p["comparable"]]
    modeled = [p for p in comparable
               if p["execution_feasibility"] == EXECUTION_MODELED]
    summary = {
        "n_pairs_total": len(pairs),
        "n_comparable": len(comparable),
        "n_excluded": len(pairs) - len(comparable),
        "excluded_reasons": dict(sorted(excluded_reasons.items())),
        "n_comparable_execution_modeled": len(modeled),
    }
    for label in ("normal", "elevated"):
        values = [D(p[f"delta_net_r_{label}"]) for p in comparable]
        summary[f"mean_delta_net_r_{label}"] = (
            str(sum(values) / len(values)) if values else None)
    return {"pairs": pairs, "summary": summary}


def coverage_report(rows: list[dict], integrity: dict) -> dict:
    def bucket():
        return {"events": 0, "frozen_status": {}, "executable_status": {},
                "execution_feasibility": {}, "comparable_pairs": 0}

    def bump(container, key):
        container[key] = container.get(key, 0) + 1

    by_symbol: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    totals = bucket()
    for row in rows:
        day = ts(row["signal_time"]).date().isoformat()
        for group, key in ((by_symbol, row["symbol"]), (by_day, day)):
            group.setdefault(key, bucket())
        for target in (by_symbol[row["symbol"]], by_day[day], totals):
            target["events"] += 1
            bump(target["frozen_status"], row["frozen"]["status"])
            bump(target["executable_status"], row["executable"]["status"])
            bump(target["execution_feasibility"], row["execution_feasibility"]["status"])
    paired = paired_comparison(rows)
    for pair in paired["pairs"]:
        if pair["comparable"]:
            by_symbol[pair["symbol"]]["comparable_pairs"] += 1
            by_day[ts(pair["signal_time"]).date().isoformat()]["comparable_pairs"] += 1
            totals["comparable_pairs"] += 1
    return {"by_symbol": dict(sorted(by_symbol.items())),
            "by_day": dict(sorted(by_day.items())),
            "totals": totals,
            "candle_integrity": integrity}


def _cluster_key(row: dict) -> str:
    return f"{row['symbol']}|{ts(row['signal_time']).date().isoformat()}"


def _percentile(sorted_values: list[Decimal], q: float):
    if not sorted_values:
        return None
    idx = (len(sorted_values) - 1) * q
    low, high = math.floor(idx), math.ceil(idx)
    if low == high:
        return sorted_values[low]
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * D(str(idx - low))


def cluster_bootstrap(values_by_row, rows: list[dict], *, unit: str,
                      replicates: int = 2000, seed: int = 20261027) -> dict:
    """Symbol-day cluster bootstrap.

    Repeated/overlapping signals of one symbol on one UTC day stay inside a
    single cluster, so their correlation is preserved when clusters are
    resampled with replacement. Events are pooled (event-weighted) inside a
    resampled cluster draw.
    """
    clusters: dict[str, list[Decimal]] = {}
    for row, value in zip(rows, values_by_row):
        if value is None:
            continue
        clusters.setdefault(_cluster_key(row), []).append(value)
    result = {"unit": unit, "method": "CLUSTER_BOOTSTRAP_SYMBOL_UTC_DAY",
              "event_weighting": "POOLED_EVENTS_WITHIN_RESAMPLED_CLUSTERS",
              "cluster_definition": "symbol + UTC date of signal_time",
              "n_clusters": len(clusters), "n_events": sum(len(v) for v in clusters.values()),
              "replicates": replicates, "seed": seed, "mean": None,
              "ci95_low": None, "ci95_high": None}
    if not clusters:
        return result
    keys = sorted(clusters)
    rng = random.Random(seed)
    observed = [value for values in clusters.values() for value in values]
    result["mean"] = str(sum(observed) / len(observed))
    replicates_means = []
    for _ in range(replicates):
        draw = [rng.choice(keys) for _ in keys]
        pool = [value for key in draw for value in clusters[key]]
        replicates_means.append(sum(pool) / len(pool))
    replicates_means.sort()
    low = _percentile(replicates_means, 0.025)
    high = _percentile(replicates_means, 0.975)
    result["ci95_low"] = str(low) if low is not None else None
    result["ci95_high"] = str(high) if high is not None else None
    return result


def bootstrap_report(rows: list[dict], paired: dict, *,
                     replicates: int = 2000, seed: int = 20261027) -> dict:
    report: dict = {"replicates": replicates, "seed": seed, "series": {}}
    for arm in ("frozen", "executable"):
        subset = [row for row in rows if row[arm]["status"] == FINAL]
        for label in ("normal", "elevated"):
            values = [D(row[arm][f"net_r_{label}"]) for row in subset]
            report["series"][f"{arm}_net_r_{label}"] = cluster_bootstrap(
                values, subset, unit=f"{arm} {label}-cost net R over FINAL events",
                replicates=replicates, seed=seed)
    tradable = [row for row in rows
                if row["executable"]["status"] == FINAL
                and row["execution_feasibility"]["status"] == EXECUTION_MODELED]
    for label in ("normal", "elevated"):
        values = [D(row["executable"][f"net_r_{label}"]) for row in tradable]
        report["series"][f"executable_modeled_feasible_net_r_{label}"] = cluster_bootstrap(
            values, tradable,
            unit=f"executable {label}-cost net R over FINAL + EXECUTION_FEASIBILITY_MODELED events",
            replicates=replicates, seed=seed)
    comparable = [p for p in paired["pairs"] if p["comparable"]]
    for label in ("normal", "elevated"):
        values = [D(p[f"delta_net_r_{label}"]) for p in comparable]
        report["series"][f"paired_delta_net_r_{label}"] = cluster_bootstrap(
            values, comparable,
            unit=f"paired executable-frozen {label}-cost net R delta over comparable pairs",
            replicates=replicates, seed=seed)
    return report


# ─── CLI ─────────────────────────────────────────────────────────────────────


def report(signals: list[dict], candles: list[dict], *,
           as_of: datetime | None = None,
           bootstrap_replicates: int = 2000,
           bootstrap_seed: int = 20261027) -> dict:
    integrity = build_integrity(candles, sorted({s["symbol"] for s in signals}), as_of=as_of)
    rows = audit(signals, candles, as_of=as_of)
    paired = paired_comparison(rows)
    return {
        "protocol": PROTOCOL,
        "assumptions": ASSUMPTIONS,
        "cost_model": COST_MODEL,
        "candle_integrity": integrity,
        "results": rows,
        "paired": paired,
        "coverage": coverage_report(rows, integrity),
        "bootstrap": bootstrap_report(rows, paired,
                                      replicates=bootstrap_replicates,
                                      seed=bootstrap_seed),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--candles", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", type=str, default=None,
                        help="export timestamp; candles not closed by it are excluded")
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=20261027)
    args = parser.parse_args()
    with args.signals.open(newline="") as f:
        signals = list(csv.DictReader(f))
    with args.candles.open(newline="") as f:
        candles = list(csv.DictReader(f))
    as_of = ts(args.as_of) if args.as_of else None
    full = report(signals, candles, as_of=as_of,
                  bootstrap_replicates=args.bootstrap_replicates,
                  bootstrap_seed=args.bootstrap_seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(full, indent=2) + "\n", encoding="utf-8")
    rows = full["results"]
    print(json.dumps({"n": len(rows),
        "executable_final": sum(r["executable"]["status"] == FINAL for r in rows),
        "inconclusive": sum(r["executable"]["status"] != FINAL for r in rows),
        "execution_modeled": sum(r["execution_feasibility"]["status"] == EXECUTION_MODELED for r in rows),
        "execution_unknown": sum(r["execution_feasibility"]["status"] == EXECUTION_UNKNOWN for r in rows),
        "execution_infeasible": sum(r["execution_feasibility"]["status"] == EXECUTION_INFEASIBLE for r in rows),
        "comparable_pairs": full["paired"]["summary"]["n_comparable"]}))


if __name__ == "__main__":
    main()
