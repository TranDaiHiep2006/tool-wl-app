@echo off
REM ---------------------------------------------------------------------------
REM  tool-wl-app console launcher.
REM
REM  Double-click this file to open the console window. It runs the repo's
REM  virtualenv Python (falling back to whatever python is on PATH) and keeps
REM  the window open even if the console exits with an error, so you can see
REM  what went wrong.
REM
REM  Pass --selftest as an argument to run the headless self-test instead:
REM      scripts\console.bat --selftest
REM ---------------------------------------------------------------------------
setlocal

REM Where this .bat lives (...tool-wl-app\scripts).
set "SCRIPT_DIR=%~dp0"
REM Repo root is one level up.
set "REPO_ROOT=%SCRIPT_DIR%.."

REM Prefer the repo virtualenv, exactly like the Cursor MCP registration.
set "PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

cd /d "%REPO_ROOT%"

echo Starting tool-wl-app console...
echo (close this window to stop the server)
echo.

"%PYTHON%" "%SCRIPT_DIR%console.py" %*

REM If it exits (error or quit), keep the window readable.
echo.
echo Console exited. Press any key to close...
pause >nul

endlocal