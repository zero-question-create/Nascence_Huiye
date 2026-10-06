@echo off
setlocal enabledelayedexpansion

title Nascence Huiye - Starting...

echo ==========================================
echo  Nascence Huiye - Starting...
echo ==========================================

set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"

:: ---------- 0. Check system Python ----------
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

:: ---------- 1. Check venv ----------
if not exist "%PROJECT_DIR%venv\Scripts\python.exe" (
    echo [Error] Virtual env not found. Please run setup.ps1 first.
    pause
    exit /b 1
)
echo [OK] Virtual env found

:: ---------- 2. Ensure data dirs ----------
if not exist "%PROJECT_DIR%data\test" mkdir "%PROJECT_DIR%data\test"

:: ---------- 3. Start Ollama ----------
if defined OLLAMA_BASE_URL (
    set "OLLAMA_TARGET_URL=%OLLAMA_BASE_URL%"
) else (
    set "OLLAMA_TARGET_URL=http://localhost:11434"
)

if not defined OLLAMA_BIN (
    set "OLLAMA_BIN=%PROJECT_DIR%ollama\bin\ollama.exe"
)
set "OLLAMA_SERVER="

:: 优先检查目标 Ollama 服务是否已在运行
powershell -Command "try { $r = Invoke-WebRequest -Uri '%OLLAMA_TARGET_URL%/api/tags' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if "!ERRORLEVEL!"=="0" (
    echo [OK] Ollama already running at %OLLAMA_TARGET_URL%
    goto :ollama_ready
)

if exist "%OLLAMA_BIN%" (
    echo [*] Starting local Ollama service...
    set "OLLAMA_HOME=%PROJECT_DIR%ollama\home"
    if not exist "!OLLAMA_HOME!" mkdir "!OLLAMA_HOME!"
    set "OLLAMA_MODELS=!OLLAMA_HOME!\models"
    if not exist "!OLLAMA_MODELS!" mkdir "!OLLAMA_MODELS!"

    echo [*] Launching Ollama...
    start "Ollama" /B "%OLLAMA_BIN%" serve > nul 2>&1
    set "OLLAMA_SERVER=1"
    echo [*] Waiting for Ollama...
    for /l %%i in (1,1,30) do (
        timeout /t 1 /nobreak >nul
        powershell -Command "try { $r = Invoke-WebRequest -Uri '%OLLAMA_TARGET_URL%/api/tags' -UseBasicParsing -TimeoutSec 1; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
        if "!ERRORLEVEL!"=="0" (
            echo [OK] Ollama ready
            goto :ollama_ready
        )
    )
    echo [Warning] Ollama start timed out, please start manually
) else (
    echo [*] Local Ollama binary not found at %OLLAMA_BIN%
    echo [*] If using remote or host Ollama, ensure service is running at %OLLAMA_TARGET_URL%
)
:ollama_ready

:: ---------- 4. Check embedding model ----------
echo [*] Checking embedding model...
powershell -Command "try { $r = Invoke-WebRequest -Uri '%OLLAMA_TARGET_URL%/api/tags' -UseBasicParsing -TimeoutSec 2; $d = $r.Content | ConvertFrom-Json; $found = $d.models | Where-Object { $_.name -like '*dmeta-embedding-zh*' }; if ($found) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if "!ERRORLEVEL!"=="0" (
    echo [OK] Embedding model exists
) else (
    if exist "%OLLAMA_BIN%" (
        echo [*] Pulling embedding model shaw/dmeta-embedding-zh ...
        start "Ollama Pull" /B "%OLLAMA_BIN%" pull shaw/dmeta-embedding-zh
    )
)

:: ---------- 5. Mode selection ----------
echo.
echo ==========================================
echo  Select mode:
echo    1) CLI Interactive (main.py)
echo    2) QQ Bot (qq_bot.py)
echo ==========================================
echo.

set /p MODE_CHOICE="Choice (1/2, default 1): "
if "!MODE_CHOICE!"=="" set MODE_CHOICE=1

call "%PROJECT_DIR%venv\Scripts\activate.bat"

if "!MODE_CHOICE!"=="1" (
    echo [*] Starting CLI mode...
    python main.py
) else if "!MODE_CHOICE!"=="2" (
    echo [*] Starting QQ Bot mode...
    python qq_bot.py
) else (
    echo [*] Invalid choice, starting CLI mode...
    python main.py
)

call deactivate
pause
