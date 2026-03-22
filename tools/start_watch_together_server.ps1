param(
    [string]$ProjectDir = "D:\project\watch_together_local",
    [string]$PythonExe = "D:\project\watch_together_local\.venv\Scripts\python.exe",
    [int[]]$Ports = @(8090, 8091),
    [string]$BindHost = "::"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$logDir = Join-Path $ProjectDir "log\debug"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

foreach ($Port in $Ports) {
    $outLog = if ($Port -eq 8091) {
        Join-Path $logDir "server_8091_admin.log"
    } else {
        Join-Path $logDir "server_$Port.log"
    }
    $errLog = if ($Port -eq 8091) {
        Join-Path $logDir "server_8091_admin_err.log"
    } else {
        Join-Path $logDir "server_${Port}_err.log"
    }

    # Stop older uvicorn process on target port.
    $pids = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique

    foreach ($procId in @($pids)) {
        try {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction Stop
            if ($proc.Name -match "python" -and $proc.CommandLine -match "uvicorn" -and $proc.CommandLine -match "--port\s+$Port") {
                Stop-Process -Id $procId -Force
                Start-Sleep -Milliseconds 300
            }
        }
        catch {
        }
    }

    Start-Process -FilePath $PythonExe `
        -ArgumentList @("-m", "uvicorn", "app:app", "--host", $BindHost, "--port", "$Port") `
        -WorkingDirectory $ProjectDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog
}
