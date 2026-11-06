param(
  [string]$HealthUrl = 'http://101.201.108.188:8787/health',
  [string]$ControlUrl = 'http://myip.ipip.net',
  [string]$SshHost = '101.201.108.188',
  [int]$SshPort = 22,
  [string]$DataDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'data\health-watch'),
  [switch]$TestAlert,
  [switch]$Verbose
)

# 本机外部探活。2026-11-06 重构（O6）：只报"需要动作"和"已经出事"，内存压力不再单独打断人。
#  1) 保留：8787 健康 + SSH banner + 对照站；连续 3 次失败邮件+弹窗，恢复通知。
#  2) 内存 PSI 降级：仅当 avg300>=40% 且健康耗时>2s 或非 200 才告警（夜间只发邮件）。
#  3) 新增：OOM kill、服务异常重启、journal Traceback、WAL>8GB、盘可用<8GB 立即告警。
#  4) 新增：每天 09:00 一封资源摘要（available/PSI/WAL/DB/重启次数），只发邮件。

$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$LogPath = Join-Path $DataDir 'watch.log'
$StatePath = Join-Path $DataDir 'state.json'

$WAL_ALERT_BYTES = 2GB
$DISK_ALERT_BYTES = 8GB
$PSI_ALERT_AVG300 = 40.0
$HEALTH_SLOW_SECONDS = 2.0
$COOLDOWN_PSI = 3600
$COOLDOWN_OOM = 1800
$COOLDOWN_RESTART = 1800
$COOLDOWN_TRACE = 1800
$COOLDOWN_WAL = 3600
$COOLDOWN_DISK = 3600

function Write-Log([string]$Message) {
  $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
  Add-Content -Path $LogPath -Value $line -Encoding UTF8
  if ($Verbose) { Write-Host $line }
}

function Get-CurlCode([string]$Url) {
  $raw = & curl.exe --noproxy '*' -4 -s -o NUL -w '%{http_code} %{time_total}' --max-time 8 $Url 2>$null
  $text = ("$raw").Trim()
  $parts = $text.Split(' ')
  $code = if ($parts.Count -ge 1) { $parts[0] } else { '000' }
  $secs = if ($parts.Count -ge 2) { $parts[1] } else { '' }
  return [pscustomobject]@{ Code = $code; Seconds = $secs }
}

function Test-SshBanner {
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $task = $client.ConnectAsync($SshHost, $SshPort)
    if (-not $task.Wait(5000)) { return [pscustomobject]@{ Ok = $false; Detail = 'connect_timeout' } }
    $stream = $client.GetStream()
    $stream.ReadTimeout = 5000
    $buf = New-Object byte[] 64
    try {
      $n = $stream.Read($buf, 0, 64)
      if ($n -le 0) { return [pscustomobject]@{ Ok = $false; Detail = 'no_banner' } }
      $text = [Text.Encoding]::ASCII.GetString($buf, 0, $n).Trim()
      return [pscustomobject]@{ Ok = $text.StartsWith('SSH-'); Detail = $text }
    } catch {
      return [pscustomobject]@{ Ok = $false; Detail = 'banner_timeout' }
    }
  } catch {
    return [pscustomobject]@{ Ok = $false; Detail = $_.Exception.Message }
  } finally {
    $client.Close()
  }
}

