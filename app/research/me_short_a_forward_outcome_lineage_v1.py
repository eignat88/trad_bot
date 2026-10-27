"""Issue #208 — ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1.

Independent, strictly prospective, **observe-only** outcome + lineage
collector for the ongoing #202 Phase 1 snapshots. It keeps the frozen ME
SHORT A geometry exactly as registered by migration 071 and produces the
deterministic ``signal -> Phase 1 snapshot -> outcome`` chain that
Issue #206 / draft PR #207 need for the read-only price-alignment
counterfactual.

Frozen protocol (never retuned here):
    scanner        = MOMENTUM_EXHAUSTION
    direction      = SHORT only
    entry          = reference_price        (frozen A: max of last five 5m highs)
    stop           = invalidation_price     (absolute, > reference_price)
    target         = target_1               (absolute, < reference_price)
    max hold       = 240 minutes
    intrabar       = STOP_FIRST  (an ambiguous candle resolves to the stop)
    touch basis    = CANDLE_OHLC_TOUCH_NOT_ORDERBOOK_FILL
    exit fills     = MODELED_UNKNOWN  (never observed by this collector)
    partial fills  = UNKNOWN_NOT_INDEPENDENTLY_CAPTURED

Deterministic event identity
----------------------------
``event_key = sha256(symbol|SHORT|<utc isoformat signal_time>|<ref>|<stop>|<target>)``
is **byte-identical** to the identity frozen in migration 077 and consumed by
PR #207. All three layers therefore join on one collision-resistant key.

Safety invariants
-----------------
* No order submission is possible in this module. There is no order,
  paper, live, or direction-gate surface at all.
* Every capture is fail-closed: it requires BOTH the runtime feature flag
  ``ME_SHORT_A_LINEAGE_CAPTURE_ENABLED=1`` and a RUNNING DB activation row
  with an explicit immutable ``freeze_ts``, and every table carries
  ``CHECK (signal_time > freeze_ts)``. Historical backfill is impossible by
  construction.
* All writes are append-only and idempotent on ``event_key`` (``INSERT ...
  ON CONFLICT DO NOTHING``); nothing is ever updated or deleted.
* No-lookahead: source context may only use data closed at or before
  ``signal_time``; outcome computation may only use candles closed at or
  before ``asof_time``. Both are asserted by explicit flags and by DB
  ``CHECK`` constraints.
* Missing/unavailable data is recorded explicitly in ``missing_fields`` /
  ``source_quality_flags``. It is never imputed, and an event with missing
  source data is never given economics: it stays ``SOURCE_INCOMPLETE``.
* The closed frozen OOS (``research.prospective_observation`` /
  ``research.prospective_outcome``) and the Phase 1 evidence table
  ``research.me_short_a_fill_feasibility_v1`` are read-only for this module.
  Its ``source_observation_ids`` are never retro-fitted.

Cost model (frozen ``ME_SHORT_GEOM_A_EXECUTION_V1`` constants, used only to
express net R on the frozen structural risk):
    structural_r = stop_price - reference_price
    net_r_normal   = gross_r - NORMAL_ROUND_TRIP   * reference / structural_r
    net_r_elevated = gross_r - ELEVATED_ROUND_TRIP * reference / structural_r
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

LOG = logging.getLogger(__name__)

PROTOCOL_VERSION = "ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1"
EXPERIMENT = PROTOCOL_VERSION

SCANNER_NAME = "MOMENTUM_EXHAUSTION"
DIRECTION = "SHORT"

# Frozen execution semantics (migration 071 / ME_SHORT_GEOM_A_EXECUTION_V1).
MAX_HOLD_MINUTES = 240
INTRABAR_POLICY = "STOP_FIRST"
CANDLE_INTERVAL_MINUTES = 5
# Upper bound on the number of closed 5m candles inside the 240m horizon.
# The exact count depends on the alignment of signal_time to the 5m grid (47
# for a grid-aligned signal, 48 otherwise) and is always derived from the
# frozen window inequalities in ``window_bounds`` — never assumed.
MAX_EXPECTED_CANDLES = MAX_HOLD_MINUTES // CANDLE_INTERVAL_MINUTES  # 48

FEE_PER_SIDE = Decimal("0.00055")
NORMAL_ROUND_TRIP = Decimal("0.0021")
ELEVATED_ROUND_TRIP = Decimal("0.0031")

TOUCH_BASIS = "CANDLE_OHLC_TOUCH_NOT_ORDERBOOK_FILL"
EXIT_FILL_SEMANTICS = "MODELED_UNKNOWN"
PARTIAL_FILL_STATUS = "UNKNOWN_NOT_INDEPENDENTLY_CAPTURED"

# Lineage output statuses (exactly four, in precedence order).
STATUS_MISSING_LINEAGE = "MISSING_LINEAGE"
STATUS_OUTCOME_UNMATURED = "OUTCOME_UNMATURED"
STATUS_SOURCE_INCOMPLETE = "SOURCE_INCOMPLETE"
STATUS_MATCHED_FINALIZED = "MATCHED_FINALIZED"
LINEAGE_STATUSES = (
    STATUS_MISSING_LINEAGE,
    STATUS_OUTCOME_UNMATURED,
    STATUS_SOURCE_INCOMPLETE,
    STATUS_MATCHED_FINALIZED,
)

# Outcome statuses that may actually be written. OUTCOME_UNMATURED is a
# derived/defensive state: this collector never writes it, because a row is
# only ever created once the horizon is settled on closed candles.
OUTCOME_STATUS_SOURCE_INCOMPLETE = "SOURCE_INCOMPLETE"
OUTCOME_STATUS_MATCHED_FINALIZED = "MATCHED_FINALIZED"
OUTCOME_STATUS_UNMATURED = "OUTCOME_UNMATURED"
OUTCOME_STATUSES = (
    OUTCOME_STATUS_UNMATURED,
    OUTCOME_STATUS_SOURCE_INCOMPLETE,
    OUTCOME_STATUS_MATCHED_FINALIZED,
)

PATH_STOP_HIT = "STOP_HIT"
PATH_TARGET_HIT = "TARGET_HIT"
PATH_TIMEOUT_240M = "TIMEOUT_240M"
PATH_UNRESOLVED = "UNRESOLVED"
PATH_CLASSES = (PATH_STOP_HIT, PATH_TARGET_HIT, PATH_TIMEOUT_240M, PATH_UNRESOLVED)

# Linkage states.
LINK_PENDING = "PENDING"
LINK_LINKED = "LINKED"
LINK_QUARANTINED = "QUARANTINED"

ACTIVATION_FLAG = "ME_SHORT_A_LINEAGE_CAPTURE_ENABLED"

# ── Frozen protocol payload and hash ────────────────────────────────────
# The hash is embedded in the activation row and in every captured row so a
# verifier can prove that a row was produced by exactly this protocol.

PROTOCOL_PAYLOAD = json.dumps(
    {
        "protocol_version": PROTOCOL_VERSION,
        "scanner_name": SCANNER_NAME,
        "direction": DIRECTION,
        "entry_rule": "immediate SHORT entry = reference_price (frozen A: max(last five 5m highs))",
        "stop_rule": "stop = invalidation_price (absolute, > reference_price)",
        "target_rule": "target = target_1 (absolute, < reference_price)",
        "max_hold_minutes": MAX_HOLD_MINUTES,
        "intrabar_policy": INTRABAR_POLICY,
        "candle_interval_minutes": CANDLE_INTERVAL_MINUTES,
        "touch_basis": TOUCH_BASIS,
        "exit_fill_semantics": EXIT_FILL_SEMANTICS,
        "partial_fill_status": PARTIAL_FILL_STATUS,
        "fee_per_side": str(FEE_PER_SIDE),
        "normal_round_trip_cost": str(NORMAL_ROUND_TRIP),
        "elevated_round_trip_cost": str(ELEVATED_ROUND_TRIP),
        "event_key_formula": "sha256(symbol|SHORT|<utc-iso signal_time>|<ref>|<stop>|<target>)",
        "backfill": "FORBIDDEN",
        "orders": "NONE",
    },
    sort_keys=True,
    separators=(",", ":"),
)
PROTOCOL_HASH = hashlib.sha256(PROTOCOL_PAYLOAD.encode("utf-8")).hexdigest()


# ── small helpers ───────────────────────────────────────────────────────


def _datetime(value) -> datetime:
    dt = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _datetime(value).isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def canonical_decimal(value) -> Decimal:
    """Exact decimal used inside the event identity.

    The identity is built from ``str(Decimal(str(value)))`` so that the key is
    reproducible from the stored numeric columns without any float step.
    """
    parsed = _decimal(value)
    if parsed is None:
        raise ValueError("event identity requires a parseable decimal")
    return parsed


# ── deterministic event identity ────────────────────────────────────────


def identity_payload(symbol, direction, signal_time, reference_price,
                     stop_price, target_price) -> str:
    """The exact pre-hash payload of the frozen ``event_key``.

    Byte-identical to the formula frozen in migration 077 and used by the
    Phase 1 collector, so all three layers join on one key.
    """
    if direction != DIRECTION:
        raise ValueError(f"identity is frozen to direction {DIRECTION}")
    if not symbol:
        raise ValueError("identity requires a symbol")
    ts = _datetime(signal_time).isoformat()
    ref = canonical_decimal(reference_price)
    stop = canonical_decimal(stop_price)
    target = canonical_decimal(target_price)
    return f"{symbol}|{DIRECTION}|{ts}|{ref}|{stop}|{target}"


def event_key(symbol, direction, signal_time, reference_price, stop_price,
              target_price) -> str:
    return hashlib.sha256(
        identity_payload(symbol, direction, signal_time, reference_price,
                         stop_price, target_price).encode("utf-8")
    ).hexdigest()


def verify_identity(*, event_key_stored, symbol, direction, signal_time,
                    reference_price, stop_price, target_price,
                    protocol_version=PROTOCOL_VERSION,
                    protocol_hash=PROTOCOL_HASH,
                    expected_protocol_version=PROTOCOL_VERSION,
                    expected_protocol_hash=PROTOCOL_HASH) -> dict:
    """Recompute and verify every identity component.

    Returns ``{"event_key_recomputed", "event_key_integrity", "link_status",
    "quarantine_reason", "linkage_checks"}``. A mismatch is QUARANTINED, never
    repaired and never silently accepted.
    """
    checks: dict[str, bool] = {}
    problems: list[str] = []

    checks["direction_is_short"] = direction == DIRECTION
    if not checks["direction_is_short"]:
        problems.append("direction")

    checks["scanner_protocol_version_matches"] = (
        protocol_version == expected_protocol_version)
    if not checks["scanner_protocol_version_matches"]:
        problems.append("protocol_version")

    checks["protocol_hash_matches"] = protocol_hash == expected_protocol_hash
    if not checks["protocol_hash_matches"]:
        problems.append("protocol_hash")

    ref = _decimal(reference_price)
    stop = _decimal(stop_price)
    target = _decimal(target_price)
    checks["frozen_geometry_valid"] = bool(
        ref is not None and stop is not None and target is not None
        and ref > 0 and stop > ref and 0 < target < ref)
    if not checks["frozen_geometry_valid"]:
        problems.append("frozen_geometry")

    try:
        recomputed = event_key(symbol, direction, signal_time, ref, stop, target)
        checks["event_key_recomputes"] = True
    except Exception as exc:  # pragma: no cover - defensive
        recomputed = ""
        checks["event_key_recomputes"] = False
        problems.append(f"event_key:{type(exc).__name__}")

    integrity = bool(recomputed and event_key_stored == recomputed)
    checks["event_key_integrity"] = integrity
    if not integrity:
        problems.append("event_key_integrity")

    return {
        "event_key_recomputed": recomputed,
        "event_key_integrity": integrity,
        "link_status": LINK_LINKED if not problems else LINK_QUARANTINED,
        "quarantine_reason": None if not problems else ",".join(problems),
        "linkage_checks": checks,
    }


# ── no-lookahead / window geometry ──────────────────────────────────────

_GRID_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _grid_step() -> timedelta:
    return timedelta(minutes=CANDLE_INTERVAL_MINUTES)


def _first_grid_point_after(dt: datetime) -> datetime:
    """Smallest 5m grid point STRICTLY after ``dt``."""
    minutes = (dt - _GRID_EPOCH).total_seconds() / 60.0
    index = math.floor(minutes / CANDLE_INTERVAL_MINUTES) + 1
    return _GRID_EPOCH + timedelta(minutes=CANDLE_INTERVAL_MINUTES * index)


def _last_grid_point_before(dt: datetime) -> datetime:
    """Largest 5m grid point STRICTLY before ``dt``."""
    minutes = (dt - _GRID_EPOCH).total_seconds() / 60.0
    index = math.ceil(minutes / CANDLE_INTERVAL_MINUTES) - 1
    return _GRID_EPOCH + timedelta(minutes=CANDLE_INTERVAL_MINUTES * index)


def window_bounds(signal_time) -> tuple[datetime, datetime, datetime]:
    """``(first_open, last_open, horizon_end)`` of the frozen 240m window.

    The window is defined by the SAME inequalities the frozen evaluator uses
    in ``prospective_evaluator._check_tp_sl`` / ``_calculate_mfe_mae``: a 5m
    candle belongs to the horizon when its OPEN time ``o`` satisfies

        signal_time < o < signal_time + 240 minutes

    so the window boundary is explicit and reproducible regardless of the
    time zone or the alignment of ``signal_time`` to the 5m grid. For a
    grid-aligned signal this yields 47 candles (the +0m candle is the trigger
    candle and is excluded, exactly as in the frozen protocol); for an
    off-grid signal it yields 48.

    ``horizon_end`` is the frozen ``MAX_HOLD`` boundary. The window's last
    candle always closes at or after ``horizon_end``, and finalization is
    only allowed once that candle is closed.
    """
    start = _datetime(signal_time)
    horizon_end = start + timedelta(minutes=MAX_HOLD_MINUTES)
    return (
        _first_grid_point_after(start),
        _last_grid_point_before(horizon_end),
        horizon_end,
    )


def expected_window_opens(signal_time) -> list[datetime]:
    first_open, last_open, _ = window_bounds(signal_time)
    step = _grid_step()
    out: list[datetime] = []
    cursor = first_open
    while cursor <= last_open:
        out.append(cursor)
        cursor += step
    return out


def expected_candle_count(signal_time) -> int:
    """Number of authoritative closed 5m candles the horizon requires."""
    return len(expected_window_opens(signal_time))


def normalize_candles(candles) -> list[dict]:
    """Normalize candle rows to the internal closed-5m representation.

    Accepts dicts or objects with ``open_time``/``timestamp``/``open``/
    ``high``/``low``/``close``. Only candles that are closed at or before
    ``retrieved_at`` survive; an open or future candle is dropped and reported
    by the caller through ``missing_fields``/``no_lookahead_flags``.
    """
    out: list[dict] = []
    for raw in candles or []:
        if isinstance(raw, dict):
            get = raw.get
        else:
            get = lambda key, _r=raw: getattr(_r, key, None)  # noqa: E731
        open_raw = get("open_time")
        if open_raw is None:
            open_raw = get("timestamp")
        close_raw = get("close_time")
        open_dt = _datetime(open_raw)
        close_dt = _datetime(close_raw) if close_raw is not None else (
            open_dt + timedelta(minutes=CANDLE_INTERVAL_MINUTES))
        out.append({
            "open_time": open_dt,
            "close_time": close_dt,
            "open": _decimal(get("open") if get("open") is not None else get("open_price")),
            "high": _decimal(get("high") if get("high") is not None else get("high_price")),
            "low": _decimal(get("low") if get("low") is not None else get("low_price")),
            "close": _decimal(get("close") if get("close") is not None else get("close_price")),
            "is_closed": bool(get("is_closed", True)),
            "source": get("source") or "unspecified",
        })
    out.sort(key=lambda c: c["open_time"])
    return out


# ── outcome math (frozen STOP_FIRST semantics) ──────────────────────────


def _round_minutes(delta: timedelta) -> float:
    return round(delta.total_seconds() / 60.0, 1)


def compute_outcome(candles, *, symbol, signal_time, reference_price,
                    stop_price, target_price, asof_time,
                    source_quality_flags=None, missing_fields=None):
    """Pure, offline 240m STOP_FIRST outcome over closed 5m candles.

    Semantics (frozen):
      * the evaluation window is exactly the closed 5m candles whose OPEN
        times lie strictly inside ``(signal_time, signal_time + 240m)`` — the
        same inequality the frozen ``prospective_evaluator`` uses;
      * SHORT: a candle touches the stop when ``high >= stop_price`` and the
        target when ``low <= target_price``;
      * an ambiguous candle (both) resolves to the STOP (``STOP_FIRST``) and is
        flagged ``ambiguous_intrabar``;
      * otherwise the FIRST touch in the window decides the terminal path;
      * with no touch inside the window the path is ``TIMEOUT_240M`` and the
        exit is the close of the LAST in-window candle;
      * ``gross_r = (reference_price - exit_price) / structural_r`` so a stop
        is exactly ``-1R`` and an ambiguous candle is exactly ``-1R``;
      * ``return_at_240m`` follows the frozen direction-agnostic convention
        of ``research.prospective_outcome`` — ``(last in-window close -
        reference_price) / reference_price * 100`` — so it stays directly
        comparable with the closed frozen OOS cohort;
      * ``mae_240m``/``mfe_240m`` follow the frozen SHORT definitions.

    Every candle used must be closed at or before ``asof_time``; otherwise the
    result is ``SOURCE_INCOMPLETE`` with no economics at all.
    """
    symbol = str(symbol)
    ref = canonical_decimal(reference_price)
    stop = canonical_decimal(stop_price)
    target = canonical_decimal(target_price)
    if not (ref > 0 and stop > ref and 0 < target < ref):
        raise ValueError("invalid frozen SHORT geometry")
    signal_dt = _datetime(signal_time)
    asof_dt = _datetime(asof_time)
    structural_r = stop - ref

    first_open, last_open, horizon_end = window_bounds(signal_dt)
    expected = expected_window_opens(signal_dt)
    expected_set = set(expected)

    normalized = normalize_candles(candles)
    all_in_window = [
        c for c in normalized
        if first_open <= c["open_time"] <= last_open
    ]
    extra = [c for c in normalized if c["open_time"] not in expected_set]

    flags: list[str] = list(source_quality_flags or [])
    missing_list: list[str] = list(missing_fields or [])

    # No-lookahead: only candles already CLOSED at the decision ASOF may be
    # used. Anything open or closing after the ASOF is unusable and is
    # reported, never silently dropped into the result.
    not_closed = [c for c in all_in_window if not c["is_closed"]]
    future = [c for c in all_in_window if c["close_time"] > asof_dt]
    if not_closed:
        flags.append("OPEN_CANDLE_IN_WINDOW")
    if future:
        flags.append("CANDLE_CLOSE_AFTER_ASOF")
    if extra:
        flags.append("CANDLES_OUTSIDE_WINDOW_IGNORED")

    usable = [
        c for c in all_in_window
        if c["is_closed"] and c["close_time"] <= asof_dt
    ]
    present_opens = {c["open_time"] for c in usable}
    missing = [ts for ts in expected if ts not in present_opens]

    candles_present = len(usable)
    expected_count = len(expected)
    coverage_complete = (candles_present == expected_count)
    if candles_present < expected_count:
        missing_list.append("post_signal_candle_highs_240m:incomplete_window")
    max_close = max((c["close_time"] for c in usable), default=None)
    asof_ok = bool(max_close is not None and max_close <= asof_dt)

    base = {
        "symbol": symbol,
        "signal_time": signal_dt,
        "horizon_end_time": horizon_end,
        "horizon_minutes": MAX_HOLD_MINUTES,
        "intrabar_policy": INTRABAR_POLICY,
        "reference_price": ref,
        "stop_price": stop,
        "target_price": target,
        "structural_r": structural_r,
        "candles_expected": expected_count,
        "candles_present": candles_present,
        "candles_missing": [_datetime(ts).isoformat() for ts in missing],
        "coverage_complete": coverage_complete,
        "max_candle_close_time": max_close,
        "asof_time": asof_dt,
        "asof_ok": asof_ok,
        "touch_basis": TOUCH_BASIS,
        "exit_fill_semantics": EXIT_FILL_SEMANTICS,
        "partial_fill_status": PARTIAL_FILL_STATUS,
        "source_quality_flags": flags,
        "missing_fields": missing_list,
    }

    matured = asof_dt >= horizon_end

    # ── candle-touch observables (safe to report even when incomplete) ──
    highs = [c["high"] for c in usable if c["high"] is not None]
    lows = [c["low"] for c in usable if c["low"] is not None]
    max_high = max(highs) if highs else None
    min_low = min(lows) if lows else None
    base["limit_at_reference_touch"] = bool(max_high is not None and max_high >= ref)
    base["mae_240m"] = (
        (max_high - ref) / ref * 100 if max_high is not None else None)
    base["mfe_240m"] = (
        (ref - min_low) / ref * 100 if min_low is not None else None)

    # ── unmatured: nothing is settled yet ───────────────────────────────
    if not matured:
        base.update({
            "outcome_status": OUTCOME_STATUS_UNMATURED,
            "path_class": None,
            "sl_before_tp": False,
            "tp_before_sl": False,
            "ambiguous_intrabar": False,
            "time_to_sl_minutes": None,
            "time_to_tp_minutes": None,
            "exit_price": None,
            "exit_basis": None,
            "gross_r": None,
            "net_r_normal": None,
            "net_r_elevated": None,
            "return_at_240m": None,
            "is_final": False,
            "finalized_at": None,
            "finalization_candle_close_time": None,
        })
        return base

    # ── matured but source-incomplete: never produce economics ─────────
    if not (coverage_complete and asof_ok):
        base.update({
            "outcome_status": OUTCOME_STATUS_SOURCE_INCOMPLETE,
            "path_class": None,
            "sl_before_tp": False,
            "tp_before_sl": False,
            "ambiguous_intrabar": False,
            "time_to_sl_minutes": None,
            "time_to_tp_minutes": None,
            "exit_price": None,
            "exit_basis": None,
            "gross_r": None,
            "net_r_normal": None,
            "net_r_elevated": None,
            "return_at_240m": None,
            "is_final": False,
            "finalized_at": None,
            "finalization_candle_close_time": None,
        })
        return base

    # ── settled and complete: resolve the frozen STOP_FIRST terminal path ──
    path_class = PATH_TIMEOUT_240M
    sl_before_tp = False
    tp_before_sl = False
    ambiguous = False
    time_to_sl = None
    time_to_tp = None
    exit_price = None
    exit_basis = "LAST_IN_WINDOW_5M_CLOSE"

    for candle in usable:
        stop_now = candle["high"] is not None and candle["high"] >= stop
        target_now = candle["low"] is not None and candle["low"] <= target
        if not (stop_now or target_now):
            continue
        minutes = _round_minutes(candle["open_time"] - signal_dt)
        if stop_now and target_now:
            ambiguous = True
            sl_before_tp = True
            tp_before_sl = False
            path_class = PATH_STOP_HIT
            exit_price = stop
            exit_basis = "INTRABAR_STOP_FIRST"
            time_to_sl = minutes
            time_to_tp = minutes
        elif stop_now:
            sl_before_tp = True
            path_class = PATH_STOP_HIT
            exit_price = stop
            exit_basis = "STOP_LEVEL"
            time_to_sl = minutes
        else:
            tp_before_sl = True
            path_class = PATH_TARGET_HIT
            exit_price = target
            exit_basis = "TARGET_LEVEL"
            time_to_tp = minutes
        break

    if path_class == PATH_TIMEOUT_240M:
        last_candle = usable[-1]
        exit_price = last_candle["close"]
        if exit_price is None:
            base["missing_fields"] = missing_list + ["timeout_close_price"]
            base.update({
                "outcome_status": OUTCOME_STATUS_SOURCE_INCOMPLETE,
                "path_class": None,
                "sl_before_tp": False,
                "tp_before_sl": False,
                "ambiguous_intrabar": False,
                "time_to_sl_minutes": None,
                "time_to_tp_minutes": None,
                "exit_price": None,
                "exit_basis": None,
                "gross_r": None,
                "net_r_normal": None,
                "net_r_elevated": None,
                "return_at_240m": None,
                "is_final": False,
                "finalized_at": None,
                "finalization_candle_close_time": None,
            })
            return base

    gross_r = (ref - exit_price) / structural_r
    cost_normal_r = NORMAL_ROUND_TRIP * ref / structural_r
    cost_elevated_r = ELEVATED_ROUND_TRIP * ref / structural_r
    close_240m = usable[-1]["close"]

    base.update({
        "outcome_status": OUTCOME_STATUS_MATCHED_FINALIZED,
        "path_class": path_class,
        "sl_before_tp": sl_before_tp,
        "tp_before_sl": tp_before_sl,
        "ambiguous_intrabar": ambiguous,
        "time_to_sl_minutes": time_to_sl,
        "time_to_tp_minutes": time_to_tp,
        "exit_price": exit_price,
        "exit_basis": exit_basis,
        "gross_r": gross_r,
        "net_r_normal": gross_r - cost_normal_r,
        "net_r_elevated": gross_r - cost_elevated_r,
        # Frozen, direction-agnostic convention of research.prospective_outcome
        # .return_at_240m: (last in-window close - entry) / entry * 100. For a
        # SHORT a favourable move therefore shows up negative, exactly as in
        # the closed frozen OOS cohort this must stay comparable with.
        "return_at_240m": (
            (close_240m - ref) / ref * 100 if close_240m is not None else None),
        "is_final": True,
        "finalized_at": asof_dt,
        "finalization_candle_close_time": max(
            horizon_end, usable[-1]["close_time"]),
    })
    return base


# ── fail-closed DB gate ─────────────────────────────────────────────────


def _activation(conn, experiment=EXPERIMENT):
    """Return ``(status, freeze_ts)`` or ``None`` when not capturable."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT status,freeze_ts FROM research.me_short_a_lineage_activation_v1 "
            "WHERE protocol_version=%s",
            (experiment,))
        row = cursor.fetchone()
    finally:
        cursor.close()
    if not row or row[0] != "RUNNING" or row[1] is None:
        return None
    return row[0], _datetime(row[1])


