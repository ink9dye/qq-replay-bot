param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$sshProcess = $null
$port = 16099
$url = "http://127.0.0.1:$port/webui/"

try {
    # Reuse only a tunnel created with these exact forwarding arguments.
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
    foreach ($listener in $listeners) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        if ($listener.LocalAddress -ne '127.0.0.1' -or $owner.Name -ne 'ssh.exe' -or
            $owner.CommandLine -notmatch '127\.0\.0\.1:16099:127\.0\.0\.1:6099' -or
            $owner.CommandLine -notmatch '\s+ygo(?:\s|$)') {
            throw 'Local port 16099 is occupied by another application. Close that application and retry.'
        }
    }
    if ($listeners.Count -eq 0) {
        $sshExe = (Get-Command ssh.exe -ErrorAction Stop).Source
        $sshArgs = @('-N', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3', '-L', '127.0.0.1:16099:127.0.0.1:6099', 'ygo')
        $sshProcess = Start-Process -FilePath $sshExe -ArgumentList $sshArgs -WindowStyle Hidden -PassThru
    }
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ($sshProcess -and $sshProcess.HasExited) { break }
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'Cannot reach the cloud QQ login page. Check your network and SSH access to ygo, then retry.' }
    if ($CheckOnly) {
        Write-Output 'Cloud login page: HTTP 200. SSH tunnel ready on loopback port 16099.'
    } else {
        # Open the existing server instance. Never start/restart QQ or include credentials in a URL.
        Start-Process $url
    }
} catch {
    if ($sshProcess -and -not $sshProcess.HasExited) { Stop-Process -Id $sshProcess.Id -ErrorAction SilentlyContinue }
    if ($CheckOnly) { Write-Error $_; exit 1 }
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, 'QQ login', 'OK', 'Error') | Out-Null
    exit 1
}
