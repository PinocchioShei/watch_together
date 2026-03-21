param(
    [string]$ScriptPath = "D:\project\watch_together_local\tools\ddns_cf_ipv6.ps1",
    [int]$IntervalSeconds = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

while ($true) {
    try {
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ScriptPath | Out-Null
    }
    catch {
    }
    Start-Sleep -Seconds $IntervalSeconds
}
