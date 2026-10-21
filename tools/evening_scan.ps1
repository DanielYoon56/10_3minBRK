# Korea3MinBreakoutTrader - 18:00 accumulation scan, Task Scheduler registration
#   install : powershell -ExecutionPolicy Bypass -File tools\evening_scan.ps1
#   remove  : powershell -ExecutionPolicy Bypass -File tools\evening_scan.ps1 -Remove
# Runs as the current user (no admin needed). Mon-Fri 18:00, shows a console window
# with the result (stays open until Enter) and sends it to Telegram.
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$TaskName = "Korea3minTrader_EveningScan"
$Root = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed: $TaskName"
    } else {
        Write-Output "Not registered: $TaskName"
    }
    exit 0
}

$Bat = Join-Path $Root "evening_scan.bat"
if (-not (Test-Path (Join-Path $Root ".venv\Scripts\python.exe"))) {
    Write-Output "ERROR: .venv not found. Run setup.bat first."
    exit 1
}
if (-not (Test-Path $Bat)) {
    Write-Output "ERROR: $Bat not found."
    exit 1
}

$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument ('/c ""{0}""' -f $Bat) -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "18:00"
$Settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12) -MultipleInstances IgnoreNew
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Principal $Principal -Description "Korea3MinBreakoutTrader 18:00 accumulation scan + Telegram (Mon-Fri)" -Force | Out-Null

Write-Output "Registered: $TaskName (Mon-Fri 18:00, user $User, wake from sleep ON)"
Write-Output "Check: Task Scheduler > Task Scheduler Library > $TaskName"
