@echo off
setlocal EnableDelayedExpansion
REM Gracefully stop the scanner and its paper-trading gate.
for %%T in (BybitScanner BybitPaperRunner) do (
    echo Stopping scheduled task: %%T
    schtasks /end /tn "%%T" 2>nul
    if !errorlevel! equ 0 (
        echo Stop requested: %%T
    ) else (
        echo Task was not running or could not be stopped: %%T
    )
)
