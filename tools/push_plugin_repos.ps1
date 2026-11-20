# push_plugin_repos.ps1
# Publish local plugin repos (dist/plugin_repos/nhy-plugin-*) to public GitHub.
# Requires `gh auth login` or GH_TOKEN.
$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ReposRoot = Join-Path $ProjectDir "dist\plugin_repos"
$Gh = "F:\Program Files\GitHub CLI\gh.exe"
if (-not (Test-Path $Gh)) {
    $Gh = "gh"
}
$Git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $Git) {
    foreach ($candidate in @("F:\Program Files\Git\cmd\git.exe", "C:\Program Files\Git\cmd\git.exe")) {
        if (Test-Path $candidate) { $Git = $candidate; break }
    }
}
if (-not $Git) {
    Write-Host "[ERROR] git executable not found."
    exit 1
}

$ErrorActionPreference = "Continue"
& $Gh auth status *> $null
$AuthExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($AuthExit -ne 0 -and -not $env:GH_TOKEN) {
    Write-Host "[ERROR] GitHub CLI is not logged in. Run: " -ForegroundColor Red
    Write-Host "  `"$Gh`" auth login"
    Write-Host "or set GH_TOKEN and retry."
    exit 1
}

$Owner = (& $Gh api user -q .login 2>$null)
if ($Owner) { $Owner = $Owner.Trim() }
if (-not $Owner) {
    Write-Host "[ERROR] Cannot read GitHub username."
    exit 1
}

Get-ChildItem $ReposRoot -Directory | ForEach-Object {
    $RepoDir = $_.FullName
    $RepoName = $_.Name
    Write-Host "[*] Publishing $RepoName ..."
    Push-Location $RepoDir
    try {
        $Manifest = Get-Content (Join-Path $RepoDir "plugin.json") -Raw | ConvertFrom-Json
        & $Gh repo view "$Owner/$RepoName" 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            & $Gh repo create "$Owner/$RepoName" --public --source . --remote origin --push `
                --description "$($Manifest.description)"
        } else {
            & $Gh repo set-default "$Owner/$RepoName"
            & $Git push origin HEAD --tags
        }
        & $Gh repo edit "$Owner/$RepoName" --add-topic "nhy-plugin" | Out-Null
        & $Git push origin --tags
        Write-Host "[OK] https://github.com/$Owner/$RepoName"
    } finally {
        Pop-Location
    }
}
Write-Host "[OK] All plugin repositories published."