def _gated(conn, symbol, direction, signal_time):
    """Fail-closed pre-check shared by every capture entry point."""
    if os.environ.get(ACTIVATION_FLAG) != "1":
        return None
    if symbol is None or direction != DIRECTION:
        return None
    if conn is None:
        return None
    activation = _activation(conn)
    if activation is None:
        return None
    _, freeze = activation
    event_time = _datetime(signal_time)
    if event_time <= freeze:
        return None
    return freeze


# ── append-only writers (idempotent, INSERT-only) ───────────────────────


def _insert(conn, sql, params) -> bool:
    """Execute an idempotent append-only INSERT; True only for a fresh row."""
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - defensive
            LOG.debug("ME SHORT A lineage rollback failed", exc_info=True)
        LOG.exception("ME SHORT A forward lineage write failed closed; no orders")
        return False
    finally:
        cursor.close()


def capture_source_signal(conn, *, symbol, signal_time, reference_price,
                          stop_price, target_price,
                          trigger_5m_close=None, entry_zone_low=None,
                          entry_zone_high=None, signal_candle_open_time=None,
                          signal_candle_close_time=None,
                          recent_high_window=None, market_context=None,
                          signal_time_quote=None, missing_fields=None,
                          source_quality_flags=None, no_lookahead_flags=None,
                          source_observation_ids=None,
                          protocol_version=PROTOCOL_VERSION,
                          protocol_hash=PROTOCOL_HASH):
    """Append the forward-only source signal detail for one event.

    The values that PR #207 reported as MISSING from Phase 1 evidence
    (``trigger_5m_close``, ``entry_zone_low``/``entry_zone_high``, the five 5m
    candles behind ``reference_price``) are recorded here. Anything that was
    unavailable MUST be listed in ``missing_fields`` — it is never imputed.

    Returns True only when a new row was inserted.
    """
    freeze = _gated(conn, symbol, DIRECTION, signal_time)
    if freeze is None:
        return False
    ref = canonical_decimal(reference_price)
    stop = canonical_decimal(stop_price)
    target = canonical_decimal(target_price)
    if not (ref > 0 and stop > ref and 0 < target < ref):
        return False
    event_time = _datetime(signal_time)
    key = event_key(symbol, DIRECTION, event_time, ref, stop, target)

    window = list(recent_high_window or [])
    window_start = None
    window_end = None
    if window:
        try:
            normalized_window = normalize_candles(window)
        except Exception:
            LOG.exception("ME SHORT A source window is not parseable; rejected")
            return False
        if len(normalized_window) != len(window):
            return False
        window_start = min(c["open_time"] for c in normalized_window)
        window_end = max(c["close_time"] for c in normalized_window)
        # No-lookahead is a HARD REJECT: a reference window that closes after
        # the signal time would leak future information into the frozen A
        # reference_price and can never enter the append-only evidence.
        if window_end > event_time:
            LOG.warning(
                "ME SHORT A source window rejected (lookahead): event=%s "
                "window_end=%s signal_time=%s", key, window_end, event_time)
            return False
    if signal_candle_close_time is not None:
        if _datetime(signal_candle_close_time) > event_time:
            return False

    flags = list(source_quality_flags or [])
    no_lookahead = list(no_lookahead_flags or [])
    missing = list(missing_fields or [])
    for name, value in (("trigger_5m_close", trigger_5m_close),
                        ("entry_zone_low", entry_zone_low),
                        ("entry_zone_high", entry_zone_high)):
        if value is None:
            missing.append(name)
    if not window:
        missing.append("recent_high_window_candles")
        no_lookahead.append("recent_high_window_unavailable")

    sql = (
        "INSERT INTO research.me_short_a_source_signal_v1 "
        "(event_key,protocol_version,protocol_hash,symbol,direction,scanner_name,"
        "signal_time,captured_at,freeze_ts,reference_price,stop_price,target_price,"
        "structural_r,trigger_5m_close,entry_zone_low,entry_zone_high,"
        "signal_candle_open_time,signal_candle_close_time,"
        "recent_high_window,recent_high_window_candles,recent_high_window_start,"
        "recent_high_window_end,market_context,signal_time_quote,"
        "missing_fields,source_quality_flags,no_lookahead_flags,"
        "source_observation_ids) "
        "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        "%s::jsonb,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb "
        "WHERE EXISTS (SELECT 1 FROM research.me_short_a_lineage_activation_v1 "
        "WHERE protocol_version=%s AND status='RUNNING' AND freeze_ts=%s) "
        "ON CONFLICT(event_key) DO NOTHING"
    )
    params = (
        key, protocol_version, protocol_hash, symbol, DIRECTION, SCANNER_NAME,
        event_time, datetime.now(timezone.utc), freeze,
        ref, stop, target, stop - ref,
        _decimal(trigger_5m_close), _decimal(entry_zone_low), _decimal(entry_zone_high),
        _datetime(signal_candle_open_time) if signal_candle_open_time else None,
        _datetime(signal_candle_close_time) if signal_candle_close_time else None,
        json.dumps(_jsonable(window)),
        len(window) if window else None,
        window_start, window_end,
        json.dumps(_jsonable(market_context or {})),
        json.dumps(_jsonable(signal_time_quote or {})),
        json.dumps(_jsonable(missing)),
        json.dumps(_jsonable(flags)),
        json.dumps(_jsonable(no_lookahead)),
        json.dumps(_jsonable(source_observation_ids)) if source_observation_ids else None,
        EXPERIMENT, freeze,
    )
    return _insert(conn, sql, params)


