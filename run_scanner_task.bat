@echo off
REM Foreground launcher used by Windows Task Scheduler.
REM Keep python attached to the scheduled task so schtasks /end can stop it.
cd /d %~dp0
set PYTHON=%~dp0.venv\Scripts\python.exe
if not exist "%PYTHON%" set PYTHON=python
"%PYTHON%" scanner_runner.py
