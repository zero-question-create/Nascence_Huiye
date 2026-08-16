@echo off
REM Start the Nascence dialogue gateway (server + client page)
cd /d "%~dp0.."
set VENV=venv
if not exist "%VENV%\Scripts\python.exe" (
  echo [ERROR] Virtual env not found: %VENV%\
  pause
  exit /b 1
)
"%VENV%\Scripts\python.exe" gateway\server.py --port 8899
pause
