"""Shared offline fixtures for the Issue #324 H1-E test suite.

SYNTHETIC TEST FIXTURES ONLY — zero network, zero production DB.  Every
value here is deterministic and labelled ``SYNTHETIC_TEST_FIXTURE``.

Provides:

* ``make_source_row``  — a valid #305 ``source_event`` mapping with the
  deterministic key / lineage-hash derivations (mirrors of the frozen
  #305 functions), plus corruption overrides for fail-closed tests;
* ``zigzag_rows``      — synthetic 4H/1D OHLCV rows engineered to classify
  into a chosen H1 regime (strict zigzag pivots, no extra swings);
* ``StubTransport``    — programmable offline HTF transport (payloads and
  typed failures: limiter / 429 / 10006 / breaker / timeout / API);
* ``FakeReader``       — capture-ordered #305 delivery fake;
* ``make_classification_record`` — deterministic classification records.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from app.research.me_entry_quality_h1c_v1.event_adapter import parse_utc_us

from app.research.me_entry_quality_h1e_v1.classification import (
    ClassificationRecord,
    H1E_CLASSIFICATION_VERSION,
    HtfPayload,
    STATUS_CLASSIFIED,
)
from app.research.me_entry_quality_h1e_v1.contracts import (
    canonical_lineage_hash,
    expected_source_event_key,
    preserved_identity_hash,
)

# ─── deterministic time anchors (synthetic) ─────────────────────────────────

DAY_US = 86_400 * 1_000_000
H4_US = 4 * 3_600 * 1_000_000
DAY_MS = 86_400 * 1_000
H4_MS = 4 * 3_600 * 1_000

#: synthetic H1-E fixture freeze (2027-01-01T00:00:00Z) — NOT a real freeze
FIXTURE_FREEZE_US = 1_798_761_600_000_000

#: synthetic day anchor for the HTF windows (2027-02-01T00:00:00Z)
FIXTURE_DAY0_MS = 1_801_440_000_000

#: the ASOF of the fixture events: close of the 60th daily candle
FIXTURE_ASOF_US = (FIXTURE_DAY0_MS + 60 * DAY_MS) * 1000

FIXTURE_SYMBOL = "FIXTUSDT"
FIXTURE_FINGERPRINT = "f" * 64
FIXTURE_LABEL = "SYNTHETIC_TEST_FIXTURE"

SOURCE_SCANNER = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def us_to_datetime(us: int) -> datetime:
    """Exact integer µs -> aware datetime (microsecond resolution)."""
    return _EPOCH + timedelta(microseconds=int(us))


# ─── #305 source rows ───────────────────────────────────────────────────────


def source_identity_for(
    symbol: str = FIXTURE_SYMBOL, candle_open_ms: int = 0, entry_timeframe: str = "5m",
) -> str:
    return (
        f"{SOURCE_SCANNER}|{symbol}|LONG|{entry_timeframe}|{int(candle_open_ms)}"
    )


def make_source_row(
    *,
    symbol: str = FIXTURE_SYMBOL,
    candle_open_ms: int | None = None,
    signal_time_us: int = FIXTURE_ASOF_US,
    captured_us: int | None = None,
    source_freeze_us: int = FIXTURE_FREEZE_US,
    fingerprint: str = FIXTURE_FINGERPRINT,
    identity: str | None = None,
    source_event_key: str | None = None,
    lineage_overrides: Mapping[str, Any] | None = None,
    corrupt: str = "",
) -> dict[str, Any]:
    """Build ONE valid (or deliberately corrupted) #305 source row.

    ``corrupt`` injects a specific violation: ``lineage_hash`` (wrong hash),
    ``identity_key`` (key not derived from identity), ``fingerprint``
    (empty), ``lineage_obligation`` (lineage disagrees with row identity).
    """
    if candle_open_ms is None:
        candle_open_ms = int(signal_time_us // 1000) // 300_000 * 300_000
    if captured_us is None:
        captured_us = signal_time_us + 5_000_000
    norm_identity = identity or source_identity_for(symbol, candle_open_ms)
    key = source_event_key or expected_source_event_key(norm_identity)
    lineage: dict[str, Any] = {
        "source_identity": norm_identity,
        "source_setup_id": "setup-1",
        "source_scanner": SOURCE_SCANNER,
        "source_scanner_version": "2.0.0",
        "source_direction": "LONG",
        "source_signal_time": "2027-02-01T00:00:00+00:00",
        "source_signal_candle_open_time": int(candle_open_ms),
        "source_entry_timeframe": "5m",
        "source_features": {
            "rsi_14": 42.0,
            "rsi_delta_3": 1.5,
            "entry_confirmation_rsi_rising": True,
        },
        "source_pipeline_fingerprint": fingerprint,
    }
    if lineage_overrides:
        lineage.update(lineage_overrides)

    row: dict[str, Any] = {
        "source_event_key": key,
        "source_identity": norm_identity,
        "symbol": symbol,
        "signal_time": us_to_datetime(signal_time_us),
        "captured_at": us_to_datetime(captured_us),
        "activation_freeze_ts": us_to_datetime(source_freeze_us),
        "signal_candle_open_time": int(candle_open_ms),
        "entry_timeframe": "5m",
        "source_fingerprint": fingerprint,
        "features": dict(lineage["source_features"]),
        "lineage": lineage,
        "lineage_hash": canonical_lineage_hash(lineage),
    }

    if corrupt == "lineage_hash":
        row["lineage_hash"] = "0" * 64
    elif corrupt == "identity_key":
        row["source_event_key"] = "1" * 64
    elif corrupt == "fingerprint":
        row["source_fingerprint"] = ""
    elif corrupt == "lineage_obligation":
        row["lineage"] = {**lineage, "source_identity": "MISMATCH"}
        row["lineage_hash"] = canonical_lineage_hash(row["lineage"])
    elif corrupt:
        raise ValueError(f"unknown corrupt mode {corrupt!r}")
    return row


def preserved_hash_of(row: Mapping[str, Any]) -> str:
    return preserved_identity_hash(
        source_event_key=str(row["source_event_key"]),
        source_identity=str(row["source_identity"]),
        signal_time_us=parse_utc_us(row["signal_time"], field_name="signal_time"),
        lineage_hash=str(row["lineage_hash"]),
        source_fingerprint=str(row["source_fingerprint"]),
    )


# ─── synthetic HTF rows (regime-engineered zigzag) ──────────────────────────

#: pivot layouts (index, center price) inside a 60-candle window:
#: swing highs at 10 / 30, swing lows at 20 / 40 (strict zigzag, k = 3).
REGIME_PIVOTS: dict[str, tuple[tuple[int, float], ...]] = {
    "BULLISH": ((0, 100.0), (10, 110.0), (20, 95.0), (30, 120.0),
                (40, 105.0), (59, 115.0)),
    "BEARISH": ((0, 100.0), (10, 110.0), (20, 95.0), (30, 100.0),
                (40, 85.0), (59, 90.0)),
    "NEUTRAL": ((0, 100.0), (10, 110.0), (20, 95.0), (30, 120.0),
                (40, 85.0), (59, 90.0)),
}


def _centers(n: int, pivots: Sequence[tuple[int, float]]) -> list[float]:
    pts = sorted(pivots)
    values: list[float] = []
    for i in range(n):
        placed = False
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= i <= x1:
                if x1 == x0:
                    values.append(float(y0))
                else:
                    t = (i - x0) / (x1 - x0)
                    values.append(float(y0 + (y1 - y0) * t))
                placed = True
                break
        if not placed:
            values.append(float(pts[-1][1]))
    return values


def zigzag_rows(
    regime: str,
    *,
    n: int = 60,
    timeframe_ms: int = DAY_MS,
    first_open_ms: int | None = None,
    spread: float = 0.5,
    extra_future_rows: int = 0,
) -> list[dict[str, Any]]:
    """Synthetic closed OHLCV rows engineered for one H1 regime.

    ``open == close == center``, ``high/low`` are a constant spread around
    the center so the ``high`` and ``low`` series mirror the strict zigzag
    pivots (exactly two swing highs at 10/30 and two swing lows at 20/40).
    """
    if regime not in REGIME_PIVOTS:
        raise ValueError(f"unknown regime {regime!r}")
    if first_open_ms is None:
        # end the window exactly at the fixture ASOF close
        first_open_ms = (
            FIXTURE_ASOF_US // 1000
            - (n + extra_future_rows) * timeframe_ms
        )
    centers = _centers(n + extra_future_rows, REGIME_PIVOTS[regime])
    rows: list[dict[str, Any]] = []
    for i in range(n + extra_future_rows):
        center = centers[i]
        rows.append({
            "open_ms": int(first_open_ms + i * timeframe_ms),
            "open": center,
            "high": center + spread,
            "low": center - spread,
            "close": center,
            "volume": 1.0,
        })
    return rows


def timeframe_payload(
    rows: Sequence[Mapping[str, Any]],
    *,
    interval: str,
    label: str = "offline-mock",
    fixture_label: str = FIXTURE_LABEL,
    fetched_at_ms: int | None = None,
) -> HtfPayload:
    return HtfPayload(
        rows=tuple(dict(row) for row in rows),
        source_kind="OFFLINE_SNAPSHOT",
        source_label=label,
        fixture_label=fixture_label,
        fetched_at_ms=fetched_at_ms,
    )


# ─── offline collaborators ──────────────────────────────────────────────────


class FakeReader:
    """Capture-ordered #305 delivery fake (cursor semantics included)."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.rows = [dict(row) for row in rows]

    def read_after(
        self,
        cursor: Any,
        *,
        limit: int,
        ceiling_captured_us: int | None = None,
    ) -> list[dict[str, Any]]:
        cursor_tuple = cursor.as_tuple()
        selected: list[dict[str, Any]] = []
        for row in sorted(
            self.rows,
            key=lambda r: (
                parse_utc_us(r["captured_at"], field_name="captured_at"),
                str(r.get("source_event_key") or ""),
            ),
        ):
            key = str(row.get("source_event_key") or "")
            row_cursor = (
                parse_utc_us(row["captured_at"], field_name="captured_at"), key)
            if row_cursor <= cursor_tuple:
                continue
            if (
                ceiling_captured_us is not None
                and parse_utc_us(row["captured_at"], field_name="captured_at")
                > int(ceiling_captured_us)
            ):
                continue
            selected.append(row)
        return selected[: int(limit)]


