param(
    [switch]$Enable,
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $project '.venv\Scripts\pythonw.exe'
$config = Join-Path $project 'config.yaml'
$taskName = 'TikTok-GMV-Hourly-Reporter'
$runValueName = 'TikTokGMVHourlyReporter'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runCommand = '"' + $pythonw + '" -m tiktok_zalo_bot --config "' + $config + '" run-gmv'

if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "Không tìm thấy $pythonw"
}

$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument "-m tiktok_zalo_bot --config `"$config`" run-gmv" `
    -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

try {
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null

    if ($Enable) {
        Enable-ScheduledTask -TaskName $taskName | Out-Null
    } else {
        Disable-ScheduledTask -TaskName $taskName | Out-Null
    }
    if ($Enable -and $StartNow) {
        Start-ScheduledTask -TaskName $taskName
    }
    $state = (Get-ScheduledTask -TaskName $taskName).State
    Write-Output "$taskName mode=TaskScheduler state=$state"
} catch {
    if (-not $Enable) {
        throw
    }
    New-ItemProperty `
        -Path $runKey `
        -Name $runValueName `
        -Value $runCommand `
        -PropertyType String `
        -Force | Out-Null
    if ($StartNow) {
        Start-Process `
            -FilePath $pythonw `
            -ArgumentList @('-m', 'tiktok_zalo_bot', '--config', $config, 'run-gmv') `
            -WorkingDirectory $project `
            -WindowStyle Hidden
    }
    Write-Output "$taskName mode=HKCU-Run state=Enabled"
    Write-Warning "Task Scheduler unavailable; installed the per-user logon fallback."
}
