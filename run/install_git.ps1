# install_git.ps1
# NHY bundled portable Git (MinGit). Extracts to run/lib/git only.
# The plugin marketplace prefers this git for clone/fetch/update.

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$GitRoot = Join-Path $ProjectDir "run\lib\git"
$GitExe = Join-Path $GitRoot "cmd\git.exe"
$SystemGitMarker = Join-Path $GitRoot "using_system_git.txt"
$PinnedTag = "v2.47.1.windows.1"

function Find-SystemGit {
    $cmd = Get-Command git -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }
    foreach ($candidate in @("F:\Program Files\Git\cmd\git.exe", "C:\Program Files\Git\cmd\git.exe")) {
        if (Test-Path $candidate) { return $candidate }
    }
    return ""
}

function Mark-SystemGit($path) {
    New-Item -ItemType Directory -Force -Path $GitRoot | Out-Null
    Set-Content -Path $SystemGitMarker -Value ("path=" + $path + "`nupdated=" + (Get-Date -Format "s")) -Encoding UTF8
}

function Test-GitReady {
    if (-not (Test-Path $GitExe)) { return $false }
    try {
        & $GitExe --version | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

if (Test-GitReady) {
    Remove-Item $SystemGitMarker -Force -ErrorAction SilentlyContinue
    Write-Host "[OK] Internal git already exists: $GitExe"
    exit 0
}

# If system git is available, do not retry the 45MB MinGit download every launch.
# Set NHY_FORCE_INTERNAL_GIT=1 to force the bundled MinGit install.
$SystemGit = Find-SystemGit
if ($SystemGit -and $env:NHY_FORCE_INTERNAL_GIT -ne "1") {
    Mark-SystemGit $SystemGit
    Write-Host "[OK] Using system Git: $SystemGit (set NHY_FORCE_INTERNAL_GIT=1 to install bundled MinGit)"
    exit 0
}

Write-Host "[*] Installing internal Git (MinGit) ..."
$Release = $null
try {
    $Release = Invoke-RestMethod -Uri "https://api.github.com/repos/git-for-windows/git/releases/tags/$PinnedTag" -UseBasicParsing -TimeoutSec 30 -ErrorAction Stop
} catch {
    Write-Host "[!] Pinned Git release unavailable, falling back to latest."
    $Release = Invoke-RestMethod -Uri "https://api.github.com/repos/git-for-windows/git/releases/latest" -UseBasicParsing -TimeoutSec 30 -ErrorAction Stop
}

$Asset = $Release.assets | Where-Object {
    $_.name -match "^MinGit-.*-64-bit\.zip$"
} | Select-Object -First 1
if (-not $Asset) {
    Write-Host "[ERROR] No MinGit 64-bit zip asset found in release $($Release.tag_name)."
    exit 1
}

$DownloadUrl = $Asset.browser_download_url
$Digest = [string]$Asset.digest
$Expected = ""
if ($Digest -match "^sha256:(.+)$") { $Expected = $Matches[1].ToLower() }

$TempRoot = Join-Path $ProjectDir "run\lib\git_download"
$ZipPath = Join-Path $TempRoot $Asset.name
New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
try {
    Write-Host "[*] Downloading $($Asset.name) ..."
    $Curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
    $Downloaded = $false
    if ($Curl) {
        & $Curl -L --fail --max-time 90 --retry 1 -o $ZipPath $DownloadUrl
        $Downloaded = ($LASTEXITCODE -eq 0) -and (Test-Path $ZipPath)
    } else {
        try {
            Invoke-WebRequest -Uri $DownloadUrl -OutFile $ZipPath -UseBasicParsing -TimeoutSec 90 -ErrorAction Stop
            $Downloaded = Test-Path $ZipPath
        } catch {
            $Downloaded = $false
        }
    }
    if (-not $Downloaded) {
        Remove-Item $ZipPath -Force -ErrorAction SilentlyContinue
        $SystemGit = Get-Command git -ErrorAction SilentlyContinue
        if (-not $SystemGit) {
            foreach ($candidate in @("F:\Program Files\Git\cmd\git.exe", "C:\Program Files\Git\cmd\git.exe")) {
                if (Test-Path $candidate) {
                    $SystemGit = [pscustomobject]@{ Source = $candidate }
                    break
                }
            }
        }
        if ($SystemGit) {
            Mark-SystemGit $SystemGit.Source
            Write-Host "[WARN] Internal Git download failed; falling back to system git: $($SystemGit.Source)"
            exit 0
        }
        Write-Host "[ERROR] Internal Git download failed and system git is unavailable."
        exit 1
    }
    if ($Expected) {
        $Actual = (Get-FileHash -Algorithm SHA256 -Path $ZipPath).Hash.ToLower()
        if ($Actual -ne $Expected) {
            Write-Host "[ERROR] Git archive SHA256 mismatch."
            exit 1
        }
        Write-Host "[OK] SHA256 verified"
    } else {
        Write-Host "[WARN] Release asset has no digest; continuing with HTTPS download."
    }

    $ExtractDir = Join-Path $TempRoot "extract"
    Remove-Item $ExtractDir -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force -ErrorAction Stop
    $Found = Get-ChildItem -Path $ExtractDir -Recurse -Filter "git.exe" -File |
        Where-Object { $_.DirectoryName -match "\\cmd$" } |
        Select-Object -First 1
    if (-not $Found) {
        Write-Host "[ERROR] git.exe not found in MinGit archive."
        exit 1
    }
    $SourceRoot = Split-Path -Parent (Split-Path -Parent $Found.FullName)
    if (Test-Path $GitRoot) { Remove-Item $GitRoot -Recurse -Force }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $GitRoot) | Out-Null
    Copy-Item -Path (Join-Path $SourceRoot "*") -Destination $GitRoot -Recurse -Force
    if (-not (Test-GitReady)) {
        Write-Host "[ERROR] Internal git install failed."
        exit 1
    }
    Remove-Item $SystemGitMarker -Force -ErrorAction SilentlyContinue
    Write-Host "[OK] Internal Git installed: $GitExe"
} finally {
    Remove-Item $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