function Get-RemoteProbe {
  # 一次 SSH 取全部判据；只读，不写库、不重启。
  $remote = @'
echo MEM $(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
echo SWAP $(awk '/^SwapFree:/{print $2}' /proc/meminfo)
echo PSI $(awk '/^some/{print $2, $4}' /proc/pressure/memory)
echo FULL $(awk '/^full/{print $2, $4}' /proc/pressure/memory)
echo OOM $(awk '/^oom_kill/{print $2}' /proc/vmstat)
echo RESTART $(systemctl show -p NRestarts --value ygo-web) $(systemctl show -p NRestarts --value ygo-collector) $(systemctl show -p NRestarts --value ygo-qq)
echo WAL $(stat -c %s /opt/ygobot/data/bot.sqlite3-wal 2>/dev/null || echo 0)
echo DB $(stat -c %s /opt/ygobot/data/bot.sqlite3 2>/dev/null || echo 0)
echo DISK $(df --output=avail -B1 /opt 2>/dev/null | tail -1)
echo TRACE $(journalctl -u ygo-web -u ygo-collector -u ygo-qq --since '-16 min' --no-pager 2>/dev/null | grep -c Traceback)
echo TRACELAST $(journalctl -u ygo-web -u ygo-collector -u ygo-qq --since '-16 min' --no-pager 2>/dev/null | grep -E 'Error|Exception' | tail -1 | cut -c1-160)
'@
  $out = & ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "root@$SshHost" $remote 2>$null
  if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
  $probe = [pscustomobject]@{
    AvailableMB = -1; SwapFreeMB = -1;
    Psi10 = -1.0; Psi300 = -1.0; Full10 = -1.0; Full300 = -1.0;
    OomKill = -1;
    RestartWeb = -1; RestartCollector = -1; RestartQq = -1;
    WalBytes = -1; DbBytes = -1; DiskAvail = -1;
    TraceCount = -1; TraceLast = ''
  }
  foreach ($line in @($out)) {
    if ($line -match '^MEM\s+(\d+)') { $probe.AvailableMB = [int]([int64]$Matches[1] / 1024) }
    elseif ($line -match '^SWAP\s+(\d+)') { $probe.SwapFreeMB = [int]([int64]$Matches[1] / 1024) }
    elseif ($line -match '^PSI\s+avg10=([0-9.]+)\s+avg300=([0-9.]+)') { $probe.Psi10 = [double]$Matches[1]; $probe.Psi300 = [double]$Matches[2] }
    elseif ($line -match '^FULL\s+avg10=([0-9.]+)\s+avg300=([0-9.]+)') { $probe.Full10 = [double]$Matches[1]; $probe.Full300 = [double]$Matches[2] }
    elseif ($line -match '^OOM\s+(\d+)') { $probe.OomKill = [int64]$Matches[1] }
    elseif ($line -match '^RESTART\s+(\d+)\s+(\d+)\s+(\d+)') {
      $probe.RestartWeb = [int64]$Matches[1]; $probe.RestartCollector = [int64]$Matches[2]; $probe.RestartQq = [int64]$Matches[3]
    }
    elseif ($line -match '^WAL\s+(\d+)') { $probe.WalBytes = [int64]$Matches[1] }
    elseif ($line -match '^DB\s+(\d+)') { $probe.DbBytes = [int64]$Matches[1] }
    elseif ($line -match '^DISK\s+(\d+)') { $probe.DiskAvail = [int64]$Matches[1] }
    elseif ($line -match '^TRACE\s+(\d+)') { $probe.TraceCount = [int64]$Matches[1] }
    elseif ($line -match '^TRACELAST\s+(.*)$') { $probe.TraceLast = $Matches[1].Trim() }
  }
  if ($probe.AvailableMB -lt 0) { return $null }
  return $probe
}

function Read-NotifyEnv {
  $candidates = @(
    (Join-Path $env:LOCALAPPDATA 'ygobot\notify.env'),
    (Join-Path $DataDir 'notify.env'),
    'D:\pythons\qq-replay-bot\data\secrets\notify.env'
  )
  foreach ($path in $candidates) {
    if (Test-Path $path) {
      $cfg = @{}
      foreach ($line in Get-Content $path -Encoding UTF8) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $k, $v = $line.Split('=', 2)
        $cfg[$k.Trim()] = $v.Trim()
      }
      if ($cfg['SMTP_USER'] -and $cfg['SMTP_PASS']) { return $cfg }
    }
  }
  return $null
}