def capture_post_signal_candle(conn, *, event_key_value=None, symbol,
                               signal_time, reference_price, stop_price,
                               target_price, candle):
    """Append one CLOSED post-signal 5m candle of provenance.

    Only closed candles strictly after ``signal_time`` are accepted; the DB
    enforces ``candle_close_time <= retrieved_at`` so no future data can ever
    enter. A level touch recorded here is a CANDLE touch, never a fill.

    Returns True only when a new row was inserted.
    """
    freeze = _gated(conn, symbol, DIRECTION, signal_time)
    if freeze is None:
        return False
    ref = canonical_decimal(reference_price)
    stop = canonical_decimal(stop_price)
    target = canonical_decimal(target_price)
    event_time = _datetime(signal_time)
    key = event_key_value or event_key(symbol, DIRECTION, event_time, ref, stop, target)

    row = normalize_candles([candle])
    if not row:
        return False
    c = row[0]
    if not c["is_closed"] or c["open_time"] <= event_time:
        return False
    if c["close_time"] > datetime.now(timezone.utc):
        return False
    if any(c[name] is None for name in ("open", "high", "low", "close")):
        return False

    sql = (
        "INSERT INTO research.me_short_a_post_signal_candle_v1 "
        "(event_key,candle_open_time,candle_close_time,symbol,signal_time,freeze_ts,"
        "open_price,high_price,low_price,close_price,is_closed,source,retrieved_at,"
        "high_touches_stop,low_touches_target,high_touches_reference,touch_semantics) "
        "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s "
        "WHERE EXISTS (SELECT 1 FROM research.me_short_a_lineage_activation_v1 "
        "WHERE protocol_version=%s AND status='RUNNING' AND freeze_ts=%s) "
        "ON CONFLICT(event_key,candle_open_time) DO NOTHING"
    )
    params = (
        key, c["open_time"], c["close_time"], symbol, event_time, freeze,
        c["open"], c["high"], c["low"], c["close"], True, c["source"],
        datetime.now(timezone.utc),
        c["high"] >= stop, c["low"] <= target, c["high"] >= ref, TOUCH_BASIS,
        EXPERIMENT, freeze,
    )
    return _insert(conn, sql, params)


