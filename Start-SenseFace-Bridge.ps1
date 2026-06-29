$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python -ErrorAction Stop).Source
$port = if ($env:SENSEFACE_PORT) { $env:SENSEFACE_PORT } else { '8090' }
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Host "Port $port is already in use by PID $($listener.OwningProcess). Stop that application first." -ForegroundColor Red
    exit 1
}
Set-Location $root
Write-Host "Starting SenseFace HRMS Bridge on port $port..."
Write-Host "Keep this window open. Press Ctrl+C to stop."
& $python -u "$root\server.py"
