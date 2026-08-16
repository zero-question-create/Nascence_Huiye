# run/install_llama.ps1
# ========================================================================
# 下载并安装 llama.cpp 及三个 GGUF 模型（Windows）
#
# 安装内容：
#   1. llama.cpp 预编译 Windows 二进制（含 llama-server.exe）
#   2. 文本模型   : Qwen3-4B-Instruct-2507-Q4_K_M.gguf
#   3. 向量模型   : qwen3-embed-0.6b-q8_0.gguf
#                   （注意：必须用 0.6B 版本！向量维度=1024。
#                    若与记忆库/faiss 维度不一致，用控制面板「维护」页的
#                    维度重建工具全量重建即可，无需重装）
#   4. 多模态模型 : Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf + mmproj
#
# 下载源说明：
#   - 模型文件同时支持 huggingface.co 直链 与 国内镜像 hf-mirror.com，
#     脚本会按顺序尝试，任一成功即可。
#   - 若全部失败，会提示手动放置 GGUF 到 models\ 目录。
#
# 幂等：已存在的文件跳过下载。
# ========================================================================

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ProjectDir
Set-Location $ProjectDir

$ModelDir = "$ProjectDir\models"
$LlamaDir = "$ProjectDir\llama"
New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null
New-Item -ItemType Directory -Force -Path $LlamaDir | Out-Null

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Nascence Huiye - llama.cpp Installer" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# ------------------------------------------------------------------------
# 1. llama.cpp 二进制
# ------------------------------------------------------------------------
$LlamaBin = "$LlamaDir\bin\llama-server.exe"
if (Test-Path $LlamaBin) {
    Write-Host "[OK] llama-server already exists" -ForegroundColor Green
} else {
    Write-Host "[*] Downloading llama.cpp (Windows)..."
    # 官方 GitHub Releases 最新版（仓库已迁移到 ggml-org）
    # 备用：AMD 提供的 Windows 预编译包
    $ReleaseApi = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
    $LlamaZip = "$LlamaDir\llama.zip"
    $downloadOk = $false
    try {
        $ReleaseData = Invoke-RestMethod -Uri $ReleaseApi -Headers @{"User-Agent" = "Nascence-Installer"} -ErrorAction Stop
        # 选取 win + x64 + zip 且含 avx2/cpu 的资产（单行脚本块避免跨行续行解析问题）
        $Asset = $ReleaseData.assets | Where-Object { ($_.name -match "win") -and ($_.name -match "x64") -and ($_.name -like "*.zip") -and (($_.name -match "avx2") -or ($_.name -match "cpu")) } | Select-Object -First 1
        if ($Asset) {
            Write-Host "    Found release asset: $($Asset.name)"
            Invoke-WebRequest -Uri $Asset.browser_download_url -OutFile $LlamaZip -UseBasicParsing -ErrorAction Stop
            $downloadOk = $true
        } else {
            Write-Host "[!] 未在最新 release 中找到合适的 Windows 资产，尝试备用源..."
        }
    } catch {
        Write-Host "[!] GitHub API 获取失败: $_"
    }

    # 备用：AMD 提供的预编译包
    if (-not $downloadOk) {
        $AmdUrl = "https://repo.radeon.com/rocm/llama.cpp/windows/rocm-rel-7.2/llama-b7782-windows-rocm-7.2.0-gfx110X-gfx115X-gfx120X-x64.zip"
        try {
            Write-Host "[*] 尝试 AMD 备用源..."
            Invoke-WebRequest -Uri $AmdUrl -OutFile $LlamaZip -UseBasicParsing -ErrorAction Stop
            $downloadOk = $true
        } catch {
            Write-Host "[!] AMD 备用源下载失败: $_"
        }
    }

    if ($downloadOk) {
        Write-Host "[*] Extracting..."
        $TempDir = "$LlamaDir\temp"
        Expand-Archive -Path $LlamaZip -DestinationPath $TempDir -Force -ErrorAction Stop
        $ServerFile = Get-ChildItem -Path $TempDir -Recurse -Filter "llama-server.exe" | Select-Object -First 1
        if ($ServerFile) {
            New-Item -ItemType Directory -Force -Path "$LlamaDir\bin" | Out-Null
            # 复制完整发行包（llama-server.exe 及其依赖 dll）
            Copy-Item -Path "$($ServerFile.Directory)\*" -Destination "$LlamaDir\bin" -Recurse -Force
            Write-Host "[OK] llama.cpp installed to llama\bin" -ForegroundColor Green
        } else {
            Write-Host "[!] llama-server.exe not found in archive." -ForegroundColor Yellow
        }
        Remove-Item $LlamaZip -Force -ErrorAction SilentlyContinue
        Remove-Item $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "[!] 所有 llama.cpp 下载源均失败，请手动下载 llama.cpp release" -ForegroundColor Yellow
        Write-Host "[!] 将 llama-server.exe 及其 dll 放到 llama\bin\ 目录" -ForegroundColor Yellow
    }
}

