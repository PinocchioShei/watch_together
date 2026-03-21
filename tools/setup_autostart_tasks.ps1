param(
    [string]$ProjectDir = "D:\project\watch_together_local"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$serverScript = Join-Path $ProjectDir "tools\start_watch_together_server.ps1"
$ddnsScript = Join-Path $ProjectDir "tools\ddns_cf_ipv6.ps1"

if (-not (Test-Path -LiteralPath $serverScript)) {
    throw "Missing server script: $serverScript"
}
if (-not (Test-Path -LiteralPath $ddnsScript)) {
    throw "Missing DDNS script: $ddnsScript"
}

$serverAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$serverScript`""
$serverTrigger = New-ScheduledTaskTrigger -AtLogOn
$serverSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "WatchTogether-Server-Startup" -Action $serverAction -Trigger $serverTrigger -Settings $serverSettings -Description "Start Watch Together server at user logon" -Force | Out-Null

$ddnsCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ddnsScript`""
schtasks /Create /TN "WatchTogether-DDNS-IPv6" /SC MINUTE /MO 2 /TR "$ddnsCmd" /F | Out-Null

Write-Host "Scheduled tasks installed: WatchTogether-Server-Startup, WatchTogether-DDNS-IPv6"
