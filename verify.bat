@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
"%~dp0.venv\Scripts\python.exe" "%~dp0app.py" --verify %*
set "verify_exit=%errorlevel%"
echo.
echo See test_runs for the HTML and JSON verification reports.
pause
exit /b %verify_exit%
