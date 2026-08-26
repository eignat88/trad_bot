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
configured mode. The continuous runner refreshes the dynamic universe before
every cycle. If Bybit is temporarily unavailable during a refresh, it keeps the
last successfully loaded universe and retries on the next cycle.

## Running the project

Create the environment and install dependencies on Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Start the continuous scanner (TOP-N universe, all seven scanners, PostgreSQL):

```powershell
python .\scanner_runner.py
```

Alternatively, use the bundled background-process scripts:

```powershell
.\start_scanner.bat
.\stop_scanner.bat
```

Install Windows Scheduled Tasks for unattended operation:

```powershell
.\install_task.bat
```

This creates three tasks:

- `BybitScanner` starts `scanner_runner.py` automatically on computer startup with the project directory as the working directory.
- `BybitScannerStop` stops `BybitScanner` at 18:00, Monday through Friday.
- `BybitScannerOutcomeBackfill` runs `run_outcome_backfill.bat` hourly to keep `dds.signal_outcome` and expectancy reports current. The launcher self-skips weekends and hours after 18:00.

Manual task commands:

```powershell
schtasks /run /tn "BybitScanner"
schtasks /end /tn "BybitScanner"
python .\create_scheduled_task.py uninstall
```

Run a one-off scanner check, fill measured signal outcomes, or start the paper-trading application:

```powershell
python -m app.scanners.cli --once
python -m app.scanners.outcome_cli --limit 100 --min-age-minutes 240
python -m app.scanners.expectancy_report
python .\bot.py --check-config
python .\bot.py
```

On Linux or macOS, activate the environment with
`source .venv/bin/activate`; the Python commands are otherwise the same.
