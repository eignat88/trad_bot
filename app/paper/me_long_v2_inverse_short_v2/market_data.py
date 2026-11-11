"""Market data provider for the ME_LONG_V2_INVERSE_SHORT_V2 runtime (Issue #268).

One provider serves exactly what the frozen contour engine consumes:

* **prices**   — the last traded price per requested symbol (the documented
  execution policy of this contour: ``ENTRY_MODEL =
  NEXT_OBSERVED_MARKET_PRICE`` — the same ``lastPrice`` source the existing
  Paper runner and ``me_inverse_short_runner`` use);
* **bars**     — the LAST **CLOSED** OHLC bar of the configured interval,
  used only for the intrabar STOP/TP touch check of open positions;
* **funding**  — the last observed funding rate in PERCENT for symbols with
  open positions (the engine owns period confirmation on the position row;
  this provider never fabricates a rate or a period).

Safety contract (all enforced here, all unit-tested):

* **No lookahead** — a bar qualifies only when
  ``open_ms + interval_ms <= now_ms`` (closed at or before the observation
  time).  A still-forming candle is never returned, so no future OHLC value
  can ever reach the engine.
* **UTC everywhere** — one injected UTC clock timestamps observations,
  closes and ages; a negative age (clock moved backwards) is never treated
  as fresh.
* **Freshness per data type** — every stream has an explicit maximum age
  (``MarketDataConfig``).  A value older than its limit is EXCLUDED, never
  silently reused.
* **Explicit triage** — every (symbol, stream) gets an honest status:
  ``OK`` (fresh observation), ``DELAYED`` (usable cached observation, the
  fetch failed or returned nothing new this cycle), ``STALE`` (exists but
  past its max age — excluded), ``MISSING`` (never observed — excluded),
  ``API_ERROR`` (fetch failed and no usable cache — excluded) or
  ``INVALID`` (malformed / non-positive / non-finite / off-grid — excluded).
  Missing or stale data is NEVER substituted with a fabricated value.
* **Global limiter untouched** — every request goes through the project's
  ``BybitClient`` (retry budget + API telemetry + shared global rate
  limiter).  This module performs no HTTP of its own and adds no bypass.
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Exchange symbol shape accepted for API requests and responses
#: (Bybit linear perps: upper-case alphanumerics, e.g. ``BTCUSDT``).
SYMBOL_RE = re.compile(r"[A-Z0-9]{1,30}\Z")


def valid_symbol(symbol: Any) -> bool:
    """True only for a well-formed exchange symbol (never raises)."""
    return isinstance(symbol, str) and bool(SYMBOL_RE.match(symbol))

# Honest per-(symbol, stream) statuses.
STATUS_OK = "OK"
STATUS_DELAYED = "DELAYED"
STATUS_STALE = "STALE"
STATUS_MISSING = "MISSING"
STATUS_API_ERROR = "API_ERROR"
STATUS_INVALID = "INVALID"

# Statuses whose value is EXCLUDED from the snapshot handed to the engine.
EXCLUDED_STATUSES = frozenset({
    STATUS_STALE, STATUS_MISSING, STATUS_API_ERROR, STATUS_INVALID,
})

PRICE_STREAM = "price"
BAR_STREAM = "bar"
FUNDING_STREAM = "funding"

# -----------------------------------------------------------------------
# Defaults (documented execution policy — see the deployment runbook)
# -----------------------------------------------------------------------
#: Prices: a last-traded price older than this is stale and excluded.
#: The runtime cycles at ~10s, so 30s tolerates two failed refreshes.
DEFAULT_PRICE_MAX_AGE_S = 30.0
#: Bars: only the last CLOSED bar is used; it must have closed within this
#: many seconds (two 5m intervals + slack by default).
DEFAULT_BAR_MAX_AGE_S = 600.0
#: Funding: the observed rate must have been refreshed within this window;
#: beyond it the rate is UNKNOWN (the engine leaves the obligation open).
DEFAULT_FUNDING_MAX_AGE_S = 3600.0
#: OHLC interval used for the intrabar stop/target check (minutes).
DEFAULT_BAR_INTERVAL = "5"

ENV_PRICE_MAX_AGE = "ME_INVERSE_SHORT_MD_PRICE_MAX_AGE_S"
ENV_BAR_MAX_AGE = "ME_INVERSE_SHORT_MD_BAR_MAX_AGE_S"
ENV_FUNDING_MAX_AGE = "ME_INVERSE_SHORT_MD_FUNDING_MAX_AGE_S"
ENV_BAR_INTERVAL = "ME_INVERSE_SHORT_MD_BAR_INTERVAL"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _positive_float(raw: Any, name: str) -> float:
    try:
        number = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name}_INVALID:{raw!r}") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name}_INVALID:{raw!r}")
    return number


@dataclass(frozen=True)
class MarketDataConfig:
    """Per-stream freshness limits + bar interval (all values validated)."""

    price_max_age_s: float = DEFAULT_PRICE_MAX_AGE_S
    bar_max_age_s: float = DEFAULT_BAR_MAX_AGE_S
    funding_max_age_s: float = DEFAULT_FUNDING_MAX_AGE_S
    bar_interval: str = DEFAULT_BAR_INTERVAL

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "price_max_age_s",
            _positive_float(self.price_max_age_s, "PRICE_MAX_AGE"),
        )
        object.__setattr__(
            self, "bar_max_age_s",
            _positive_float(self.bar_max_age_s, "BAR_MAX_AGE"),
        )
        object.__setattr__(
            self, "funding_max_age_s",
            _positive_float(self.funding_max_age_s, "FUNDING_MAX_AGE"),
        )
        interval = str(self.bar_interval).strip()
        try:
            minutes = int(interval)
        except (TypeError, ValueError):
            raise ValueError(f"BAR_INTERVAL_INVALID:{self.bar_interval!r}") from None
        if minutes <= 0 or minutes > 1440:
            raise ValueError(f"BAR_INTERVAL_INVALID:{self.bar_interval!r}")
        object.__setattr__(self, "bar_interval", str(minutes))

    @property
    def interval_ms(self) -> int:
        return int(self.bar_interval) * 60_000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "MarketDataConfig":
        """Build the config from the documented environment overrides.

        Any malformed value raises ``ValueError`` — the runtime fails fast
        at startup instead of trading on an unexpected freshness window.
        """
        source = os.environ if env is None else env
        overrides: dict[str, Any] = {}
        for key, attr in (
            (ENV_PRICE_MAX_AGE, "price_max_age_s"),
            (ENV_BAR_MAX_AGE, "bar_max_age_s"),
            (ENV_FUNDING_MAX_AGE, "funding_max_age_s"),
        ):
            raw = source.get(key)
            if raw is not None and str(raw).strip():
                overrides[attr] = raw
        raw_interval = source.get(ENV_BAR_INTERVAL)
        if raw_interval is not None and str(raw_interval).strip():
            overrides["bar_interval"] = raw_interval
        return cls(**overrides)


@dataclass(frozen=True)
class MarketDataSnapshot:
    """One cycle's market data: usable values + honest per-symbol triage."""

    observed_at: datetime
    prices: dict[str, float] = field(default_factory=dict)
    bars: dict[str, dict[str, float]] = field(default_factory=dict)
    funding_rates_percent: dict[str, float] = field(default_factory=dict)
    price_age_ms: dict[str, int] = field(default_factory=dict)
    statuses: dict[str, dict[str, str]] = field(default_factory=dict)
    api_errors: int = 0

    @property
    def max_price_age_ms(self) -> int | None:
        """Age of the OLDEST price actually handed to the engine.

        ``None`` when no usable price exists (excluded/stale symbols do
        not inflate the age of the data the engine trades on).
        """
        ages = [
            self.price_age_ms[symbol] for symbol in self.prices
            if symbol in self.price_age_ms
        ]
        return max(ages) if ages else None

    def status(self, symbol: str, stream: str) -> str:
        return self.statuses.get(symbol, {}).get(stream, STATUS_MISSING)

    def price_status(self, symbol: str) -> str:
        return self.status(symbol, PRICE_STREAM)

    def entry_refusals(self, symbols: Any) -> list[str]:
        """Per-symbol price blockers for prospective entries (sorted)."""
        refusals: list[str] = []
        for symbol in sorted(set(symbols or ())):
            status = self.price_status(symbol)
            if status != STATUS_OK:
                refusals.append(f"PRICE_{status}:{symbol}")
        return refusals

    def summary(self) -> dict[str, int]:
        """Counts per price status (telemetry-friendly, no symbol spam)."""
        counts: dict[str, int] = {}
        for streams in self.statuses.values():
            status = streams.get(PRICE_STREAM, STATUS_MISSING)
            counts[status] = counts.get(status, 0) + 1
        return counts


