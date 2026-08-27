@echo off
REM Foreground launcher used by Windows Task Scheduler for the paper gate.
REM Keep python attached to the scheduled task so schtasks /end can stop it.
cd /d %~dp0
set PYTHON=%~dp0.venvScriptspython.exe
if not exist "%PYTHON%" set PYTHON=python
"%PYTHON%" paper_runner.py
