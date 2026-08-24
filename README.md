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
