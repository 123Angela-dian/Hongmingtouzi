param(
    [int]$Port = 8030
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Entry = Join-Path $Root "serve_api_logged.py"

if (-not (Test-Path $Pythonw)) {
    throw "pythonw.exe not found: $Pythonw"
}
if (-not (Test-Path $Entry)) {
    throw "entrypoint not found: $Entry"
}

$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    $ownerPid = [int]$listener.OwningProcess
    if ($ownerPid -gt 0) {
        Write-Host "Stopping process on port ${Port}: PID $ownerPid"
        Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
    }
}

Start-Sleep -Milliseconds 700

$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $Pythonw
$psi.Arguments = "`"$Entry`""
$psi.WorkingDirectory = $Root
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.EnvironmentVariables["ASSET_RADAR_PORT"] = [string]$Port

$process = [System.Diagnostics.Process]::Start($psi)
Write-Host "Started Asset Radar API: PID $($process.Id), port $Port"

Start-Sleep -Seconds 1
$active = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($active) {
    Write-Host "Asset Radar API is listening on http://127.0.0.1:$Port"
} else {
    Write-Warning "API process started but port $Port is not listening yet. Check data\logs\api_stderr.log"
}
