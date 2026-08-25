@echo off
REM Install scanner as Windows scheduled task (runs on startup)
REM Run this script as Administrator

set TASK_NAME=BybitScanner
set SCRIPT_DIR=%~dp0
set PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe
set RUNNER=%SCRIPT_DIR%scanner_runner.py

if not exist "%PYTHON%" (
    set PYTHON=python
)

echo Creating scheduled task: %TASK_NAME%
echo Script: %RUNNER%
echo Python: %PYTHON%

schtasks /create /tn "%TASK_NAME%" /tr "\"%PYTHON%\" \"%RUNNER%\"" /sc onstart /ru SYSTEM /rl HIGHEST /f
if %errorlevel% equ 0 (
    echo Task created successfully!
    echo.
    echo To start now: schtasks /run /tn "%TASK_NAME%"
    echo To stop:      schtasks /end /tn "%TASK_NAME%"
    echo To delete:    schtasks /delete /tn "%TASK_NAME%" /f
) else (
    echo Failed to create task. Run this script as Administrator.
)

pause
