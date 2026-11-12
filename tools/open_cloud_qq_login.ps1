param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$sshProcess = $null
$port = 16099
$url = "http://127.0.0.1:$port/webui/"

function Get-CloudWebToken {
    # Capture directly in memory: never echo credentials or save a local config copy.
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = (Get-Command ssh.exe -ErrorAction Stop).Source
    $info.Arguments = '-T -o BatchMode=yes -o ConnectTimeout=10 ygo docker exec napcat cat /app/napcat/config/webui.json'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(15000)) {
            $process.Kill()
            throw 'SSH timed out.'
        }
        if ($process.ExitCode -ne 0) { throw 'SSH failed.' }
        $config = $outputTask.GetAwaiter().GetResult() | ConvertFrom-Json
        if (-not $config.token) { throw 'Missing WebUI token.' }
        return [string]$config.token
    } catch {
        throw 'Cannot read the existing cloud WebUI token over SSH. No login was attempted.'
    } finally {
        $process.Dispose()
    }
}

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
    $webToken = Get-CloudWebToken
    if ($CheckOnly) {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $bytes = [Text.Encoding]::UTF8.GetBytes($webToken + '.napcat')
            $hash = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
            $body = @{ hash = $hash } | ConvertTo-Json -Compress
            $login = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/auth/login" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 8
            if ($login.code -ne 0 -or -not $login.data.Credential) {
                throw 'WebUI authentication requires attention (for example, 2FA).'
            }
        } catch { throw 'WebUI authentication check failed. No QQ login or restart was attempted.' }
        finally { $sha.Dispose(); $body = $null; $login = $null; $hash = $null; $bytes = $null }
        Write-Output 'Cloud login page: HTTP 200. Existing WebUI token authenticated successfully.'
    } else {
        # NapCat's own token-link flow authenticates and replaces this URL with /qq_login.
        # The token is read afresh each time, never embedded in this script or shortcut.
        $loginUrl = $url + 'web_login?token=' + [Uri]::EscapeDataString($webToken)
        try { Start-Process $loginUrl }
        catch { throw 'Cannot open the browser for cloud WebUI authentication.' }
        finally { $loginUrl = $null }
    }
    $webToken = $null
} catch {
    if ($sshProcess -and -not $sshProcess.HasExited) { Stop-Process -Id $sshProcess.Id -ErrorAction SilentlyContinue }
    if ($CheckOnly) { Write-Error $_; exit 1 }
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, 'QQ login', 'OK', 'Error') | Out-Null
    exit 1
}