def record_event_lineage(conn, *, symbol, signal_time, reference_price,
                         stop_price, target_price, event_key_stored=None,
                         source_observation_id=None, phase1_event_key=None,
                         phase1_observation_id=None,
                         phase1_source_observation_ids=None,
                         source_signal_present=True,
                         protocol_version=PROTOCOL_VERSION,
                         protocol_hash=PROTOCOL_HASH,
                         expected_protocol_version=PROTOCOL_VERSION,
                         expected_protocol_hash=PROTOCOL_HASH):
    """Append the deterministic linkage record for one event.

    The identity is recomputed from the supplied components and compared with
    the stored key; a mismatch QUARANTINES the record instead of linking it.
    Phase 1 linkage is only ever asserted when a Phase 1 row with the SAME
    ``event_key`` is actually observed — no Phase 1 row is ever created,
    updated, or retro-fitted here.

    Returns True only when a new row was inserted.
    """
    freeze = _gated(conn, symbol, DIRECTION, signal_time)
    if freeze is None:
        return False
    ref = canonical_decimal(reference_price)
    stop = canonical_decimal(stop_price)
    target = canonical_decimal(target_price)
    event_time = _datetime(signal_time)
    key = event_key(symbol, DIRECTION, event_time, ref, stop, target)
    stored = event_key_stored or key

    verification = verify_identity(
        event_key_stored=stored, symbol=symbol, direction=DIRECTION,
        signal_time=event_time, reference_price=ref, stop_price=stop,
        target_price=target, protocol_version=protocol_version,
        protocol_hash=protocol_hash,
        expected_protocol_version=expected_protocol_version,
        expected_protocol_hash=expected_protocol_hash)

    link_status = verification["link_status"]
    reason = verification["quarantine_reason"]
    checks = dict(verification["linkage_checks"])
    phase1_linked = False
    if phase1_event_key is not None:
        phase1_linked = phase1_event_key == key
        checks["phase1_event_key_matches"] = phase1_linked
        if not phase1_linked:
            link_status = LINK_QUARANTINED
            reason = ",".join(filter(None, [reason, "phase1_event_key"]))
    checks["source_signal_present"] = bool(source_signal_present)
    if link_status == LINK_LINKED and not source_signal_present:
        link_status = LINK_PENDING
        reason = "source_signal_missing"
    if link_status != LINK_LINKED:
        phase1_linked = False

    sql = (
        "INSERT INTO research.me_short_a_event_lineage_v1 "
        "(event_key,protocol_version,protocol_hash,symbol,direction,signal_time,"
        "captured_at,freeze_ts,reference_price,stop_price,target_price,"
        "event_key_recomputed,event_key_integrity,link_status,quarantine_reason,"
        "linkage_checks,source_signal_present,source_observation_id,"
        "phase1_event_key,phase1_linked,phase1_observation_id) "
        "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s "
        "WHERE EXISTS (SELECT 1 FROM research.me_short_a_lineage_activation_v1 "
        "WHERE protocol_version=%s AND status='RUNNING' AND freeze_ts=%s) "
        "ON CONFLICT(event_key) DO NOTHING"
    )
    params = (
        key, protocol_version, protocol_hash, symbol, DIRECTION, event_time,
        datetime.now(timezone.utc), freeze, ref, stop, target,
        verification["event_key_recomputed"], verification["event_key_integrity"],
        link_status, reason, json.dumps(_jsonable(checks)),
        bool(source_signal_present), source_observation_id,
        (key if phase1_linked else None), phase1_linked,
        (phase1_observation_id if phase1_linked else None),
        EXPERIMENT, freeze,
    )
    return _insert(conn, sql, params)


