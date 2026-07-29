# magazine の常駐監視を Windows のタスクとして登録する（ログオン時に起動）。
#   powershell -ExecutionPolicy Bypass -File windows\register-task.ps1
# 解除:
#   Unregister-ScheduledTask -TaskName magazine-watch -Confirm:$false
$ErrorActionPreference = "Stop"

$mag = Join-Path $env:USERPROFILE ".local\share\magazine\mag.py"
if (-not (Test-Path $mag)) { $mag = Join-Path $PSScriptRoot "..\mag.py" }
if (-not (Test-Path $mag)) { throw "mag.py が見つかりません" }

$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python.exe).Source }

$action  = New-ScheduledTaskAction -Execute $py -Argument "`"$mag`" watch"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "magazine-watch" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Switch AI subscription accounts before they hit a limit" -Force | Out-Null
Write-Host "登録しました: magazine-watch（ログオン時に起動）"
