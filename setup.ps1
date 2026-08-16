# ============================================================
# Nascence Huiye Environment Setup (Windows)
# ============================================================

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

Write-Host "=========================================="
Write-Host "  Nascence Huiye - Environment Setup"
Write-Host "=========================================="

# ---------- 1. Create venv ----------
Write-Host "[*] Creating Python virtual environment..."
if (-not (Test-Path "venv\Scripts\python.exe")) {
    python -m venv venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[!] Failed to create venv. Please ensure Python is installed and in PATH."
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Host "[OK] Virtual environment created"
} else {
    Write-Host "[OK] Virtual environment already exists"
}

$Pip = "$ProjectDir\venv\Scripts\pip.exe"

# ---------- 2. Install dependencies ----------
Write-Host "[*] Installing Python dependencies..."

$HuaweiSource = "https://mirrors.huaweicloud.com/repository/pypi/simple/"
$TsinghuaSource = "https://pypi.tuna.tsinghua.edu.cn/simple"
$OfficialSource = "https://pypi.org/simple/"

# 优先使用华为云 PyPI 镜像源
& $Pip install -r requirements.txt -q -i $HuaweiSource
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] 使用华为源安装失败（可能超时或包不存在）"
    Write-Host "[!] 尝试切换清华源..."
    & $Pip install -r requirements.txt -q -i $TsinghuaSource
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[!] 使用清华源安装失败"
        Write-Host "[!] 尝试使用默认源（Python 官方源）..."
        & $Pip install -r requirements.txt -q -i $OfficialSource
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[!] Dependency installation failed. Check requirements.txt."
            Read-Host "Press Enter to exit"
            exit 1
        }
    }
}
Write-Host "[OK] Python dependencies installed"

# ---------- 3. Install llama.cpp + 3 GGUF models ----------
Write-Host "[*] Installing llama.cpp and GGUF models..."
& powershell -ExecutionPolicy Bypass -File "$ProjectDir\run\install_llama.ps1"

# ---------- 4. Create data directories ----------
New-Item -ItemType Directory -Force -Path "data\test" | Out-Null
Write-Host "[OK] Data directories created"

Write-Host ""
Write-Host "=========================================="
Write-Host "  Setup complete! You can now run"
Write-Host "  'start.bat' to launch the WebUI."
Write-Host "=========================================="
Read-Host "Press Enter to exit"
