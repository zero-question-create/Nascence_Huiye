@echo off
setlocal enabledelayedexpansion

title Nascence 辉夜控制面板

set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"
set "QT_PLUGIN_PATH=%PROJECT_DIR%\run\qt-plugins"

echo ==========================================
echo  Nascence 辉夜控制面板
echo  项目目录: %PROJECT_DIR%
echo  关闭此终端即停止全部项目服务
echo ==========================================

if not exist "%PROJECT_DIR%\venv\Scripts\python.exe" (
    echo 未找到项目虚拟环境, 正在执行 setup.ps1...
    powershell -ExecutionPolicy Bypass -File "%PROJECT_DIR%\setup.ps1"
)

echo 正在启动控制面板...
set "PYSIDE_DIR=%PROJECT_DIR%\venv\lib\site-packages\PySide6"
if exist "%PYSIDE_DIR%" (
    set "PATH=%PYSIDE_DIR%;%PATH%"
)
"%PROJECT_DIR%\venv\Scripts\python.exe" "%PROJECT_DIR%\control_panel.py"

echo 控制面板已退出。
pause
