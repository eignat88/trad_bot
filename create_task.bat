@echo off
schtasks /create /tn "BybitScanner" /tr "D:\Python\python.exe D:\py_pro\trad_bot\scanner_runner.py" /sc onstart /ru SYSTEM /rl HIGHEST /f
echo Result: %errorlevel%
pause
