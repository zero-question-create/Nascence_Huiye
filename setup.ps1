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

# ---------- 3. Download Ollama (Windows) ----------
$ollamaComplete = $false
if (Test-Path "ollama\bin\ollama.exe") {
    $serverFound = Get-ChildItem -Path "ollama\bin" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq "llama-server.exe" } |
        Select-Object -First 1
    if ($null -ne $serverFound) { $ollamaComplete = $true }
}
if (-not $ollamaComplete) {
    Write-Host "[*] Downloading Ollama (Windows)..."
    try {
        $ReleaseApi = "https://api.github.com/repos/ollama/ollama/releases/latest"
        $ReleaseData = Invoke-RestMethod -Uri $ReleaseApi -ErrorAction Stop
        $Tag = $ReleaseData.tag_name
        Write-Host "    Version: $Tag"
        $ZipUrl = "https://github.com/ollama/ollama/releases/download/${Tag}/ollama-windows-amd64.zip"
        $ZipPath = "$ProjectDir\ollama\ollama.zip"
        New-Item -ItemType Directory -Force -Path "$ProjectDir\ollama" | Out-Null
        Write-Host "[*] Downloading..."
        Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipPath -UseBasicParsing -ErrorAction Stop
        Write-Host "[*] Extracting..."
        $TempDir = "$ProjectDir\ollama\temp"
        Expand-Archive -Path $ZipPath -DestinationPath $TempDir -Force -ErrorAction Stop
        $ExeFile = Get-ChildItem -Path $TempDir -Recurse -Filter "ollama.exe" | Select-Object -First 1
        if ($ExeFile) {
            New-Item -ItemType Directory -Force -Path "$ProjectDir\ollama\bin" | Out-Null
            # 复制完整发行包（ollama.exe / llama-server.exe / lib 运行时库），只复制单个 exe 会导致推理服务不可用
            Copy-Item -Path "$($ExeFile.Directory)\*" -Destination "$ProjectDir\ollama\bin" -Recurse -Force
            Write-Host "[OK] Ollama installed to ollama\bin\ (full package incl. llama-server)"
        } else {
            Write-Host "[!] ollama.exe not found in extracted files."
        }
        Remove-Item $ZipPath -Force -ErrorAction SilentlyContinue
        Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Host "[!] Failed to download Ollama. Error: $_"
        Write-Host "[!] You can manually download ollama-windows-amd64.zip and place ollama.exe in ollama\bin\"
    }
} else {
    Write-Host "[OK] Ollama already exists"
}

# ---------- 4. Create data directories ----------
New-Item -ItemType Directory -Force -Path "data\test" | Out-Null
Write-Host "[OK] Data directories created"

# ---------- 5. (Optional) Pull embedding model ----------
$OllamaBin = "$ProjectDir\ollama\bin\ollama.exe"
if (Test-Path $OllamaBin) {
    Write-Host "[*] Starting Ollama and pulling embedding model..."
    $env:OLLAMA_HOME = "$ProjectDir\ollama\home"
    $env:OLLAMA_MODELS = "$ProjectDir\ollama\home\models"
    New-Item -ItemType Directory -Force -Path $env:OLLAMA_HOME | Out-Null

    $OllamaProc = Start-Process -FilePath $OllamaBin -ArgumentList "serve" -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 3

    $Model = "shaw/dmeta-embedding-zh"
    try {
        $TagList = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -ErrorAction Stop
        $Exists = $TagList.models | Where-Object { $_.name -like "*dmeta-embedding-zh*" }
    } catch {
        $Exists = $null
    }
    if (-not $Exists) {
        Write-Host "[*] Pulling model $Model (about 400MB, first time may be slow)..."
        $PullBody = @{ model = $Model } | ConvertTo-Json
        Invoke-RestMethod -Uri "http://localhost:11434/api/pull" -Method Post -Body $PullBody -ContentType "application/json" | Out-Null
        Write-Host "[OK] Embedding model pulled"
    } else {
        Write-Host "[OK] Embedding model already exists"
    }

    Stop-Process -Id $OllamaProc.Id -Force -ErrorAction SilentlyContinue
    Write-Host "[OK] Ollama service stopped"
} else {
    Write-Host "[*] Ollama not installed, skipping model pull."
}

Write-Host ""
Write-Host "=========================================="
Write-Host "  Setup complete! You can now run"
Write-Host "  'start.bat' to start the project."
Write-Host "=========================================="
Read-Host "Press Enter to exit"
