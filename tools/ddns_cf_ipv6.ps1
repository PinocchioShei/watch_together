param(
    [string]$ConfigPath = "D:\project\watch_together_local\tools\ddns_cf_config.json"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Config file not found: $ConfigPath"
}

$cfg = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json

if ([string]::IsNullOrWhiteSpace($cfg.api_token) -or [string]::IsNullOrWhiteSpace($cfg.zone_id) -or [string]::IsNullOrWhiteSpace($cfg.record_name)) {
    throw "Config must include api_token, zone_id, and record_name"
}

$ttl = if ($cfg.ttl) { [int]$cfg.ttl } else { 120 }
$proxied = if ($null -ne $cfg.proxied) { [bool]$cfg.proxied } else { $false }

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
    # Global unicast IPv6 is 2000::/3 (usually starts with 2xxx or 3xxx).
    if ($IpText -notmatch '^[23][0-9a-fA-F]{0,3}:') {
        return $false
    }
    if ($IpText -like "fe80:*" -or $IpText -like "fc*" -or $IpText -like "fd*" -or $IpText -eq "::1") {
        return $false
    }
    return $true
}

function Get-PublicIPv6 {
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

    $candidates = @(
        "https://api64.ipify.org",
        "https://v6.ident.me",
        "https://ifconfig.co/ip"
    )

    foreach ($u in $candidates) {
        try {
            $text = (Invoke-RestMethod -Uri $u -TimeoutSec 8).ToString().Trim()
            if (Test-IsPublicIPv6 $text) {
                return $text
            }
        }
        catch {
        }
    }

    throw "Unable to determine a usable public IPv6 address"
}

$ipv6Text = Get-PublicIPv6

$headers = @{
    Authorization = "Bearer $($cfg.api_token)"
    "Content-Type" = "application/json"
}

$queryUri = "https://api.cloudflare.com/client/v4/zones/$($cfg.zone_id)/dns_records?type=AAAA&name=$($cfg.record_name)"
$query = Invoke-RestMethod -Method Get -Uri $queryUri -Headers $headers

if (-not $query.success) {
    throw "Cloudflare query failed"
}

$record = $null
if ($query.result -and $query.result.Count -gt 0) {
    $record = $query.result[0]
}

if ($record -and $record.content -eq $ipv6Text) {
    Write-Host "No change. AAAA already $ipv6Text"
    exit 0
}

$body = @{
    type = "AAAA"
    name = $cfg.record_name
    content = $ipv6Text
    ttl = $ttl
    proxied = $proxied
} | ConvertTo-Json

if ($record) {
    $updateUri = "https://api.cloudflare.com/client/v4/zones/$($cfg.zone_id)/dns_records/$($record.id)"
    $resp = Invoke-RestMethod -Method Put -Uri $updateUri -Headers $headers -Body $body
    if (-not $resp.success) {
        throw "Cloudflare update failed"
    }
    Write-Host "Updated AAAA $($cfg.record_name) -> $ipv6Text"
}
else {
    $createUri = "https://api.cloudflare.com/client/v4/zones/$($cfg.zone_id)/dns_records"
    $resp = Invoke-RestMethod -Method Post -Uri $createUri -Headers $headers -Body $body
    if (-not $resp.success) {
        throw "Cloudflare create failed"
    }
    Write-Host "Created AAAA $($cfg.record_name) -> $ipv6Text"
}
