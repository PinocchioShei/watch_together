param(
    [string]$ProjectDir = "D:\project\watch_together_local"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$guardScript = Join-Path $ProjectDir "tools\ddns_cf_ensure_network.ps1"

if (-not (Test-Path -LiteralPath $guardScript)) {
    throw "Missing guard script: $guardScript"
}

$taskName = "WatchTogether-DDNS-Guard"
$taskCmd = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$guardScript`""

schtasks /Create /TN $taskName /SC MINUTE /MO 5 /TR "$taskCmd" /F | Out-Null
try {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    $task.Settings.Hidden = $true
    Set-ScheduledTask -InputObject $task | Out-Null
}
catch {
}
Write-Host "Scheduled task installed: $taskName (every 5 minutes, hidden)"
