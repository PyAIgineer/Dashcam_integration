# Opens the SSH tunnels for video (HLS) + telemetry (SSE), then launches the dashboard.
# Both VM ports are bound to loopback there, so this tunnel is the only way in.

$Key    = "C:\Users\ADMIN\Downloads\VM-Teosa_key.pem"
$VmUser = "azureuser"
$VmHost = "20.197.4.129"
$Page   = Join-Path $PSScriptRoot "viewer.html"

Write-Host "Opening tunnel  localhost:8888 -> HLS video" -ForegroundColor Cyan
Write-Host "                localhost:8099 -> telemetry" -ForegroundColor Cyan

$ssh = Start-Process ssh -PassThru -WindowStyle Minimized -ArgumentList @(
    "-i", "`"$Key`"", "-N",
    "-L", "8888:127.0.0.1:8888",
    "-L", "8099:127.0.0.1:8099",
    "-o", "ServerAliveInterval=30",
    "-o", "ExitOnForwardFailure=yes",
    "$VmUser@$VmHost"
)

# give the forwards a moment before the page starts requesting through them
Start-Sleep -Seconds 3
if ($ssh.HasExited) {
    Write-Host "Tunnel failed to start (ports 8888/8099 already in use?)." -ForegroundColor Red
    exit 1
}

Start-Process $Page
Write-Host ""
Write-Host "Dashboard open. Tunnel PID $($ssh.Id)." -ForegroundColor Green
Write-Host "Press Ctrl+C here (or run: Stop-Process -Id $($ssh.Id)) to close the tunnel." -ForegroundColor DarkGray

try   { Wait-Process -Id $ssh.Id }
finally {
    if (-not $ssh.HasExited) { Stop-Process -Id $ssh.Id -Force }
    Write-Host "Tunnel closed." -ForegroundColor DarkGray
}
