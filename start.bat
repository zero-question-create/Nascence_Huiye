@echo off
chcp 65001 >nul
title NASCENCE Huiye - Launcher
setlocal

pushd "%~dp0"
set "PROJECT_DIR=%CD%"

echo ==========================================
echo  NASCENCE Huiye - Web Server Launcher
echo  Client : http://127.0.0.1:8787/
echo  Admin  : http://127.0.0.1:8787/admin
echo ==========================================

rem ==================== 1. Check system Python ====================
set "PYTHON_OK="
python --version >nul 2>nul
if not errorlevel 1 set "PYTHON_OK=1"
if not defined PYTHON_OK (
    py --version >nul 2>nul
    if not errorlevel 1 set "PYTHON_OK=1"
)
if not defined PYTHON_OK (
    echo.
    echo [ERROR] Python was not found!
    echo Please install Python from https://www.python.org/downloads/
    echo and make sure to check "Add Python to PATH" during installation.
    echo Then reopen the terminal and run this script again.
    echo.
    pause
    popd
    exit /b 1
)
echo [OK] Python found

rem ==================== 2. Check virtual env ====================
set "VENV_PY=%PROJECT_DIR%\venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [*] Virtual env not found, running setup.ps1...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\setup.ps1"
    if not exist "%VENV_PY%" (
        echo [ERROR] Virtual env creation failed. Please run setup.ps1 manually.
        pause
        popd
        exit /b 1
    )
)
echo [OK] Virtual env found

rem ==================== 3. Ensure data dir ====================
if not exist "%PROJECT_DIR%\data\test" mkdir "%PROJECT_DIR%\data\test"

rem ==================== 4. Ensure llama.cpp ====================
if not exist "%PROJECT_DIR%\llama\bin\llama-server.exe" (
    echo [*] llama.cpp not found, downloading...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\run\install_llama.ps1"
    if not exist "%PROJECT_DIR%\llama\bin\llama-server.exe" (
        echo [ERROR] llama.cpp install failed. Please check network or install manually.
        pause
        popd
        exit /b 1
    )
)
echo [OK] llama.cpp ready

rem ==================== 5. Port 8787 pre-check ====================
set "PORT_BUSY="
netstat -ano | findstr ":8787" | findstr "LISTENING" >nul
if not errorlevel 1 set "PORT_BUSY=1"

if defined PORT_BUSY (
    echo.
    echo [*] Port 8787 is already in use. Server may already be running.
    echo [*] Opening browser to client and admin pages.
    start "" "http://127.0.0.1:8787/"
    start "" "http://127.0.0.1:8787/admin"
    echo.
    pause
    popd
    exit /b 0
)

rem ==================== 6. Start WebUI ====================
echo.
echo [*] Starting Web server, please wait...
echo [*] Browser will open client and admin pages after startup.
echo.

rem Open both pages 2s later (after port binding)
start "" powershell -NoProfile -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8787/'; Start-Process 'http://127.0.0.1:8787/admin'"
"%VENV_PY%" webui.py
set "CODE=%ERRORLEVEL%"

if "%CODE%" neq "0" (
    powershell -Command "Write-Host '[ERROR] Server exited abnormally, code: %CODE%' -ForegroundColor Red"
) else (
    powershell -Command "Write-Host 'Server exited normally' -ForegroundColor Green"
)

popd
pause
