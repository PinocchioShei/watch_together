param(
    [string]$ConfigPath = "D:\project\watch_together_local\tools\ddns_cf_config.json",
    [string]$DdnsScriptPath = "D:\project\watch_together_local\tools\ddns_cf_ipv6.ps1",
    [string]$WifiProfileName = "SEU-WLAN"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsPublicIPv6([string]$IpText) {
    if ([string]::IsNullOrWhiteSpace($IpText)) {
        return $false
    }
    try {
        $ip = [System.Net.IPAddress]::Parse($IpText)
    }
    catch {
        return $false
    }
    if ($ip.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetworkV6) {
        return $false
    }
    if ($IpText -notmatch '^[23][0-9a-fA-F]{0,3}:') {
        return $false
    }
    if ($IpText -like "fe80:*" -or $IpText -like "fc*" -or $IpText -like "fd*" -or $IpText -eq "::1") {
        return $false
    }
    return $true
}

function Get-PreferredPublicIPv6 {
    $localPreferred = Get-NetIPAddress -AddressFamily IPv6 -ErrorAction SilentlyContinue |
        Where-Object {
            (Test-IsPublicIPv6 $_.IPAddress) -and
            $_.AddressState -eq "Preferred" -and
            $_.ValidLifetime -gt 0 -and
            $_.InterfaceAlias -ne "Loopback Pseudo-Interface 1"
        } |
        Sort-Object -Property ValidLifetime -Descending |
        Select-Object -First 1

    if ($localPreferred) {
        return $localPreferred.IPAddress
    }
    return ""
}

function Connect-WifiProfile([string]$ProfileName) {
    try {
        netsh wlan connect name="$ProfileName" | Out-Null
    }
    catch {
    }
}

function Ensure-PublicIPv6 {
    $ip = Get-PreferredPublicIPv6
    if (Test-IsPublicIPv6 $ip) {
        return $ip
    }

    Connect-WifiProfile -ProfileName $WifiProfileName
    Start-Sleep -Seconds 8

    $ip = Get-PreferredPublicIPv6
    if (Test-IsPublicIPv6 $ip) {
        return $ip
    }

    throw "No public IPv6 available after Wi-Fi reconnect attempt"
}

if (-not (Test-Path -LiteralPath $DdnsScriptPath)) {
    throw "DDNS script missing: $DdnsScriptPath"
}

try {
    $ip = Ensure-PublicIPv6
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File $DdnsScriptPath -ConfigPath $ConfigPath | Out-Null
}
catch {
    # keep silent for scheduled background run
}
