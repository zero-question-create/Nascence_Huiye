@echo off
REM Open the Nascence dialogue client (standalone, no server required)
REM The client remembers the last-used server port in localStorage
echo ==========================================
echo  Nascence Huiye - Client Launcher
echo ==========================================
set "CLIENT_FILE=%~dp0..\client\index.html"
echo Opening standalone dialogue client: %CLIENT_FILE%
echo [Tip] The client auto-connects to the last-used server port.
echo       Make sure the server backend (start.bat) is running.
echo.
start "" "%CLIENT_FILE%"
timeout /t 3 >nul
exit /b 0