function Send-AlertMail([string]$Subject, [string]$Body) {
  $cfg = Read-NotifyEnv
  if (-not $cfg) { Write-Log 'mail skipped: notify.env missing'; return $false }
  $hostName = if ($cfg['SMTP_HOST']) { $cfg['SMTP_HOST'] } else { 'smtp.qq.com' }
  $basePort = if ($cfg['SMTP_PORT']) { [int]$cfg['SMTP_PORT'] } else { 587 }
  $user = $cfg['SMTP_USER']
  $to = if ($cfg['SMTP_TO']) { $cfg['SMTP_TO'] } else { $user }
  # .NET SmtpClient 的 EnableSsl 是 STARTTLS，不支持 465 隐式 SSL；优先 587，再退回配置端口。
  $ports = @(587, $basePort) | Select-Object -Unique
  $lastError = ''
  foreach ($port in $ports) {
    try {
      $client = New-Object System.Net.Mail.SmtpClient($hostName, $port)
      $client.EnableSsl = $true
      $client.Timeout = 15000
      $client.Credentials = New-Object System.Net.NetworkCredential($user, $cfg['SMTP_PASS'])
      $mail = New-Object System.Net.Mail.MailMessage($user, $to, $Subject, $Body)
      $mail.BodyEncoding = [Text.Encoding]::UTF8
      $mail.SubjectEncoding = [Text.Encoding]::UTF8
      $client.Send($mail)
      $mail.Dispose(); $client.Dispose()
      Write-Log ("mail sent via {0}:{1}: {2}" -f $hostName, $port, $Subject)
      return $true
    } catch {
      $lastError = $_.Exception.Message
      $inner = $_.Exception.InnerException
      while ($inner) { $lastError = $lastError + ' <- ' + $inner.Message; $inner = $inner.InnerException }
      Write-Log ("mail failed via {0}:{1}: {2}" -f $hostName, $port, $lastError)
    }
  }
  return $false
}

