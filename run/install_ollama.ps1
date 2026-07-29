[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ProjectDir  # go up from run\ to root
Set-Location $ProjectDir

if (Test-Path "ollama\bin\ollama.exe") {
    Write-Host "[OK] Ollama already exists" -ForegroundColor Green
    exit 0
}

Write-Host "[*] Downloading Ollama (Windows)..." -ForegroundColor Cyan
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
        Write-Host "[OK] Ollama installed to ollama\bin\" -ForegroundColor Green
    } else {
        Write-Host "[!] ollama.exe not found in extracted files." -ForegroundColor Red
    }
    Remove-Item $ZipPath -Force -ErrorAction SilentlyContinue
    Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue
} catch {
    Write-Host "[!] Failed to download Ollama. Error: $_" -ForegroundColor Red
    Write-Host "[!] You can manually download from https://github.com/ollama/ollama/releases" -ForegroundColor Yellow
    exit 1
}
