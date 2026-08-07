@echo off

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

powershell -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
pause
