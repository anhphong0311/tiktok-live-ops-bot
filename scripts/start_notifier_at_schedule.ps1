$ErrorActionPreference = 'Stop'
$notifierTask = 'TikTok-Zalo-Live-Notifier'

& schtasks.exe /Change /TN $notifierTask /ENABLE
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& schtasks.exe /Run /TN $notifierTask
exit $LASTEXITCODE
