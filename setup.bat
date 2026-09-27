@echo off
rem ai-context-kit: run tools\setup.py with the Python found on this PC.
rem Double-click, or run "setup.bat --dry-run" to only see what would change.
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3 was not found. Install it from https://www.python.org/ and run this again.
  pause
  exit /b 1
)
%PY% "%~dp0tools\setup.py" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
