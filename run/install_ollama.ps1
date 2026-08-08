[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ProjectDir
Set-Location $ProjectDir

$ollamaComplete = $false
if (Test-Path "ollama\bin\ollama.exe") {
    $serverFound = Get-ChildItem -Path "ollama\bin" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq "llama-server.exe" } |
        Select-Object -First 1
    if ($null -ne $serverFound) { $ollamaComplete = $true }
}
if ($ollamaComplete) {
    Write-Host "[OK] Ollama already exists (complete)" -ForegroundColor Green
    exit 0
}
if (Test-Path "ollama\bin\ollama.exe") {
    Write-Host "[!] 检测到旧版不完整安装（缺少 llama-server.exe），正在重新下载完整包..." -ForegroundColor Yellow
}

Write-Host @"

  [$([char]0x26A0)] 正在从 GitHub 下载 Ollama（约 1391MB）
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
        # 复制完整发行包（ollama.exe / llama-server.exe / lib 运行时库），只复制单个 exe 会导致推理服务不可用
        Copy-Item -Path "$($ExeFile.Directory)\*" -Destination "$ProjectDir\ollama\bin" -Recurse -Force
        Write-Host "[OK] Ollama 已安装到 ollama\bin\（含 llama-server 完整组件）" -ForegroundColor Green
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
