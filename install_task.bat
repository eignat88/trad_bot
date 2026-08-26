@echo off
REM Install scanner Windows Scheduled Tasks.
REM Creates:
REM   BybitScanner     - starts on computer startup
REM   BybitScannerStop - stops BybitScanner at 18:00 Mon-Fri
REM Run this script as the Windows user that should own the tasks.

cd /d %~dp0
set PYTHON=%~dp0.venv\Scripts\python.exe
if not exist "%PYTHON%" set PYTHON=python

echo Installing scheduled tasks from %CD%
"%PYTHON%" create_scheduled_task.py install
if %errorlevel% equ 0 (
    echo.
    echo Tasks installed successfully.
    echo Start now:   schtasks /run /tn "BybitScanner"
    echo Stop now:    schtasks /end /tn "BybitScanner"
    echo Remove:      "%PYTHON%" create_scheduled_task.py uninstall
) else (
    echo.
    echo Failed to install tasks. Try running this script as Administrator.
)
pause
