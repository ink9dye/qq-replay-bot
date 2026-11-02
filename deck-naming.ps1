param()

# Open the cloud deck-naming web UI through an SSH tunnel.
# The page edits /opt/ygobot/data/bot.sqlite3 on the server.

$server = 'root@101.201.108.188'
$localPort = 18788
$remotePort = 8788

function Test-Port([int]$port) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect('127.0.0.1', $port)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

if (-not (Test-Port $localPort)) {
    Start-Process -FilePath 'ssh' -ArgumentList '-N', '-L', "$localPort`:127.0.0.1:$remotePort", $server -WindowStyle Hidden
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 300
        if (Test-Port $localPort) { break }
    }
}

if (-not (Test-Port $localPort)) {
    Write-Host 'SSH 隧道建立失败：检查网络或服务器是否可达。'
    exit 1
}

$secret = (ssh $server 'sudo -u ygo cat /opt/ygobot/data/statistics-owner.key').Trim()
if (-not $secret) {
    Write-Host '读取服务器 owner key 失败。'
    exit 1
}

Start-Process "http://127.0.0.1:$localPort/statistics/access/$secret?next=/statistics/deck-naming"
Write-Host "已打开云端卡组命名页面（隧道端口 $localPort）。"
exit 0