# ------------------------------------------------------------------------
# 2. 模型下载（GGUF）
#   每个模型给出 [目标文件名, 最小预期大小(字节), HF直链, hf-mirror镜像]
#   已存在文件若小于最小预期大小，视为下载不完整，删除并重新下载。
# ------------------------------------------------------------------------
$ModelFiles = @(
    @{
        Name = "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
        MinSize = 2000000000     # ~2.5GB，Q4_K_M 量化
        Urls = @(
            "https://huggingface.co/DhruvalLabs/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
            "https://hf-mirror.com/DhruvalLabs/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
        )
    },
    @{
        Name = "qwen3-embed-0.6b-q8_0.gguf"
        MinSize = 500000000      # ~640MB
        Urls = @(
            "https://huggingface.co/cstr/qwen3-embed-0.6b-GGUF/resolve/main/qwen3-embed-0.6b-q8_0.gguf",
            "https://hf-mirror.com/cstr/qwen3-embed-0.6b-GGUF/resolve/main/qwen3-embed-0.6b-q8_0.gguf"
        )
    },
    @{
        Name = "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf"
        MinSize = 1500000000     # ~1.9GB
        Urls = @(
            "https://huggingface.co/DhruvalLabs/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf",
            "https://hf-mirror.com/DhruvalLabs/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf"
        )
    },
    @{
        Name = "Qwen2.5-VL-3B-Instruct-mmproj-f16.gguf"
        MinSize = 1000000000     # ~1.3GB
        Urls = @(
            "https://huggingface.co/DhruvalLabs/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-mmproj-f16.gguf",
            "https://hf-mirror.com/DhruvalLabs/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-mmproj-f16.gguf"
        )
    }
)

foreach ($m in $ModelFiles) {
    $Target = Join-Path $ModelDir $m.Name
    if (Test-Path $Target) {
        $size = (Get-Item $Target).Length
        if ($size -ge $m.MinSize) {
            Write-Host "[OK] $($m.Name) already exists ($([math]::Round($size/1MB)) MB)" -ForegroundColor Green
            continue
        }
        # 文件太小 → 视为下载不完整，删除重下
        Write-Host "[!] $($m.Name) 存在但大小异常 ($([math]::Round($size/1MB)) MB)，判定为损坏，重新下载" -ForegroundColor Yellow
        Remove-Item $Target -Force -ErrorAction SilentlyContinue
    }
    $ok = $false
    foreach ($u in $m.Urls) {
        Write-Host "[*] Downloading $($m.Name)"
        Write-Host "    from $u"
        try {
            Invoke-WebRequest -Uri $u -OutFile $Target -UseBasicParsing -ErrorAction Stop
            # 校验大小达到最小预期，否则视为下载失败
            if ((Get-Item $Target).Length -lt $m.MinSize) {
                throw "download incomplete (size below expected)"
            }
            Write-Host "[OK] $($m.Name) downloaded ($([math]::Round((Get-Item $Target).Length/1MB)) MB)" -ForegroundColor Green
            $ok = $true
            break
        } catch {
            Remove-Item $Target -Force -ErrorAction SilentlyContinue
            Write-Host "[!] 该源下载失败: $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
    if (-not $ok) {
        Write-Host "[!] 所有源均失败，请手动下载 $($m.Name) 放入 models\ 目录" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Installer finished." -ForegroundColor Cyan
Write-Host "  llama.cpp : $LlamaDir\bin"
Write-Host "  models    : $ModelDir"
Write-Host "==========================================" -ForegroundColor Cyan
