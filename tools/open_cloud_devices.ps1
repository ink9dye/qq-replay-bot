# Open the cloud device dashboard through an authenticated SSH tunnel.
$ErrorActionPreference = 'Stop'
$taskPort = 18788
$taskSecret = (& ssh -T -o BatchMode=yes -o ConnectTimeout=10 ygo 'cat /opt/ygobot/data/statistics-owner.key' | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $taskSecret -notmatch '^[A-Za-z0-9_-]{43}$') { throw 'Could not read the dashboard credential through SSH.' }
if (Get-NetTCPConnection -LocalPort $taskPort -State Listen -ErrorAction SilentlyContinue) { throw "Port $taskPort is already in use. Close the previous dashboard SSH tunnel before reopening." }
$taskTunnel = Start-Process -FilePath 'ssh.exe' -WindowStyle Hidden -PassThru -ArgumentList @('-N','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-L',"127.0.0.1:${taskPort}:127.0.0.1:8788",'ygo')
$taskResponse = $null
for ($taskTry=0; $taskTry -lt 30; $taskTry++) {
    Start-Sleep -Milliseconds 200
    if ($taskTunnel.HasExited) { throw 'SSH tunnel did not start.' }
    try { $taskResponse = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$taskPort/health" -TimeoutSec 2; if ($taskResponse.StatusCode -eq 200) { break } } catch { }
}
if (-not $taskResponse -or $taskResponse.StatusCode -ne 200) { Stop-Process -Id $taskTunnel.Id -ErrorAction SilentlyContinue; throw 'Dashboard tunnel health check failed.' }
Start-Process "http://127.0.0.1:$taskPort/statistics/access/$taskSecret`?next=/statistics/devices"
Write-Host "Dashboard opened. Close the SSH tunnel when finished: Stop-Process -Id $($taskTunnel.Id)"
