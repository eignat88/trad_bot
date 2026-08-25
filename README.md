# Bybit Price + Open Interest research bot

Safe-by-default framework for collecting Bybit USDT perpetual (`linear`) OHLCV,
Open Interest and funding data; detecting three Price/OI setups; paper execution;
and replaying the same `StrategyEngine` in backtests.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py --check-config
python bot.py
```

The default is **paper**. Live mode requires both `TRADING_MODE=live` and
`LIVE_TRADING_ENABLED=true`; an API key alone never activates it. Backtests take
chronological `MarketSnapshot` records and support explicit train, validation,
and out-of-sample splits. No parameter optimization is performed by the project.

## Scanner universe

`scanner_runner.py` and `python -m app.scanners.cli` use the
`scanner_universe` section in `config.yaml`. In `dynamic` mode they query Bybit
for active linear USDT perpetuals, apply the configured 24-hour turnover and
volume thresholds, then run every enabled scanner over the top contracts ranked
by turnover. Start with 30–50 symbols to keep the candle, open-interest, and
funding requests comfortably bounded.

Set `scanner_universe.mode` to `static` to use the top-level `symbols` list
instead. An explicit CLI `--symbols` list always takes precedence over either
configured mode.
