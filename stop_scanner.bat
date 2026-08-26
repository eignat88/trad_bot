@echo off
REM Gracefully stop only the scheduled scanner task tracked by Windows Task Scheduler.
set TASK_NAME=BybitScanner

echo Stopping scheduled task: %TASK_NAME%
schtasks /end /tn "%TASK_NAME%" 2>nul
if %errorlevel% equ 0 (
    echo Scanner task stop requested.
) else (
    echo Scanner task was not running or could not be stopped.
)
