[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ProjectDir
Set-Location $ProjectDir

if (Test-Path "ollama\bin\ollama.exe") {
    Write-Host "[OK] Ollama already exists" -ForegroundColor Green
    exit 0
}

Write-Host @"

  [$([char]0x26A0)] 正在从 GitHub 下载 Ollama（约 700MB）
  |  国内访问 GitHub 可能较慢，请耐心等待。
  |  如需取消请按 Ctrl+C，然后手动下载 ollama-windows-amd64.zip
  |  解压后将 ollama.exe 放到 ollama\bin\ 下即可。
  |  下载地址: https://github.com/ollama/ollama/releases/latest
  |
  |  正在等待下载开始...

"@ -ForegroundColor Yellow

try {
    $ReleaseApi = "https://api.github.com/repos/ollama/ollama/releases/latest"
    $ReleaseData = Invoke-RestMethod -Uri $ReleaseApi -ErrorAction Stop
    $Tag = $ReleaseData.tag_name
    $ZipUrl = "https://github.com/ollama/ollama/releases/download/${Tag}/ollama-windows-amd64.zip"
    $ZipPath = "$ProjectDir\ollama\ollama.zip"
    New-Item -ItemType Directory -Force -Path "$ProjectDir\ollama" | Out-Null
    Write-Host "[*] 正在下载版本 $Tag ..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipPath -UseBasicParsing -ErrorAction Stop
    $downloaded = (Get-Item $ZipPath).Length
    Write-Host "[OK] 下载完成 ($([math]::Round($downloaded / 1MB, 1)) MB)" -ForegroundColor Green
    Write-Host "[*] 正在解压..." -ForegroundColor Cyan
    $TempDir = "$ProjectDir\ollama\temp"
    Expand-Archive -Path $ZipPath -DestinationPath $TempDir -Force -ErrorAction Stop
    $ExeFile = Get-ChildItem -Path $TempDir -Recurse -Filter "ollama.exe" | Select-Object -First 1
    if ($ExeFile) {
        New-Item -ItemType Directory -Force -Path "$ProjectDir\ollama\bin" | Out-Null
        Copy-Item -Path $ExeFile.FullName -Destination "$ProjectDir\ollama\bin\ollama.exe" -Force
        Write-Host "[OK] Ollama 已安装到 ollama\bin\" -ForegroundColor Green
    } else {
        Write-Host "[!] 解压后未找到 ollama.exe" -ForegroundColor Red
    }
    Remove-Item $ZipPath -Force -ErrorAction SilentlyContinue
    Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue
} catch {
    Write-Host "[!] Ollama 下载失败: $_" -ForegroundColor Red
    Write-Host "[!] 请手动下载后放到 ollama\bin\ 下" -ForegroundColor Yellow
    exit 1
}
