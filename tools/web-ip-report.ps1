# 统计统计网页的访问来源；默认只读本地日志，不把 IP 发到外部。
# 用法：
#   web-ip-report.cmd
#   web-ip-report.cmd -Days 7
#   web-ip-report.cmd -Days 7 -Geo   # 调 ip-api.com 查国家/省/市（会把公网 IP 发给第三方）
param(
  [int]$Days = 0,
  [switch]$Geo,
  [string]$LogPath = '',
  [string]$OutPath = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if (-not $LogPath) { $LogPath = Join-Path $root 'data\logs\web-access.jsonl' }
if (-not $OutPath) { $OutPath = Join-Path $root 'data\logs\web-ip-report.csv' }
if (-not (Test-Path -LiteralPath $LogPath)) {
  Write-Host "访问日志还不存在：$LogPath" -ForegroundColor Yellow
  Write-Host "请先重启统计网页（8788），之后产生的访问才会被记录。" -ForegroundColor Yellow
  exit 1
}

$cutoff = if ($Days -gt 0) { (Get-Date).AddDays(-$Days) } else { $null }
$entries = @{}
$bad = 0
$total = 0

function Test-PagePath([string]$path) {
  if ([string]::IsNullOrWhiteSpace($path)) { return $false }
  if ($path -eq '/' -or $path -match '^/statistics(/|$)') {
    if ($path -match '^/statistics/(api|assets|card-image)(/|$)') { return $false }
    return $true
  }
  if ($path -match '^/s/<redacted>$' -or $path -match '^/stats/<redacted>$') { return $true }
  return $false
}

Get-Content -LiteralPath $LogPath -Encoding UTF8 | ForEach-Object {
  $line = $_.Trim()
  if (-not $line) { return }
  try { $r = $line | ConvertFrom-Json } catch { $bad++; return }
  if (-not (Test-PagePath ([string]$r.path))) { return }
  $when = $null
  try { $when = [datetimeoffset]::Parse([string]$r.at) } catch { return }
  if ($cutoff -and $when.LocalDateTime -lt $cutoff) { return }

  $ip = ([string]$r.ip).Trim()
  if (-not $ip) { $ip = 'unknown' }
  if (-not $entries.ContainsKey($ip)) {
    $entries[$ip] = [pscustomobject]@{
      IP = $ip; Requests = 0; First = $when; Last = $when
      Paths = @{}; Statuses = @{}; CountryHeader = ''
      UA = ([string]$r.ua); GeoCountry = ''; Province = ''; City = ''; ISP = ''
    }
  }
  $entry = $entries[$ip]
  $entry.Requests++
  $total++
  if ($when -lt $entry.First) { $entry.First = $when }
  if ($when -gt $entry.Last) { $entry.Last = $when }
  $path = [string]$r.path
  $entry.Paths[$path] = 1 + [int]($entry.Paths[$path])
  $status = [string]$r.status
  $entry.Statuses[$status] = 1 + [int]($entry.Statuses[$status])
  if (-not $entry.CountryHeader -and $r.country) { $entry.CountryHeader = [string]$r.country }
  if (-not $entry.UA -and $r.ua) { $entry.UA = [string]$r.ua }
}

function Test-PublicIP([string]$ip) {
  $addr = $null
  if (-not [System.Net.IPAddress]::TryParse($ip, [ref]$addr)) { return $false }
  if ($addr.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
    $b = $addr.GetAddressBytes()
    if ($b[0] -eq 10 -or $b[0] -eq 127 -or $b[0] -eq 0) { return $false }
    if ($b[0] -eq 169 -and $b[1] -eq 254) { return $false }
    if ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) { return $false }
    if ($b[0] -eq 192 -and $b[1] -eq 168) { return $false }
    if ($b[0] -eq 100 -and $b[1] -ge 64 -and $b[1] -le 127) { return $false }
    return $true
  }
  if ($addr.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6) {
    return -not ($addr.IsIPv6LinkLocal -or $addr.IsIPv6SiteLocal -or $addr.IsIPv6UniqueLocal)
  }
  return $false
}

$publicIPs = @($entries.Keys | Where-Object { Test-PublicIP $_ })
if ($Geo -and $publicIPs.Count -gt 0) {
  Write-Host "查询 $($publicIPs.Count) 个公网 IP 的省市（ip-api.com，仅本机主动发出）..." -ForegroundColor Cyan
  for ($start = 0; $start -lt $publicIPs.Count; $start += 100) {
    $chunk = @($publicIPs[$start..([Math]::Min($start + 99, $publicIPs.Count - 1))])
    try {
      $body = ConvertTo-Json -InputObject $chunk -Compress
      $result = Invoke-RestMethod -Uri 'http://ip-api.com/batch?fields=status,message,country,regionName,city,isp,query' `
        -Method Post -ContentType 'application/json; charset=utf-8' -Body $body -TimeoutSec 30
      foreach ($place in @($result)) {
        if (-not $place.query -or $place.status -ne 'success') { continue }
        if ($entries.ContainsKey([string]$place.query)) {
          $entry = $entries[[string]$place.query]
          $entry.GeoCountry = [string]$place.country
          $entry.Province = [string]$place.regionName
          $entry.City = [string]$place.city
          $entry.ISP = [string]$place.isp
        }
      }
    } catch {
      Write-Warning "省市查询失败：$($_.Exception.Message)"
      break
    }
    Start-Sleep -Seconds 2
  }
}

$rows = foreach ($entry in $entries.Values) {
  $statusText = (($entry.Statuses.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name):$($_.Value)" }) -join ', ')
  $pathText = (($entry.Paths.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 5 | ForEach-Object { "$($_.Name)($($_.Value))" }) -join '; ')
  $country = if ($entry.GeoCountry) { $entry.GeoCountry } else { $entry.CountryHeader }
  [pscustomobject]@{
    IP = $entry.IP
    页面访问 = $entry.Requests
    首次 = $entry.First.ToString('yyyy-MM-dd HH:mm:ss')
    最后 = $entry.Last.ToString('yyyy-MM-dd HH:mm:ss')
    状态 = $statusText
    国家 = $country
    省份 = $entry.Province
    城市 = $entry.City
    ISP = $entry.ISP
    主要路径 = $pathText
    浏览器 = $entry.UA
  }
}

$rows = @($rows | Sort-Object 页面访问 -Descending)
Write-Host ""
Write-Host "不同 IP：$($entries.Count)（公网 $($publicIPs.Count)，本地/内网 $($entries.Count - $publicIPs.Count)）  页面访问：$total  无法解析行：$bad" -ForegroundColor Green
if ($rows.Count -eq 0) {
  Write-Host "没有页面访问记录。" -ForegroundColor Yellow
  exit 0
}
$rows | Select-Object IP, 页面访问, 首次, 最后, 国家, 省份, 城市, 状态 |
  Format-Table -AutoSize | Out-Host
$rows | Export-Csv -LiteralPath $OutPath -NoTypeInformation -Encoding UTF8
Write-Host "完整明细（含路径/ISP/UA）：$OutPath" -ForegroundColor Green
