# magazine installer (Windows)
#
#   irm https://raw.githubusercontent.com/shirakawayohane/magazine/main/windows/install.ps1 | iex
#
# もしくはクローンしたディレクトリで:
#   powershell -ExecutionPolicy Bypass -File windows\install.ps1
$ErrorActionPreference = "Stop"

$RepoUrl = if ($env:MAGAZINE_REPO) { $env:MAGAZINE_REPO } else { "https://github.com/shirakawayohane/magazine" }
$SrcDir  = if ($env:MAGAZINE_SRC)  { $env:MAGAZINE_SRC }  else { Join-Path $env:USERPROFILE ".local\share\magazine" }
$BinDir  = Join-Path $env:USERPROFILE ".local\bin"

function Say  ($m) { Write-Host $m -ForegroundColor White }
function Ok   ($m) { Write-Host "  + $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  ! $m" -ForegroundColor Yellow }

$py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "python が必要です (https://www.python.org/downloads/)" }

# クローン内から実行されたか、パイプ実行かを見分ける
$here = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { $null }
if ($here -and (Test-Path (Join-Path $here "mag.py"))) {
    $SrcDir = $here
    Say "magazine をインストールします ($SrcDir)"
} else {
    Say "magazine をインストールします"
    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) { throw "git が必要です" }
    if (Test-Path (Join-Path $SrcDir ".git")) {
        git -C $SrcDir pull --ff-only --quiet
        Ok "ソースを更新: $SrcDir"
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SrcDir) | Out-Null
        git clone --depth 1 --quiet $RepoUrl $SrcDir
        Ok "ソースを取得: $SrcDir"
    }
}

$mag = Join-Path $SrcDir "mag.py"
if (-not (Test-Path $mag)) { throw "mag.py が見つかりません: $SrcDir" }

# mag.cmd を置いて PATH から呼べるようにする
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
@"
@echo off
"$py" "$mag" %*
"@ | Set-Content -Path (Join-Path $BinDir "mag.cmd") -Encoding ASCII
Ok "コマンド: $(Join-Path $BinDir 'mag.cmd')"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$BinDir", "User")
    Ok "PATH に $BinDir を追加（新しいシェルから有効）"
}

# statusLine 連携（走っているセッションの残量を追加コスト無しで拾う）
if (Test-Path (Join-Path $env:USERPROFILE ".claude")) {
    & $py $mag install-statusline | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok "Claude Code の statusLine に連携" } else { Warn "statusLine 連携に失敗" }
}

Write-Host ""
Say "任意: 常駐監視（上限の手前で自動的にアカウントを切り替える）"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$SrcDir\windows\register-task.ps1`""
Write-Host ""
Say "次の手順"
Write-Host @"
  1. 使いたいアカウントにログインして登録する（アカウントの数だけ繰り返す）
       claude auth login   ->  mag add --label main
       codex login         ->  mag add --provider codex --label codex-main
  2. 残量を一覧する
       mag limits
  3. 状態を確認する
       mag doctor
"@