class StubTransport:
    """Programmable offline HTF transport.

    ``program`` maps an interval to a sequence of steps; each step is
    either an :class:`HtfPayload` or an EXCEPTION INSTANCE to raise.  The
    last step repeats for subsequent calls.  ``calls`` counts every
    invocation (bounded-work assertions).
    """

    def __init__(
        self,
        program: Mapping[str, Sequence[Any]] | None = None,
        *,
        default: Mapping[str, HtfPayload] | None = None,
    ) -> None:
        self._program: dict[str, list[Any]] = {
            interval: list(steps) for interval, steps in (program or {}).items()
        }
        self._default = dict(default or {})
        self.calls: list[tuple[str, int]] = []

    def fetch_htf_window(
        self,
        *,
        symbol: str,
        interval: str,
        asof_us: int,
        lookback: int = 60,
    ) -> HtfPayload:
        self.calls.append((interval, int(asof_us)))
        steps = self._program.get(interval)
        if steps:
            step = steps.pop(0) if len(steps) > 1 else steps[0]
        else:
            step = self._default.get(interval)
        if step is None:
            raise AssertionError(f"no programmed payload for interval {interval!r}")
        if isinstance(step, BaseException):
            raise step
        return step


def make_classification_record(
    event: Any,
    *,
    eligible: bool = True,
    status: str = STATUS_CLASSIFIED,
    non_eligible_codes: Sequence[str] = (),
    regime_4h: str = "BULLISH",
    regime_1d: str = "NEUTRAL",
    **overrides: Any,
) -> ClassificationRecord:
    values: dict[str, Any] = dict(
        version=H1E_CLASSIFICATION_VERSION,
        source_event_key=event.source_event_key,
        source_identity=event.source_identity,
        lineage_hash=event.lineage_hash,
        signal_time_us=event.signal_time_us,
        asof_us=event.signal_time_us,
        status=status,
        eligible=eligible,
        eligibility_reason=("H1E_ELIGIBLE" if eligible else "H1E_NON_ELIGIBLE"),
        non_eligible_codes=tuple(non_eligible_codes),
        regime_4h=regime_4h,
        bos_4h="NO_BOS",
        regime_1d=regime_1d,
        bos_1d="NO_BOS",
        combined_regime=f"{regime_4h}/{regime_1d}",
        coverage_4h_status="COMPLETE_COVERAGE",
        coverage_1d_status="COMPLETE_COVERAGE",
        coverage_4h_present=60,
        coverage_1d_present=60,
        coverage_4h_missing_open_ms=(),
        coverage_1d_missing_open_ms=(),
        snapshot_sha256="a" * 64,
    )
    values.update(overrides)
    return ClassificationRecord(**values)
