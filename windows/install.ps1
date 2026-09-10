# magazine installer (Windows)
#
#   irm https://raw.githubusercontent.com/shirakawayohane/magazine/main/windows/install.ps1 | iex
#
# Or run from a clone:
#   powershell -ExecutionPolicy Bypass -File windows\install.ps1
$ErrorActionPreference = "Stop"

$RepoUrl = if ($env:MAGAZINE_REPO) { $env:MAGAZINE_REPO } else { "https://github.com/shirakawayohane/magazine" }
$SrcDir  = if ($env:MAGAZINE_SRC)  { $env:MAGAZINE_SRC }  else { Join-Path $env:USERPROFILE ".local\share\magazine" }
$BinDir  = Join-Path $env:USERPROFILE ".local\bin"

function Say  ($m) { Write-Host $m -ForegroundColor White }
function Ok   ($m) { Write-Host "  + $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  ! $m" -ForegroundColor Yellow }

$py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "Python is required: https://www.python.org/downloads/" }
& $py -c "import sys; sys.exit(sys.version_info < (3, 10))"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10+ is required" }

# Detect a local clone versus a downloaded installer.
$here = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { $null }
if ($here -and (Test-Path (Join-Path $here "mag.py"))) {
    $SrcDir = $here
    Say "Installing magazine from $SrcDir"
} else {
    Say "Installing magazine"
    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) { throw "Git is required" }
    if (Test-Path (Join-Path $SrcDir ".git")) {
        git -C $SrcDir pull --ff-only --quiet
        if ($LASTEXITCODE -ne 0) { throw "Could not update the source; existing installation was left in place" }
        Ok "Updated source: $SrcDir"
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SrcDir) | Out-Null
        git clone --depth 1 --quiet $RepoUrl $SrcDir
        if ($LASTEXITCODE -ne 0) { throw "Could not clone the source" }
        Ok "Cloned source: $SrcDir"
    }
}

$mag = Join-Path $SrcDir "mag.py"
if (-not (Test-Path $mag)) { throw "mag.py was not found in $SrcDir" }

# Install a command launcher.
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
@"
@echo off
"$py" "$mag" %*
"@ | Set-Content -Path (Join-Path $BinDir "mag.cmd") -Encoding ASCII
Ok "Command: $(Join-Path $BinDir 'mag.cmd')"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$BinDir", "User")
    Ok "Added $BinDir to PATH. Open a new terminal to use it."
}

# Connect Claude Code's status line when its settings directory exists.
if (Test-Path (Join-Path $env:USERPROFILE ".claude")) {
    & $py $mag install-statusline | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok "Connected the Claude Code status line" } else { Warn "Status-line setup failed; run mag install-statusline to retry" }
}

Write-Host ""
Say "Optional: enable background account switching"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$SrcDir\windows\register-task.ps1`""
Write-Host ""
Say "Next steps"
Write-Host @"
  1. Sign in and register each account you want to use:
       claude auth login   ->  mag add main
       codex login         ->  mag add codex-main --provider codex
  2. Check usage:
       mag limits
  3. Inspect your setup:
       mag doctor
"@
