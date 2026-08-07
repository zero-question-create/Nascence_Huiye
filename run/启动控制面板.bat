@echo off

setlocal enabledelayedexpansion

set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"

rem --- check system Python ---
set "PYTHON_OK="
python --version >nul 2>nul
if %errorlevel% equ 0 set "PYTHON_OK=1"
if not defined PYTHON_OK (
    py --version >nul 2>nul
    if %errorlevel% equ 0 set "PYTHON_OK=1"
)
if not defined PYTHON_OK (
    echo.
    echo [ERROR] Python was not found!
    echo.
    echo Please install Python from https://www.python.org/downloads/
    echo and make sure to check "Add Python to PATH" during installation.
    echo Then reopen the terminal and run this script again.
    echo.
    pause
    exit /b 1
)
echo [OK] Python found

rem --- Qt plugin path (fix "could not find platform plugin" error) ---
set "QT_PLUGIN_PATH=%PROJECT_DIR%\venv\Lib\site-packages\PyQt5\Qt5\plugins"

echo ==========================================
echo  Nascence Huiye Control Panel
echo ==========================================

rem --- ensure virtual env ---
if not exist "%PROJECT_DIR%\venv\Scripts\python.exe" (
    powershell -Command "Write-Host 'Virtual env not found, running setup.ps1...' -ForegroundColor Cyan"
    powershell -ExecutionPolicy Bypass -File "%PROJECT_DIR%\setup.ps1"
    if not exist "%PROJECT_DIR%\venv\Scripts\python.exe" (
        powershell -Command "Write-Host '[ERROR] Env setup failed, please run setup.ps1 manually' -ForegroundColor Red"
        pause
        exit /b 1
    )
)

rem --- ensure ollama ---
if not exist "%PROJECT_DIR%\ollama\bin\ollama.exe" (
    powershell -Command "Write-Host 'Ollama not found, downloading...' -ForegroundColor Yellow"
    powershell -ExecutionPolicy Bypass -File "%PROJECT_DIR%\run\install_ollama.ps1"
    if not exist "%PROJECT_DIR%\ollama\bin\ollama.exe" (
        powershell -Command "Write-Host '[WARN] Ollama install failed, place ollama.exe in ollama\bin\ manually' -ForegroundColor Yellow"
    )
)

"%PROJECT_DIR%\venv\Scripts\python.exe" "%PROJECT_DIR%\control_panel.py"
if %errorlevel% neq 0 (
    powershell -Command "Write-Host '[ERROR] Program exited abnormally, code: %errorlevel%' -ForegroundColor Red"
)

powershell -Command "Write-Host 'Program exited normally' -ForegroundColor Green"
pause
