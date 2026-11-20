param(
    [string]$TaskName = "TikTok-Zalo-Live-Notifier"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Pythonw = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
$Config = Join-Path $ProjectDir "config.yaml"

if (-not (Test-Path -LiteralPath $Pythonw)) {
    throw "Khong tim thay $Pythonw. Hay chay setup.cmd truoc."
}
if (-not (Test-Path -LiteralPath $Config)) {
    throw "Khong tim thay $Config"
}

$Action = New-ScheduledTaskAction `
    -Execute $Pythonw `
    -Argument "-m tiktok_zalo_bot --config `"$Config`" run" `
    -WorkingDirectory $ProjectDir
$LogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Task Scheduler only applies RestartCount when a task reports a failure. A
# long-running process can also disappear without a useful exit result (for
# example after an app/session interruption). This repeating trigger acts as a
# watchdog: while the bot is running, MultipleInstances=IgnoreNew discards the
# trigger; if the bot has stopped, Windows starts it again within one minute.
$WatchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Triggers = @($LogonTrigger, $WatchdogTrigger)
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Triggers `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Theo doi TikTok LIVE va thong bao qua Zalo PC (chay an, watchdog 1 phut)" `
    -Force

Write-Host "Da tao task '$TaskName' o che do chay an voi watchdog 1 phut. Task chi chay khi user dang dang nhap de Zalo PC co desktop UI."

