# Korea3MinBreakoutTrader - Windows Task Scheduler registration
#   install : powershell -ExecutionPolicy Bypass -File tools\autostart.ps1
#   remove  : powershell -ExecutionPolicy Bypass -File tools\autostart.ps1 -Remove
# Runs as the current user (no admin needed). Mon-Fri 08:00, wakes PC from sleep,
# runs later if the PC was off at 08:00, never starts a second copy.
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$TaskName = "Korea3minTrader_AutoStart"
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

$Pyw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Main = Join-Path $Root "python_app\main.py"
if (-not (Test-Path $Pyw)) {
    Write-Output "ERROR: $Pyw not found. Run setup.bat first."
    exit 1
}
if (-not (Test-Path $Main)) {
    Write-Output "ERROR: $Main not found."
    exit 1
}

$Action = New-ScheduledTaskAction -Execute $Pyw -Argument ('"{0}" --autostart' -f $Main) -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "08:00"
$Settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 9) -MultipleInstances IgnoreNew
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Principal $Principal -Description "Korea3MinBreakoutTrader auto start (Mon-Fri 08:00)" -Force | Out-Null

Write-Output "Registered: $TaskName (Mon-Fri 08:00, user $User, wake from sleep ON)"

# Allow wake timers (AC + battery) on the active power plan, else WakeToRun is ignored.
# RTCWAKE: 0=disable, 1=enable, 2=important timers only (does NOT wake for this task)
try {
    & powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 2>$null
    & powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 2>$null
    & powercfg /setactive SCHEME_CURRENT 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Output "Wake timers: enabled on current power plan" }
    else { Write-Output "WARN: could not enable wake timers - set it manually (README 6장 전원 설정)" }
} catch {
    Write-Output "WARN: could not enable wake timers - set it manually (README 6장 전원 설정)"
}
Write-Output "Check: Task Scheduler > Task Scheduler Library > $TaskName"
