"""Market data provider contract for the ME_LONG_V2_INVERSE_SHORT_V1 runtime
(Issue #268 §3.3) — no PostgreSQL, no network.

Required behaviours under test:

* freshness per stream (price / closed bar / funding) with explicit
  ``OK / DELAYED / STALE / MISSING / API_ERROR / INVALID`` triage;
* NO lookahead: only bars closed at/before the observation time are ever
  returned (forming candles, off-grid candles and future OHLC excluded);
* no fabricated values: stale/missing/invalid data is EXCLUDED, never
  substituted;
* one UTC clock (a naive clock is rejected; a backwards clock is never
  "fresh");
* API failures (rate limit / timeout / outage) are counted and degrade to
  cache-or-excluded — the provider never raises on data problems;
* every request goes through the injected Bybit client (the project's
  retry/telemetry/global-limiter owner) — the provider itself performs no
  HTTP.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Candle
from app.paper.me_long_v2_inverse_short_v1.market_data import (
    DEFAULT_BAR_INTERVAL,
    EXCLUDED_STATUSES,
    STATUS_API_ERROR,
    STATUS_DELAYED,
    STATUS_INVALID,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_STALE,
    MarketDataConfig,
    MarketDataProvider,
)

NOW = datetime(2026, 6, 1, 12, 7, 0, tzinfo=timezone.utc)
INTERVAL_MS = 5 * 60_000


class _Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class FakeBybitClient:
    """Scripted stand-in for BybitClient's PUBLIC read methods only.

    The runtime contract is that MarketDataProvider calls exactly these
    methods (which own retry policy, API telemetry and the global rate
    limiter) and performs no HTTP of its own.
    """

    def __init__(self, *, tickers=None, klines=None, funding=None) -> None:
        self.tickers = tickers if tickers is not None else []
        self.klines = klines or {}
        self.funding = funding or {}
        self.calls: list[tuple] = []

    def get_tickers(self, category: str = "linear"):
        self.calls.append(("tickers", category))
        if isinstance(self.tickers, BaseException):
            raise self.tickers
        return self.tickers

    def get_klines(self, symbol: str, interval: str = "5", limit: int = 200):
        self.calls.append(("klines", symbol, interval, limit))
        value = self.klines.get(symbol, [])
        if isinstance(value, BaseException):
            raise value
        return value

    def get_funding_rate(self, symbol: str) -> float:
        self.calls.append(("funding", symbol))
        value = self.funding.get(symbol)
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise RuntimeError("no funding row")
        return value


def _config(**overrides) -> MarketDataConfig:
    defaults = dict(
        price_max_age_s=30.0, bar_max_age_s=600.0,
        funding_max_age_s=3600.0, bar_interval="5",
    )
    defaults.update(overrides)
    return MarketDataConfig(**defaults)


def _provider(client, clock=None, config=None) -> MarketDataProvider:
    return MarketDataProvider(
        client, clock=clock or _Clock(), config=config or _config(),
    )


def _candle(open_dt: datetime, *, high=100.0, low=90.0,
            open_=95.0, close=95.0) -> Candle:
    return Candle(int(open_dt.timestamp() * 1000), open_, high, low, close, 1.0)


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
class TestPrices:
    def test_fresh_price_is_ok_and_carries_its_age(self):
        client = FakeBybitClient(
            tickers=[{"symbol": "BTCUSDT", "lastPrice": "50000.5"}],
        )
        snapshot = _provider(client).snapshot(price_symbols={"BTCUSDT"})
        assert snapshot.prices == {"BTCUSDT": 50000.5}
        assert snapshot.price_status("BTCUSDT") == STATUS_OK
        assert snapshot.max_price_age_ms == 0
        assert snapshot.api_errors == 0
        assert client.calls == [("tickers", "linear")]

    def test_absent_symbol_is_missing_and_excluded(self):
        client = FakeBybitClient(tickers=[{"symbol": "ETHUSDT", "lastPrice": "1"}])
        snapshot = _provider(client).snapshot(price_symbols={"BTCUSDT"})
        assert "BTCUSDT" not in snapshot.prices
        assert snapshot.price_status("BTCUSDT") == STATUS_MISSING
        assert snapshot.max_price_age_ms is None

    @pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "abc", None])
    def test_invalid_quote_is_excluded_never_guessed(self, raw):
        client = FakeBybitClient(tickers=[{"symbol": "BTCUSDT", "lastPrice": raw}])
        snapshot = _provider(client).snapshot(price_symbols={"BTCUSDT"})
        assert "BTCUSDT" not in snapshot.prices
        assert snapshot.price_status("BTCUSDT") == STATUS_INVALID

    def test_stale_cached_price_is_excluded_after_max_age(self):
        clock = _Clock()
        client = FakeBybitClient(
            tickers=[{"symbol": "BTCUSDT", "lastPrice": "50000"}],
        )
        provider = _provider(client, clock=clock)
        first = provider.snapshot(price_symbols={"BTCUSDT"})
        assert first.prices["BTCUSDT"] == 50000.0

        clock.advance(seconds=31)          # > price_max_age_s (30)
        client.tickers = []                # symbol vanishes from the feed
        second = provider.snapshot(price_symbols={"BTCUSDT"})
        assert "BTCUSDT" not in second.prices
        assert second.price_status("BTCUSDT") == STATUS_STALE
        assert second.price_age_ms["BTCUSDT"] == 31_000
        assert second.price_status("BTCUSDT") in EXCLUDED_STATUSES

    def test_api_failure_falls_back_to_fresh_cache_as_delayed(self):
        clock = _Clock()
        client = FakeBybitClient(
            tickers=[{"symbol": "BTCUSDT", "lastPrice": "50000"}],
        )
        provider = _provider(client, clock=clock)
        provider.snapshot(price_symbols={"BTCUSDT"})

        clock.advance(seconds=5)
        client.tickers = TimeoutError("read timeout")   # outage / 429 budget
        snapshot = provider.snapshot(price_symbols={"BTCUSDT"})
        assert snapshot.prices == {"BTCUSDT": 50000.0}
        assert snapshot.price_status("BTCUSDT") == STATUS_DELAYED
        assert snapshot.api_errors == 1

    def test_api_failure_without_cache_is_api_error(self):
        client = FakeBybitClient(tickers=ConnectionError("rate limited"))
        snapshot = _provider(client).snapshot(price_symbols={"BTCUSDT"})
        assert "BTCUSDT" not in snapshot.prices
        assert snapshot.price_status("BTCUSDT") == STATUS_API_ERROR
        assert snapshot.api_errors == 1

    def test_backwards_clock_is_never_fresh(self):
        clock = _Clock(NOW)
        client = FakeBybitClient(
            tickers=[{"symbol": "BTCUSDT", "lastPrice": "50000"}],
        )
        provider = _provider(client, clock=clock)
        provider.snapshot(price_symbols={"BTCUSDT"})
        # The clock steps BACKWARDS before the next (failed) refresh: the
        # cached observation is now "in the future" — never fresh, never
        # usable, honestly reported.
        clock.now = NOW - timedelta(seconds=5)
        client.tickers = ConnectionError("outage")
        snapshot = provider.snapshot(price_symbols={"BTCUSDT"})
        assert "BTCUSDT" not in snapshot.prices
        assert snapshot.price_status("BTCUSDT") == STATUS_INVALID

    def test_empty_request_makes_no_client_calls(self):
        client = FakeBybitClient()
        snapshot = _provider(client).snapshot()
        assert snapshot.prices == {} and snapshot.bars == {}
        assert snapshot.funding_rates_percent == {} and snapshot.api_errors == 0
        assert client.calls == []

    def test_naive_clock_is_rejected(self):
        provider = MarketDataProvider(
            FakeBybitClient(), clock=lambda: datetime(2026, 6, 1, 12, 0),
            config=_config(),
        )
        with pytest.raises(ValueError, match="UTC"):
            provider.snapshot(price_symbols={"BTCUSDT"})

    @pytest.mark.parametrize("bad", ["btcusdt", "BTC/USDT", "BTC USDT", "", 7])
    def test_malformed_symbol_is_refused_before_any_request(self, bad):
        client = FakeBybitClient(
            tickers=[{"symbol": "BTCUSDT", "lastPrice": "1"}],
        )
        snapshot = _provider(client).snapshot(price_symbols={bad})
        # No request may be made for a symbol that is not well-formed.
        assert client.calls == []
        assert snapshot.prices == {}
        assert snapshot.price_status(bad) == STATUS_MISSING
        assert snapshot.entry_refusals({bad}) == [f"PRICE_MISSING:{bad}"]


# ---------------------------------------------------------------------------
# Bars — closed OHLC only, no lookahead
# ---------------------------------------------------------------------------
class TestBars:
    def test_only_closed_bar_is_returned_forming_candle_ignored(self):
        # now = 12:07 -> the 12:05 candle is still forming, 12:00 is closed.
        forming = _candle(
            datetime(2026, 6, 1, 12, 5, tzinfo=timezone.utc),
            high=999.0, low=1.0,   # a "touch" only the future can see
        )
        closed = _candle(
            datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            high=101.0, low=99.0,
        )
        client = FakeBybitClient(klines={"BTCUSDT": [closed, forming]})
        snapshot = _provider(client).snapshot(bar_symbols={"BTCUSDT"})
        assert snapshot.bars == {"BTCUSDT": {
            "high": 101.0, "low": 99.0,
            "open_ms": int(datetime(2026, 6, 1, 12, 0,
                                    tzinfo=timezone.utc).timestamp() * 1000),
            "close_ms": int(datetime(2026, 6, 1, 12, 5,
                                     tzinfo=timezone.utc).timestamp() * 1000),
        }}
        assert snapshot.status("BTCUSDT", "bar") == STATUS_OK

    def test_forming_only_feed_yields_no_bar(self):
        forming = _candle(
            datetime(2026, 6, 1, 12, 5, tzinfo=timezone.utc), high=101.0,
        )
        client = FakeBybitClient(klines={"BTCUSDT": [forming]})
        snapshot = _provider(client).snapshot(bar_symbols={"BTCUSDT"})
        assert snapshot.bars == {}
        assert snapshot.status("BTCUSDT", "bar") == STATUS_MISSING

    def test_bar_stale_after_max_age_is_excluded(self):
        # Last closed bar is the 11:00 candle (closes 11:05); now 12:07 ->
        # age 62 min > bar_max_age_s (600 s).
        old = _candle(
            datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
            high=101.0, low=99.0,
        )
        client = FakeBybitClient(klines={"BTCUSDT": [old]})
        snapshot = _provider(client).snapshot(bar_symbols={"BTCUSDT"})
        assert snapshot.bars == {}
        assert snapshot.status("BTCUSDT", "bar") == STATUS_STALE

    def test_off_grid_bar_is_rejected(self):
        # Closed (11:57:30 + 5m = 12:02:30 <= now) but not on the 5m grid.
        odd_open = datetime(2026, 6, 1, 11, 57, 30, tzinfo=timezone.utc)
        client = FakeBybitClient(klines={"BTCUSDT": [_candle(odd_open)]})
        snapshot = _provider(client).snapshot(bar_symbols={"BTCUSDT"})
        assert snapshot.bars == {}
        assert snapshot.status("BTCUSDT", "bar") == STATUS_INVALID

    def test_inverted_ohlc_is_rejected(self):
        bad = _candle(
            datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            high=90.0, low=100.0,   # high < low
        )
        client = FakeBybitClient(klines={"BTCUSDT": [bad]})
        snapshot = _provider(client).snapshot(bar_symbols={"BTCUSDT"})
        assert snapshot.bars == {}
        assert snapshot.status("BTCUSDT", "bar") == STATUS_INVALID

    def test_kline_api_failure_degrades_to_no_bar_without_raising(self):
        client = FakeBybitClient(klines={"BTCUSDT": TimeoutError("429")})
        snapshot = _provider(client).snapshot(bar_symbols={"BTCUSDT"})
        assert snapshot.bars == {}
        assert snapshot.status("BTCUSDT", "bar") == STATUS_API_ERROR
        assert snapshot.api_errors == 1

    def test_bar_interval_is_the_configured_one(self):
        client = FakeBybitClient(
            klines={"BTCUSDT": [
                _candle(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)),
            ]},
        )
        _provider(client, config=_config(bar_interval="15")).snapshot(
            bar_symbols={"BTCUSDT"},
        )
        assert ("klines", "BTCUSDT", "15", 3) in client.calls


# ---------------------------------------------------------------------------
# Funding — observed rate in percent, tri-state preserved
# ---------------------------------------------------------------------------
class TestFunding:
    def test_observed_rate_is_passed_through_as_percent(self):
        client = FakeBybitClient(funding={"BTCUSDT": 0.0125})
        snapshot = _provider(client).snapshot(funding_symbols={"BTCUSDT"})
        assert snapshot.funding_rates_percent == {"BTCUSDT": 0.0125}
        assert snapshot.status("BTCUSDT", "funding") == STATUS_OK

    def test_confirmed_zero_rate_is_preserved(self):
        client = FakeBybitClient(funding={"BTCUSDT": 0.0})
        snapshot = _provider(client).snapshot(funding_symbols={"BTCUSDT"})
        assert snapshot.funding_rates_percent == {"BTCUSDT": 0.0}
        assert snapshot.status("BTCUSDT", "funding") == STATUS_OK

    def test_funding_failure_without_cache_is_unknown_not_zero(self):
        client = FakeBybitClient(funding={"BTCUSDT": ConnectionError("down")})
        snapshot = _provider(client).snapshot(funding_symbols={"BTCUSDT"})
        assert "BTCUSDT" not in snapshot.funding_rates_percent
        assert snapshot.status("BTCUSDT", "funding") == STATUS_API_ERROR
        assert snapshot.api_errors == 1

    def test_funding_failure_uses_fresh_cache_as_delayed(self):
        clock = _Clock()
        client = FakeBybitClient(funding={"BTCUSDT": 0.01})
        provider = _provider(client, clock=clock)
        provider.snapshot(funding_symbols={"BTCUSDT"})

        clock.advance(seconds=60)
        client.funding = {"BTCUSDT": TimeoutError("timeout")}
        snapshot = provider.snapshot(funding_symbols={"BTCUSDT"})
        assert snapshot.funding_rates_percent == {"BTCUSDT": 0.01}
        assert snapshot.status("BTCUSDT", "funding") == STATUS_DELAYED
        assert snapshot.api_errors == 1

    def test_stale_funding_cache_is_excluded(self):
        clock = _Clock()
        client = FakeBybitClient(funding={"BTCUSDT": 0.01})
        provider = _provider(client, clock=clock)
        provider.snapshot(funding_symbols={"BTCUSDT"})

        clock.advance(seconds=3601)          # > funding_max_age_s
        client.funding = TimeoutError("down")
        snapshot = provider.snapshot(funding_symbols={"BTCUSDT"})
        assert "BTCUSDT" not in snapshot.funding_rates_percent
        assert snapshot.status("BTCUSDT", "funding") == STATUS_STALE

    @pytest.mark.parametrize("raw", [float("nan"), float("inf"), "x"])
    def test_non_finite_funding_is_never_booked(self, raw):
        client = FakeBybitClient(funding={"BTCUSDT": raw})
        snapshot = _provider(client).snapshot(funding_symbols={"BTCUSDT"})
        assert "BTCUSDT" not in snapshot.funding_rates_percent
        assert snapshot.status("BTCUSDT", "funding") == STATUS_INVALID
        assert not any(
            not math.isfinite(v)
            for v in snapshot.funding_rates_percent.values()
        )


# ---------------------------------------------------------------------------
# Config + telemetry helpers
# ---------------------------------------------------------------------------
class TestConfigAndHelpers:
    def test_defaults_match_documented_execution_policy(self):
        config = MarketDataConfig()
        assert config.price_max_age_s == 30.0
        assert config.bar_max_age_s == 600.0
        assert config.funding_max_age_s == 3600.0
        assert config.bar_interval == DEFAULT_BAR_INTERVAL == "5"
        assert config.interval_ms == INTERVAL_MS

    @pytest.mark.parametrize("env", [
        {"ME_INVERSE_SHORT_MD_PRICE_MAX_AGE_S": "abc"},
        {"ME_INVERSE_SHORT_MD_PRICE_MAX_AGE_S": "-5"},
        {"ME_INVERSE_SHORT_MD_BAR_INTERVAL": "0"},
        {"ME_INVERSE_SHORT_MD_BAR_INTERVAL": "five"},
        {"ME_INVERSE_SHORT_MD_FUNDING_MAX_AGE_S": "nan"},
    ])
    def test_invalid_env_config_fails_fast(self, env):
        with pytest.raises(ValueError):
            MarketDataConfig.from_env(env)

    def test_env_overrides_are_honoured(self):
        config = MarketDataConfig.from_env({
            "ME_INVERSE_SHORT_MD_PRICE_MAX_AGE_S": "45",
            "ME_INVERSE_SHORT_MD_BAR_INTERVAL": "15",
        })
        assert config.price_max_age_s == 45.0
        assert config.bar_interval == "15"
        assert config.interval_ms == 15 * 60_000

    def test_entry_refusals_and_summary(self):
        client = FakeBybitClient(tickers=[{"symbol": "OKUSDT", "lastPrice": "1"}])
        snapshot = _provider(client).snapshot(
            price_symbols={"OKUSDT", "GONEUSDT"},
        )
        assert snapshot.entry_refusals({"OKUSDT"}) == []
        assert snapshot.entry_refusals({"OKUSDT", "GONEUSDT"}) == [
            "PRICE_MISSING:GONEUSDT",
        ]
        summary = snapshot.summary()
        assert summary[STATUS_OK] == 1 and summary[STATUS_MISSING] == 1
