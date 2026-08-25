@echo off
REM Stop the scanner task
schtasks /end /tn "BybitScanner" 2>nul
taskkill /f /im python.exe /fi "WINDOWTITLE eq scanner_runner*" 2>nul
echo Scanner stopped.
