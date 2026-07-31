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
& $Pip install -r requirements.txt -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] Dependency installation failed. Check requirements.txt."
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "[OK] Python dependencies installed"

# ---------- 3. Download Ollama (Windows) ----------
if (-not (Test-Path "ollama\bin\ollama.exe")) {
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
            Copy-Item -Path $ExeFile.FullName -Destination "$ProjectDir\ollama\bin\ollama.exe" -Force
            Write-Host "[OK] Ollama installed to ollama\bin\"
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
