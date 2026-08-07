@echo off
chcp 936 >nul

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

rem --- Qt plugin path (fix "could not find platform plugin" error) ---
set "QT_PLUGIN_PATH=%PROJECT_DIR%\venv\Lib\site-packages\PyQt5\Qt5\plugins"

echo ==========================================
echo  Nascence Huiye Control Panel
echo ==========================================

rem --- ensure virtual env ---
if not exist "%PROJECT_DIR%\venv\Scripts\python.exe" (
    powershell -Command "Write-Host '未找到虚拟环境，正在执行 setup.ps1...' -ForegroundColor Cyan"
    powershell -ExecutionPolicy Bypass -File "%PROJECT_DIR%\setup.ps1"
    if not exist "%PROJECT_DIR%\venv\Scripts\python.exe" (
        powershell -Command "Write-Host '[ERROR] 环境安装失败，请手动运行 setup.ps1' -ForegroundColor Red"
        pause
        exit /b 1
    )
)

rem --- ensure ollama ---
if not exist "%PROJECT_DIR%\ollama\bin\ollama.exe" (
    powershell -Command "Write-Host '未找到 Ollama，正在重新下载安装...' -ForegroundColor Yellow"
    powershell -ExecutionPolicy Bypass -File "%PROJECT_DIR%\run\install_ollama.ps1"
    if not exist "%PROJECT_DIR%\ollama\bin\ollama.exe" (
        powershell -Command "Write-Host '[WARN] Ollama 安装失败，请手动放到 ollama\bin\ 中' -ForegroundColor Yellow"
    )
)

"%PROJECT_DIR%\venv\Scripts\python.exe" "%PROJECT_DIR%\control_panel.py"
if %errorlevel% neq 0 (
    powershell -Command "Write-Host '[ERROR] 程序运行异常退出，错误码: %errorlevel%' -ForegroundColor Red"
)

powershell -Command "Write-Host '程序已正常退出' -ForegroundColor Green"
pause