class MarketDataProvider:
    """Freshness-checked, no-lookahead market data over the shared client.

    The provider keeps only observation caches (value + observed-at); every
    value handed out has passed its stream's freshness window and validity
    checks for THIS cycle.  All HTTP goes through the injected client
    (``BybitClient``), which owns retry policy, API telemetry and the
    global rate limiter.
    """

    def __init__(
        self,
        client: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        config: MarketDataConfig | None = None,
    ) -> None:
        self.client = client
        self._clock = clock or _utc_now
        self.config = config or MarketDataConfig()
        # value -> observation time (UTC), per stream, per symbol.
        self._price_cache: dict[str, tuple[float, datetime]] = {}
        self._funding_cache: dict[str, tuple[float, datetime]] = {}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _age_ms(observed_at: datetime, now: datetime) -> int | None:
        """Observation age in ms; ``None`` when the clock moved backwards
        (a negative age is NEVER treated as fresh)."""
        delta = (now - observed_at).total_seconds() * 1000.0
        if delta < 0:
            return None
        return int(round(delta))

    def _cache_status(
        self,
        cache: tuple[float, datetime] | None,
        now: datetime,
        max_age_s: float,
        *,
        fetch_failed: bool,
        refreshed: bool,
        invalid_response: bool = False,
    ) -> tuple[str, float | None, int | None]:
        """Triage one (symbol, stream) observation (price / funding).

        ``fetch_failed``    the whole request raised this cycle;
        ``refreshed``       THIS symbol got a valid new value this cycle;
        ``invalid_response`` the response carried a value for this symbol
                            but it was malformed / non-finite / non-positive.

        Returns ``(status, usable_value, age_ms)``.  A stale cached value
        is reported ``STALE`` even when the fetch also failed (the age is
        the reason it cannot be used; the failure itself is still counted
        in the snapshot's ``api_errors``).
        """
        if cache is None:
            if fetch_failed:
                return STATUS_API_ERROR, None, None
            if invalid_response:
                return STATUS_INVALID, None, None
            return STATUS_MISSING, None, None
        value, observed_at = cache
        age_ms = self._age_ms(observed_at, now)
        if age_ms is None:
            # Clock went backwards: never usable, honestly reported.
            return STATUS_INVALID, None, None
        if age_ms > max_age_s * 1000.0:
            return STATUS_STALE, None, age_ms
        if refreshed:
            return STATUS_OK, value, age_ms
        # Usable cached observation: fresh, but not refreshed this cycle.
        return STATUS_DELAYED, value, age_ms

    @staticmethod
    def _valid_price(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number <= 0:
            return None
        return number

    def _expire_on_return(
        self,
        final_now: datetime,
        statuses: dict[str, dict[str, str]],
        prices: dict[str, float],
        price_age_ms: dict[str, int],
        bars: dict[str, dict[str, float]],
        funding: dict[str, float],
    ) -> None:
        """Drop observations that aged out BEFORE the snapshot returned.

        The per-stream triage inside ``snapshot()`` runs against the time
        the cycle STARTED, but the bar and funding requests in between
        (retries included) can consume seconds — a quote that was fresh
        when fetched may be older than ``price_max_age_s`` by the time
        the snapshot reaches the engine (Codex R2 P1).  This gate runs
        immediately before the return: every USABLE observation is
        re-checked against the CURRENT UTC clock and anything no longer
        provably fresh is removed and honestly re-triaged ``STALE`` —
        never passed on as ``OK`` with an age that was true only at cycle
        start, and never substituted with a last-known value.
        """
        final_ms = int(final_now.timestamp() * 1000)
        for symbol in list(prices):
            cached = self._price_cache.get(symbol)
            age_ms = self._age_ms(cached[1], final_now) if cached else None
            if age_ms is None or age_ms > self.config.price_max_age_s * 1000.0:
                prices.pop(symbol)
                if age_ms is not None:
                    # Diagnostic age of the newest observation (updated
                    # to the RETURN time — that is what excluded it).
                    price_age_ms[symbol] = age_ms
                statuses[symbol][PRICE_STREAM] = STATUS_STALE
        for symbol, bar in list(bars.items()):
            age_ms = final_ms - int(bar["close_ms"])
            if age_ms < 0 or age_ms > self.config.bar_max_age_s * 1000.0:
                # ``age_ms < 0`` = the clock moved backwards mid-cycle:
                # never fresh, honestly reported.
                bars.pop(symbol)
                statuses[symbol][BAR_STREAM] = STATUS_STALE
        for symbol in list(funding):
            cached = self._funding_cache.get(symbol)
            age_ms = self._age_ms(cached[1], final_now) if cached else None
            if age_ms is None or age_ms > self.config.funding_max_age_s * 1000.0:
                funding.pop(symbol)
                statuses[symbol][FUNDING_STREAM] = STATUS_STALE

    # ------------------------------------------------------------------
    # snapshot
    # ------------------------------------------------------------------
    def snapshot(
        self,
        *,
        price_symbols: Any = (),
        bar_symbols: Any = (),
        funding_symbols: Any = (),
    ) -> MarketDataSnapshot:
        """Fetch one cycle's data.  Never raises on data problems.

        ``price_symbols``   — symbols that need a last price (open
                              positions + prospective entry events);
        ``bar_symbols``     — symbols that need the last CLOSED bar (open
                              positions only — entries use the price);
        ``funding_symbols`` — symbols that need a funding rate (open
                              positions only).
        """
        price_symbols = set(price_symbols or ())
        bar_symbols = set(bar_symbols or ())
        funding_symbols = set(funding_symbols or ())
        # Symbol integrity: a malformed symbol (corrupt row / bad caller)
        # is rejected BEFORE any request is made — never sent to the API.
        requested = price_symbols | bar_symbols | funding_symbols
        malformed = sorted(s for s in requested if not valid_symbol(s))
        if malformed:
            logger.warning(
                "me inverse short v2 market data: malformed symbol(s) refused "
                "(no request is made for them): %s", malformed[:5],
            )
            price_symbols = {s for s in price_symbols if valid_symbol(s)}
            bar_symbols = {s for s in bar_symbols if valid_symbol(s)}
            funding_symbols = {s for s in funding_symbols if valid_symbol(s)}
        now = _as_utc(self._clock())
        if now is None:
            raise ValueError("MUST_USE_UTC_CLOCK")

        statuses: dict[str, dict[str, str]] = {
            symbol: {
                PRICE_STREAM: STATUS_MISSING,
                BAR_STREAM: STATUS_MISSING,
                FUNDING_STREAM: STATUS_MISSING,
            }
            for symbol in (price_symbols | bar_symbols | funding_symbols)
        }
        prices: dict[str, float] = {}
        price_age_ms: dict[str, int] = {}
        bars: dict[str, dict[str, float]] = {}
        funding: dict[str, float] = {}
        api_errors = 0

        # ---- prices: ONE tickers request for every requested symbol ----
        if price_symbols:
            rows: list[dict[str, Any]] | None = None
            try:
                rows = list(self.client.get_tickers("linear") or [])
            except Exception:
                api_errors += 1
                logger.warning(
                    "me inverse short v2 market data: ticker fetch failed "
                    "(%d symbol(s) fall back to their cache; stale/absent "
                    "prices are excluded — entries fail closed, exits hold)",
                    len(price_symbols), exc_info=True,
                )
            fetched_ok = rows is not None
            fresh: dict[str, float] = {}
            invalid_seen: set[str] = set()
            if rows is not None:
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    symbol = row.get("symbol")
                    if symbol not in price_symbols:
                        continue
                    value = self._valid_price(row.get("lastPrice"))
                    if value is None:
                        # Malformed quote: never cached, never used.
                        invalid_seen.add(symbol)
                        continue
                    fresh[symbol] = value
                    self._price_cache[symbol] = (value, now)
            for symbol in sorted(price_symbols):
                status, value, age = self._cache_status(
                    self._price_cache.get(symbol), now,
                    self.config.price_max_age_s,
                    fetch_failed=not fetched_ok,
                    refreshed=symbol in fresh,
                    invalid_response=symbol in invalid_seen,
                )
                statuses[symbol][PRICE_STREAM] = status
                if value is not None:
                    prices[symbol] = value
                if age is not None:
                    # Diagnostic age of the newest OBSERVATION of this
                    # symbol (stale ones included); usable prices only
                    # feed ``max_price_age_ms`` below.
                    price_age_ms[symbol] = age

        # ---- bars: LAST CLOSED OHLC only (no lookahead) ---------------
        for symbol in sorted(bar_symbols):
            try:
                candles = self.client.get_klines(
                    symbol, self.config.bar_interval, 3,
                )
            except Exception:
                api_errors += 1
                logger.warning(
                    "me inverse short v2 market data: kline fetch failed for %s "
                    "(no bar handed to the engine this cycle; the intrabar "
                    "check falls back to the observed price)",
                    symbol, exc_info=True,
                )
                statuses[symbol][BAR_STREAM] = STATUS_API_ERROR
                continue
            now_ms = int(now.timestamp() * 1000)
            interval_ms = self.config.interval_ms
            closed = [
                candle for candle in (candles or [])
                if int(candle.timestamp) + interval_ms <= now_ms
            ]
            if not closed:
                # Only still-forming candles were returned (or none at all).
                statuses[symbol][BAR_STREAM] = STATUS_MISSING
                continue
            last = closed[-1]
            open_ms = int(last.timestamp)
            close_ms = open_ms + interval_ms
            if open_ms % interval_ms != 0:
                # Off-grid bar: reject rather than guess the range.
                logger.warning(
                    "me inverse short v2 market data: off-grid bar rejected: "
                    "symbol=%s open_ms=%s interval=%s",
                    symbol, open_ms, self.config.bar_interval,
                )
                statuses[symbol][BAR_STREAM] = STATUS_INVALID
                continue
            high = self._valid_price(last.high)
            low = self._valid_price(last.low)
            if high is None or low is None or high < low:
                statuses[symbol][BAR_STREAM] = STATUS_INVALID
                continue
            age_ms = now_ms - close_ms  # >= 0 by the closed filter above
            if age_ms > self.config.bar_max_age_s * 1000.0:
                statuses[symbol][BAR_STREAM] = STATUS_STALE
                continue
            statuses[symbol][BAR_STREAM] = STATUS_OK
            # ``close_ms`` is the bar's provenance: the runtime clips every
            # bar to the position entry time (review P1) and treats a bar
            # WITHOUT a known close time as inadmissible for exits.
            bars[symbol] = {
                "high": high, "low": low,
                "open_ms": open_ms, "close_ms": close_ms,
            }

        # ---- funding: observed rate in PERCENT (tri-state preserved) ---
        for symbol in sorted(funding_symbols):
            fetch_failed = False
            invalid_response = False
            refreshed = False
            fetched, fetched_value, fetch_exc = self._fetch_funding_rate(
                symbol,
            )
            if fetched == STATUS_API_ERROR:
                fetch_failed = True
                api_errors += 1
                logger.warning(
                    "me inverse short v2 market data: funding fetch failed for "
                    "%s (rate stays UNKNOWN — the obligation is preserved, "
                    "nothing is booked, no zero is fabricated)",
                    symbol, exc_info=fetch_exc,
                )
            elif fetched == STATUS_INVALID:
                invalid_response = True
            elif fetched == STATUS_MISSING:
                # Valid but EMPTY funding history (Codex R2 P2): no
                # observation was made — the rate stays UNKNOWN and an
                # empty list is NEVER a confirmed 0.0 rate.
                logger.debug(
                    "me inverse short v2 market data: empty funding history "
                    "for %s — rate remains UNKNOWN (no zero fabricated)",
                    symbol,
                )
            else:  # STATUS_OK — a real row, 0.0 included
                refreshed = True
                self._funding_cache[symbol] = (fetched_value, now)
            status, value, _age = self._cache_status(
                self._funding_cache.get(symbol), now,
                self.config.funding_max_age_s,
                fetch_failed=fetch_failed,
                refreshed=refreshed,
                invalid_response=invalid_response,
            )
            statuses[symbol][FUNDING_STREAM] = status
            if value is not None:
                funding[symbol] = value

        # ---- RETURN-TIME freshness gate (Codex R2 P1) ------------------
        # The triage above ran at cycle START; the bar/funding requests
        # may have consumed seconds since.  Re-check every usable
        # observation against the CURRENT clock before returning.
        final_now = _as_utc(self._clock())
        self._expire_on_return(
            final_now if final_now is not None else now,
            statuses, prices, price_age_ms, bars, funding,
        )

        return MarketDataSnapshot(
            observed_at=now,
            prices=prices,
            bars=bars,
            funding_rates_percent=funding,
            price_age_ms=price_age_ms,
            statuses=statuses,
            api_errors=api_errors,
        )

    @staticmethod
    def _valid_price_or_zero(value: Any) -> float | None:
        """Funding may legitimately be exactly 0.0 — but never NaN/inf."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        return number

    def _fetch_funding_rate(
        self, symbol: str,
    ) -> tuple[str, float | None, BaseException | None]:
        """Fetch one funding rate WITHOUT collapsing "no history" into 0.0.

        ``BybitClient.get_funding_rate`` returns a literal ``0.0`` when
        the funding-history response is an EMPTY list — a value
        indistinguishable from a CONFIRMED zero rate.  Adopting it would
        permanently advance ``funding_periods_charged`` with a fabricated
        zero (Codex R2 P2).  This adapter goes through the SAME Bybit API
        layer that helper uses (``_public_get`` — budgeted, retried,
        limiter- and telemetry-aware) but keeps the tri-state:

          * ``("OK", rate_percent, None)``   — a history row was present
            (a genuine 0.0 included);
          * ``("MISSING", None, None)``      — the history list is empty
            (not yet published): UNKNOWN, never a zero;
          * ``("INVALID", None, None)``      — the row exists but is
            malformed / non-finite;
          * ``("API_ERROR", None, exc)``     — the request failed.

        Scripted doubles without the raw layer keep their existing
        behaviour through the public helper.  ``BybitClient`` itself is
        NOT modified — its historical contract serves other callers
        unchanged (regression: tests/test_bybit_*).
        """
        raw_get = getattr(self.client, "_public_get", None)
        if not callable(raw_get):
            # Scripted test double: only the public helper exists.
            try:
                raw = self.client.get_funding_rate(symbol)
            except Exception as exc:
                return STATUS_API_ERROR, None, exc
            value = self._valid_price_or_zero(raw)
            if value is None:
                return STATUS_INVALID, None, None
            return STATUS_OK, value, None
        try:
            payload = raw_get(
                "/v5/market/funding/history",
                category="linear", symbol=symbol, limit=1,
            )
            rows = payload["result"]["list"]
        except Exception as exc:
            return STATUS_API_ERROR, None, exc
        if not rows:
            # Empty history: no observation was made.  NEVER 0.0.
            return STATUS_MISSING, None, None
        row = rows[0] if isinstance(rows[0], dict) else None
        value = self._valid_price_or_zero(
            row.get("fundingRate") if row is not None else None,
        )
        if value is None:
            return STATUS_INVALID, None, None
        return STATUS_OK, value * 100.0, None   # fraction -> percent
