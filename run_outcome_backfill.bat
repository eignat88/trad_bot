@echo off
REM Launcher used by Windows Task Scheduler to keep signal outcomes updated.
REM It self-skips weekends and hours after the scanner stop time.
cd /d %~dp0

powershell -NoProfile -Command "$now=Get-Date; if ($now.DayOfWeek -in @('Saturday','Sunday') -or $now.Hour -ge 18) { exit 1 }"
if not %errorlevel% equ 0 (
    echo Outcome backfill skipped outside trading window.
    exit /b 0
)

set PYTHON=%~dp0.venv\Scripts\python.exe
if not exist "%PYTHON%" set PYTHON=python
"%PYTHON%" -m app.scanners.outcome_cli --limit 100 --min-age-minutes 240 --max-bars 48 --fee-slippage-r 0.05
