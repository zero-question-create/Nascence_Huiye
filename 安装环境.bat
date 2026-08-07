@echo off
chcp 936 >nul

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
    echo [ERROR] 未检测到 Python 运行环境！
    echo.
    echo 请到 https://www.python.org/downloads/ 下载并安装 Python，
    echo 安装时务必勾选 "Add Python to PATH" 选项，
    echo 安装完成后请重新打开终端再运行本脚本。
    echo.
    pause
    exit /b 1
)
echo [OK] Python 运行环境已就绪

powershell -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
pause