function Show-AlertPopup([string]$Title, [string]$Text) {
  # 优先 Windows toast；失败退到 VBS 弹窗（独立进程，不阻塞探活）。
  try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    $safeTitle = [Security.SecurityElement]::Escape($Title)
    $safeText = [Security.SecurityElement]::Escape($Text)
    $xmlText = "<toast><visual><binding template=`"ToastText02`"><text id=`"1`">$safeTitle</text><text id=`"2`">$safeText</text></binding></visual></toast>"
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($xmlText)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('YgoHealthWatch').Show($toast)
    return
  } catch {
    Write-Log ("toast failed: {0}" -f $_.Exception.Message)
  }
  try {
    # VBScript 字符串不能跨行、也不能含双引号：逐行转成字面量，再用 & vbCrLf & 拼接。
    # 2026-11-06 修：原实现把多行正文塞进一个 "..."，报 800A0409 未结束的字符串常量，弹窗从未成功。
    $vbs = Join-Path $env:TEMP ("ygo-alert-{0}.vbs" -f $PID)
    $cleanTitle = ($Title -replace '"', "'")
    $cleanText = ($Text -replace '"', "'")
    if ($cleanText.Length -gt 900) { $cleanText = $cleanText.Substring(0, 900) + '...' }
    $lines = @($cleanText -split "\r?\n")
    while ($lines.Count -gt 1 -and [string]::IsNullOrWhiteSpace($lines[-1])) { $lines = @($lines[0..($lines.Count - 2)]) }
    $expr = (($lines | ForEach-Object { '"' + $_ + '"' }) -join ' & vbCrLf & ')
    Set-Content -Path $vbs -Value ('MsgBox ' + $expr + ', 64, "' + $cleanTitle + '"') -Encoding Default
    Start-Process -FilePath 'wscript.exe' -ArgumentList ("`"$vbs`"") -WindowStyle Minimized
    # 顺手清掉两天前的残留，避免 %TEMP% 里堆积。
    Get-ChildItem -Path $env:TEMP -Filter 'ygo-alert-*.vbs' -ErrorAction SilentlyContinue |
      Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-2) } |
      Remove-Item -Force -ErrorAction SilentlyContinue
  } catch {
    Write-Log ("popup failed: {0}" -f $_.Exception.Message)
  }
}

function Send-Alert([string]$Kind, [string]$Title, [string]$Body, [bool]$Popup = $true) {
  Send-AlertMail $Title $Body | Out-Null
  if ($Popup) { Show-AlertPopup $Title $Body }
  Write-Log ("alert kind={0} popup={1}" -f $Kind, $Popup)
}

function Get-RecentLog([int]$Lines = 8) {
  if (-not (Test-Path $LogPath)) { return '' }
  return (Get-Content $LogPath -Tail $Lines -Encoding UTF8) -join "`r`n"
}

function Format-Bytes([double]$Bytes) {
  if ($Bytes -lt 0) { return '未知' }
  if ($Bytes -ge 1GB) { return ('{0:N1}GB' -f ($Bytes / 1GB)) }
  if ($Bytes -ge 1MB) { return ('{0:N0}MB' -f ($Bytes / 1MB)) }
  return ('{0:N0}KB' -f ($Bytes / 1KB))
}

# ---- 单实例锁 ----
$mutex = New-Object System.Threading.Mutex($false, 'Global\YgoServerHealthWatch')
if (-not $mutex.WaitOne(0)) {
  Write-Log 'skip: previous probe still running'
  exit 0
}

try {
  $now = [DateTimeOffset]::Now.ToUnixTimeSeconds()
  $todayDigest = (Get-Date).Date.AddHours(9)

  if ($TestAlert) {
    $sent = Send-AlertMail '【YGO】探活测试' ("这是本机探活脚本的测试邮件。`r`n时间：" + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + "`r`n新判据：PSI 需 avg300>=40 且响应变慢才报；OOM/重启/Traceback/WAL/磁盘立即报。")
    Show-AlertPopup '【YGO】探活测试' '本机探活脚本已运行，告警通道测试。'
    Write-Log ("test alert finished mail={0}" -f $sent)
    exit 0
  }

  $state = [pscustomobject]@{
    fail = 0; local_fail = 0; last_alert = 0; last_local_alert = 0; last_ok = 0; alerting = $false
    last_probe_at = 0; oom_kill = -1; restart_web = -1; restart_collector = -1; restart_qq = -1
    wal_bytes = -1; wal_at = 0; disk_avail = -1; db_bytes = -1; trace_count = -1
    last_psi_alert = 0; last_oom_alert = 0; last_restart_alert = 0; last_trace_alert = 0
    last_wal_alert = 0; last_disk_alert = 0; last_digest = 0
    mem_available = -1; swap_free = -1; psi10 = -1; psi300 = -1
  }
  if (Test-Path $StatePath) {
    try {
      $loaded = Get-Content $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
      foreach ($name in 'fail', 'local_fail', 'last_alert', 'last_local_alert', 'last_ok', 'last_probe_at', 'oom_kill',
        'restart_web', 'restart_collector', 'restart_qq', 'wal_bytes', 'wal_at', 'disk_avail', 'db_bytes', 'trace_count',
        'last_psi_alert', 'last_oom_alert', 'last_restart_alert', 'last_trace_alert', 'last_wal_alert',
        'last_disk_alert', 'last_digest', 'mem_available', 'swap_free', 'psi10', 'psi300') {
        if ($null -ne $loaded.$name) { $state.$name = [int64]$loaded.$name }
      }
      if ($null -ne $loaded.alerting) { $state.alerting = [bool]$loaded.alerting }
    } catch { Write-Log ("state parse failed: {0}" -f $_.Exception.Message) }
  }

  $control = Get-CurlCode $ControlUrl
  $health = Get-CurlCode $HealthUrl
  $banner = Test-SshBanner

  $localOk = ($control.Code -eq '200')
  $serverOk = ($health.Code -eq '200') -and $banner.Ok
  $healthSeconds = 0.0
  [double]::TryParse([string]$health.Seconds, [ref]$healthSeconds) | Out-Null

  if (-not $localOk) {
    $state.local_fail = $state.local_fail + 1
    if ($state.local_fail -ge 6 -and ($now - $state.last_local_alert) -gt 3600) {
      Send-AlertMail '【YGO】本机网络异常' ("对照站点不可达，暂不能判断服务器状态。`r`n时间：" + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + "`r`n`r`n" + (Get-RecentLog)) | Out-Null
      Show-AlertPopup '【YGO】本机网络异常' '对照站点不可达，已暂停判断服务器状态。'
      $state.last_local_alert = $now
    }
    Write-Log ("local_network control={0} health={1} banner={2}" -f $control.Code, $health.Code, $banner.Detail)
  }
  elseif ($serverOk) {
    if ($state.alerting) {
      Send-AlertMail '【YGO】服务器已恢复' ("探活恢复：HTTP 200，SSH banner 正常。`r`n时间：" + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) | Out-Null
      Show-AlertPopup '【YGO】服务器已恢复' 'HTTP 与 SSH banner 均恢复正常。'
    }
    $state.fail = 0
    $state.local_fail = 0
    $state.last_ok = $now
    $state.alerting = $false

    if (($now - $state.last_probe_at) -ge 600) {
      $probe = Get-RemoteProbe
      $state.last_probe_at = $now
      if ($null -eq $probe) {
        Write-Log 'probe=unavailable'
      } else {
        $state.mem_available = $probe.AvailableMB
        $state.swap_free = $probe.SwapFreeMB
        $state.psi10 = [int64][double]$probe.Psi10
        $state.psi300 = [int64][double]$probe.Psi300
        $state.disk_avail = $probe.DiskAvail
        $state.db_bytes = $probe.DbBytes
        Write-Log ("probe available={0}MB swapfree={1}MB psi10={2} psi300={3} full10={4} oom={5} restarts=({6},{7},{8}) wal={9} disk={10} trace={11}" -f `
            $probe.AvailableMB, $probe.SwapFreeMB, $probe.Psi10, $probe.Psi300, $probe.Full10, $probe.OomKill, `
            $probe.RestartWeb, $probe.RestartCollector, $probe.RestartQq, (Format-Bytes $probe.WalBytes), (Format-Bytes $probe.DiskAvail), $probe.TraceCount)

        # 1) OOM kill：已经掉过东西，必须让人知道。
        if ($probe.OomKill -gt $state.oom_kill -and $state.oom_kill -ge 0 -and ($now - $state.last_oom_alert) -gt $COOLDOWN_OOM) {
          $body = ("内核 OOM 计数 {0} -> {1}。`r`n时间：{2}`r`nMemAvailable：{3}MB`r`n建议：`r`n  ssh 后看 dmesg -T | grep -i oom 最后 5 行，确认被杀进程；`r`n  若为 ygo-web，多为 MemoryHigh 420MiB 触发回收，可先重启 web 再看。`r`n`r`n最近日志：`r`n{4}" -f `
              $state.oom_kill, $probe.OomKill, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $probe.AvailableMB, (Get-RecentLog 6))
          Send-Alert 'oom' '【YGO】发生 OOM kill' $body $true
          $state.last_oom_alert = $now
        }
        $state.oom_kill = $probe.OomKill

        # 2) 服务异常重启：NRestarts 增加说明进程崩过，可能丢录制。
        $restartNames = @()
        if ($state.restart_web -ge 0 -and $probe.RestartWeb -gt $state.restart_web) { $restartNames += ("ygo-web {0}->{1}" -f $state.restart_web, $probe.RestartWeb) }
        if ($state.restart_collector -ge 0 -and $probe.RestartCollector -gt $state.restart_collector) { $restartNames += ("ygo-collector {0}->{1}" -f $state.restart_collector, $probe.RestartCollector) }
        if ($state.restart_qq -ge 0 -and $probe.RestartQq -gt $state.restart_qq) { $restartNames += ("ygo-qq {0}->{1}" -f $state.restart_qq, $probe.RestartQq) }
        if ($restartNames.Count -gt 0 -and ($now - $state.last_restart_alert) -gt $COOLDOWN_RESTART) {
          $body = ("服务异常重启（systemd 已自动拉起，但当时在录的对局可能中断）：`r`n  {0}`r`n时间：{1}`r`n最后异常：{2}`r`n建议：`r`n  journalctl -u <服务> -n 50 看退出原因；`r`n  若为 database is locked，检查 WAL 大小与长查询（见 daily 摘要）。`r`n`r`n最近日志：`r`n{3}" -f `
              ($restartNames -join '；'), (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $probe.TraceLast, (Get-RecentLog 6))
          Send-Alert 'restart' '【YGO】服务异常重启' $body $true
          $state.last_restart_alert = $now
        }
        $state.restart_web = $probe.RestartWeb
        $state.restart_collector = $probe.RestartCollector
        $state.restart_qq = $probe.RestartQq

        # 3) 16 分钟内新增 Traceback：可能还没触发重启，但已经在出错。
        if ($probe.TraceCount -gt $state.trace_count -and $state.trace_count -ge 0 -and ($now - $state.last_trace_alert) -gt $COOLDOWN_TRACE) {
          $body = ("三个服务在最近 16 分钟出现 {0} 处 Traceback（上一轮 {1}）。`r`n最后异常：{2}`r`n时间：{3}`r`n建议：journalctl -u ygo-collector -u ygo-web -u ygo-qq --since '-20 min' 定位。`r`n`r`n最近日志：`r`n{4}" -f `
              $probe.TraceCount, $state.trace_count, $probe.TraceLast, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), (Get-RecentLog 6))
          Send-Alert 'traceback' '【YGO】服务日志出现异常' $body $true
          $state.last_trace_alert = $now
        }
        $state.trace_count = $probe.TraceCount

        # 4) WAL / 磁盘：这是真正会拖垮查询、写满盘的趋势，越线就报。
        if ($probe.WalBytes -ge 0) {
          $rate = ''
          if ($state.wal_bytes -ge 0 -and $state.wal_at -gt 0 -and $now -gt $state.wal_at -and $probe.WalBytes -gt $state.wal_bytes) {
            $perHour = ($probe.WalBytes - $state.wal_bytes) / [double]($now - $state.wal_at) * 3600
            $rate = ("，约 +{0}/h" -f (Format-Bytes $perHour))
            $hoursLeft = if ($perHour -gt 0 -and $probe.DiskAvail -gt 0) { [math]::Floor($probe.DiskAvail / $perHour) } else { -1 }
            if ($hoursLeft -ge 0) { $rate += ("，按此速率约 {0} 小时占满盘" -f $hoursLeft) }
          }
          if ($probe.WalBytes -gt $WAL_ALERT_BYTES -and ($now - $state.last_wal_alert) -gt $COOLDOWN_WAL) {
            $body = ("WAL 当前 {0}{1}（阈值 2GB，正常应 <=128MB，超过说明有长读事务挡住 checkpoint）。`r`n时间：{2}`r`nDB：{3}，盘可用：{4}`r`n建议（低峰执行，避开在录）：`r`n  sqlite3 /opt/ygobot/data/bot.sqlite3 'PRAGMA busy_timeout=30000; PRAGMA wal_checkpoint(TRUNCATE);'`r`n  若返回 busy=1，先 systemctl restart ygo-web 再重试。`r`n`r`n最近日志：`r`n{5}" -f `
                (Format-Bytes $probe.WalBytes), $rate, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), (Format-Bytes $probe.DbBytes), (Format-Bytes $probe.DiskAvail), (Get-RecentLog 6))
            Send-Alert 'wal' '【YGO】WAL 增长需处理' $body $true
            $state.last_wal_alert = $now
          }
          $state.wal_bytes = $probe.WalBytes
          $state.wal_at = $now
        }
        if ($probe.DiskAvail -ge 0 -and $probe.DiskAvail -lt $DISK_ALERT_BYTES -and ($now - $state.last_disk_alert) -gt $COOLDOWN_DISK) {
          $body = ("系统盘可用只剩 {0}（阈值 8GB）。`r`n时间：{1}`r`nWAL：{2}，DB：{3}`r`n建议：先 truncate WAL，再清 /opt/ygobot/data/backup 旧快照；不要删录像或主库。`r`n`r`n最近日志：`r`n{4}" -f `
            (Format-Bytes $probe.DiskAvail), (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), (Format-Bytes $probe.WalBytes), (Format-Bytes $probe.DbBytes), (Get-RecentLog 6))
          Send-Alert 'disk' '【YGO】磁盘空间不足' $body $true
          $state.last_disk_alert = $now
        }

        # 5) 内存压力：只有"有后果"才弹（响应变慢或健康非 200），否则只记日志。
        $slow = ($healthSeconds -gt $HEALTH_SLOW_SECONDS) -or ($health.Code -ne '200')
        if ($probe.Psi300 -ge $PSI_ALERT_AVG300 -and $slow -and ($now - $state.last_psi_alert) -gt $COOLDOWN_PSI) {
          $night = ((Get-Date).Hour -lt 8)
          $body = ("内存 PSI avg300={0}%（阈值 {1}%）且健康检查已变慢：HTTP {2}，耗时 {3}s。`r`n时间：{4}`r`nMemAvailable：{5}MB，swap free：{6}MB，WAL：{7}`r`n建议：先看是否有大统计查询在跑；持续不降再考虑重启 web（会自动恢复）。`r`n`r`n最近日志：`r`n{8}" -f `
            $probe.Psi300, $PSI_ALERT_AVG300, $health.Code, $health.Seconds, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $probe.AvailableMB, $probe.SwapFreeMB, (Format-Bytes $probe.WalBytes), (Get-RecentLog 6))
          Send-Alert 'psi' '【YGO】内存压力已影响响应' $body (-not $night)
          $state.last_psi_alert = $now
        }
      }
    }

    # 6) 每日 09:00 资源摘要：只发邮件，不进弹窗。
    if ((Get-Date) -ge $todayDigest -and $state.last_digest -lt ([DateTimeOffset]$todayDigest).ToUnixTimeSeconds()) {
      $body = @()
      $body += ("时间：{0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
      $body += ("MemAvailable：{0}MB / swap free：{1}MB" -f $state.mem_available, $state.swap_free)
      $body += ("PSI some avg10={0}% avg300={1}%" -f $state.psi10, $state.psi300)
      $body += ("WAL：{0} / DB：{1} / 盘可用：{2}" -f (Format-Bytes $state.wal_bytes), (Format-Bytes $state.db_bytes), (Format-Bytes $state.disk_avail))
      $body += ("NRestarts：web={0} collector={1} qq={2}" -f $state.restart_web, $state.restart_collector, $state.restart_qq)
      $body += ''
      $body += '最近日志：'
      $body += (Get-RecentLog 10)
      Send-AlertMail '【YGO】每日资源摘要' ($body -join "`r`n") | Out-Null
      $state.last_digest = ([DateTimeOffset]$todayDigest).ToUnixTimeSeconds()
      Write-Log 'daily digest sent'
    }

    Write-Log ("ok health={0} {1}s banner=ok" -f $health.Code, $health.Seconds)
  }
  else {
    $state.fail = $state.fail + 1
    if ($state.fail -ge 3 -and ($now - $state.last_alert) -gt 1800) {
      $body = "探活连续失败 {0} 次。`r`n时间：{1}`r`nHTTP {2}（{3}s）`r`nSSH banner：{4}`r`n`r`n建议：控制台看 VNC/监控，卡死则重启实例；服务均 enabled，起来后自动恢复。`r`n`r`n最近日志：`r`n{5}" -f $state.fail, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $health.Code, $health.Seconds, $banner.Detail, (Get-RecentLog 10)
      Send-AlertMail '【YGO】服务器疑似卡死' $body | Out-Null
      Show-AlertPopup '【YGO】服务器疑似卡死' ("HTTP {0} / SSH banner {1}，已连续 {2} 次。建议控制台检查或重启。" -f $health.Code, $banner.Detail, $state.fail)
      $state.last_alert = $now
      $state.alerting = $true
    }
    Write-Log ("fail n={0} health={1} banner={2}" -f $state.fail, $health.Code, $banner.Detail)
  }

  $state | ConvertTo-Json | Set-Content -Path $StatePath -Encoding UTF8
}
finally {
  $mutex.ReleaseMutex()
  $mutex.Dispose()
}
