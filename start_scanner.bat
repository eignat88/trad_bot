@echo off
REM Start the scanner in background (for manual testing)
cd /d %~dp0
start /min python scanner_runner.py
echo Scanner started in background. Check logs/scanner.log
