# Register magazine's background monitor to start at logon.
#   powershell -ExecutionPolicy Bypass -File windows\register-task.ps1
# Remove:
#   Unregister-ScheduledTask -TaskName magazine-watch -Confirm:$false
$ErrorActionPreference = "Stop"

$mag = Join-Path $env:USERPROFILE ".local\share\magazine\mag.py"
if (-not (Test-Path $mag)) { $mag = Join-Path $PSScriptRoot "..\mag.py" }
if (-not (Test-Path $mag)) { throw "mag.py was not found" }

$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python.exe).Source }

$action  = New-ScheduledTaskAction -Execute $py -Argument "`"$mag`" watch"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "magazine-watch" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Switch AI subscription accounts before they hit a limit" -Force | Out-Null
Write-Host "Registered magazine-watch (starts at logon)."