def finalize_outcome(conn, *, symbol, signal_time, reference_price,
                     stop_price, target_price, asof_time, candles,
                     source_observation_id=None, source_quality_flags=None,
                     missing_fields=None, protocol_version=PROTOCOL_VERSION,
                     protocol_hash=PROTOCOL_HASH):
    """Settle the 240m STOP_FIRST outcome and append it — exactly once.

    A row is written only after the horizon is settled on CLOSED 5m candles:
    * ``OUTCOME_UNMATURED`` is never written (nothing is settled yet);
    * a matured event with incomplete source becomes ``SOURCE_INCOMPLETE``
      and carries NO economics at all;
    * a complete, ASOF-clean event becomes ``MATCHED_FINALIZED`` with
      ``is_final`` and the frozen STOP_FIRST terminal path.

    Idempotent: replaying the same event inserts nothing and returns False.

    Returns True only when a new row was inserted.
    """
    freeze = _gated(conn, symbol, DIRECTION, signal_time)
    if freeze is None:
        return False
    ref = canonical_decimal(reference_price)
    stop = canonical_decimal(stop_price)
    target = canonical_decimal(target_price)
    event_time = _datetime(signal_time)
    key = event_key(symbol, DIRECTION, event_time, ref, stop, target)

    outcome = compute_outcome(
        candles, symbol=symbol, signal_time=event_time,
        reference_price=ref, stop_price=stop, target_price=target,
        asof_time=asof_time, source_quality_flags=source_quality_flags,
        missing_fields=missing_fields)

    # Never persist an unsettled horizon: no premature finalization and no
    # OUTCOME_UNMATURED rows from this collector.
    if outcome["outcome_status"] == OUTCOME_STATUS_UNMATURED:
        return False

    sql = (
        "INSERT INTO research.me_short_a_outcome_observation_v1 "
        "(event_key,protocol_version,protocol_hash,symbol,direction,signal_time,"
        "freeze_ts,reference_price,stop_price,target_price,structural_r,"
        "horizon_minutes,horizon_end_time,intrabar_policy,outcome_status,"
        "path_class,sl_before_tp,tp_before_sl,ambiguous_intrabar,"
        "time_to_sl_minutes,time_to_tp_minutes,exit_price,exit_basis,gross_r,"
        "net_r_normal,net_r_elevated,return_at_240m,mae_240m,mfe_240m,"
        "limit_at_reference_touch,touch_basis,exit_fill_semantics,"
        "partial_fill_status,is_final,finalized_at,finalization_candle_close_time,"
        "asof_time,max_candle_close_time,candles_expected,candles_present,"
        "candles_missing,coverage_complete,asof_ok,source_quality_flags,"
        "missing_fields,source_observation_id) "
        "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        "%s::jsonb,%s,%s,%s::jsonb,%s::jsonb,%s "
        "WHERE EXISTS (SELECT 1 FROM research.me_short_a_lineage_activation_v1 "
        "WHERE protocol_version=%s AND status='RUNNING' AND freeze_ts=%s) "
        "ON CONFLICT(event_key) DO NOTHING"
    )
    params = (
        key, protocol_version, protocol_hash, symbol, DIRECTION, event_time,
        freeze, ref, stop, target, outcome["structural_r"],
        outcome["horizon_minutes"], outcome["horizon_end_time"],
        outcome["intrabar_policy"], outcome["outcome_status"],
        outcome["path_class"], outcome["sl_before_tp"], outcome["tp_before_sl"],
        outcome["ambiguous_intrabar"], outcome["time_to_sl_minutes"],
        outcome["time_to_tp_minutes"], outcome["exit_price"],
        outcome["exit_basis"], outcome["gross_r"], outcome["net_r_normal"],
        outcome["net_r_elevated"], outcome["return_at_240m"],
        outcome["mae_240m"], outcome["mfe_240m"],
        outcome["limit_at_reference_touch"], TOUCH_BASIS, EXIT_FILL_SEMANTICS,
        PARTIAL_FILL_STATUS, outcome["is_final"], outcome["finalized_at"],
        outcome["finalization_candle_close_time"], outcome["asof_time"],
        outcome["max_candle_close_time"], outcome["candles_expected"],
        outcome["candles_present"],
        json.dumps(_jsonable(outcome["candles_missing"])),
        outcome["coverage_complete"], outcome["asof_ok"],
        json.dumps(_jsonable(outcome["source_quality_flags"])),
        json.dumps(_jsonable(outcome["missing_fields"])),
        source_observation_id,
        EXPERIMENT, freeze,
    )
    return _insert(conn, sql, params)
