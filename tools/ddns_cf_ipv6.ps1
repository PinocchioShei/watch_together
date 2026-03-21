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

function Get-PublicIPv6 {
    $candidates = @(
        "https://api64.ipify.org",
        "https://v6.ident.me",
        "https://ifconfig.co/ip"
    )

    foreach ($u in $candidates) {
        try {
            $text = (Invoke-RestMethod -Uri $u -TimeoutSec 8).ToString().Trim()
            $ip = [System.Net.IPAddress]::Parse($text)
            if ($ip.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6) {
                return $text
            }
        }
        catch {
        }
    }

    $local = Get-NetIPAddress -AddressFamily IPv6 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notlike "fe80:*" -and
            $_.IPAddress -notlike "fc*" -and
            $_.IPAddress -notlike "fd*" -and
            $_.ValidLifetime -gt 0
        } |
        Select-Object -First 1

    if ($local) {
        return $local.IPAddress
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
