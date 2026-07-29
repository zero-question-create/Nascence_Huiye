@echo off
setlocal enabledelayedexpansion

set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"

echo ==========================================
echo  Nascence Huiye Control Panel
echo ==========================================

rem --- ensure virtual env ---
if not exist "%PROJECT_DIR%\venv\Scripts\python.exe" (
    powershell -Command "Write-Host '未找到虚拟环境，正在执行 setup.ps1...' -ForegroundColor Cyan"
    powershell -ExecutionPolicy Bypass -File "%PROJECT_DIR%\setup.ps1"
    if not exist "%PROJECT_DIR%\venv\Scripts\python.exe" (
        powershell -Command "Write-Host '[ERROR] 虚拟环境安装失败，请手动运行 setup.ps1' -ForegroundColor Red"
        pause
        exit /b 1
    )
)

rem --- ensure ollama ---
if not exist "%PROJECT_DIR%\ollama\bin\ollama.exe" (
    powershell -Command "Write-Host '未找到 Ollama，正在尝试下载...' -ForegroundColor Yellow"
    powershell -ExecutionPolicy Bypass -File "%PROJECT_DIR%\run\install_ollama.ps1"
    if not exist "%PROJECT_DIR%\ollama\bin\ollama.exe" (
        powershell -Command "Write-Host '[WARN] Ollama 下载失败，可手动放到 ollama\bin\ 下' -ForegroundColor Yellow"
    )
)

"%PROJECT_DIR%\venv\Scripts\python.exe" "%PROJECT_DIR%\control_panel.py"
if %errorlevel% neq 0 (
    powershell -Command "Write-Host '[ERROR] 控制面板异常退出，错误码: %errorlevel%' -ForegroundColor Red"
)

powershell -Command "Write-Host '控制面板已退出。' -ForegroundColor Green"
pause
