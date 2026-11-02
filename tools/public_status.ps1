# 检查/修复公网入口（Tailscale Funnel + 8787）
# 普通运行只诊断并打印下一步；管理员运行可加 -Repair 自动重启 Tailscale 并启用 Funnel。
param([switch]$Repair, [string]$Url = '')

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path $PSScriptRoot -Parent
$exe = 'C:\Program Files\Tailscale\tailscale.exe'
if (-not $Url) {
    try {
        $cfg = Get-Content (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $Url = [string]$cfg.share.tailscale_url
    } catch {}
}
if (-not $Url) { $Url = 'https://ink-y9000p.tail97b750.ts.net' }
$Url = $Url.TrimEnd('/')

function Is-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Show-Actions {
    Write-Host ''
    Write-Host '下一步（按顺序）：' -ForegroundColor Yellow
    Write-Host '一键修复（会弹 UAC）：D:\pythons\qq-replay-bot\public-repair.cmd' -ForegroundColor Cyan
    Write-Host '1. 以管理员身份打开 PowerShell，执行：' -ForegroundColor White
    Write-Host '   Restart-Service Tailscale -Force' -ForegroundColor Cyan
    Write-Host '2. 等 10 秒后执行：' -ForegroundColor White
    Write-Host '   & "C:\Program Files\Tailscale\tailscale.exe" status' -ForegroundColor Cyan
    Write-Host '3. 如果显示 logged out / NeedsLogin：' -ForegroundColor White
    Write-Host '   & "C:\Program Files\Tailscale\tailscale.exe" login' -ForegroundColor Cyan
    Write-Host '   浏览器会给出登录地址；登录后回本脚本重查。' -ForegroundColor Gray
    Write-Host '4. 如果 Funnel 未启用：' -ForegroundColor White
    Write-Host '   & "C:\Program Files\Tailscale\tailscale.exe" funnel --bg --yes 8787' -ForegroundColor Cyan
    Write-Host '5. 重新运行本检查：' -ForegroundColor White
    Write-Host '   public-status.cmd' -ForegroundColor Cyan
    Write-Host ''
}

Write-Host '=== 公网入口检查 ===' -ForegroundColor Green
Write-Host "固定地址：$Url"
if (-not (Test-Path -LiteralPath $exe)) {
    Write-Host "找不到 Tailscale：$exe" -ForegroundColor Red
    Show-Actions
    exit 2
}

$svc = Get-Service Tailscale
Write-Host ("Tailscale 服务：" + $(if ($svc) { $svc.Status } else { '不存在' }))
$raw = & $exe status --json 2>$null
$statusCode = $LASTEXITCODE
$backend = 'unknown'; $online = $false
if ($statusCode -eq 0 -and $raw) {
    try {
        $json = ($raw -join "`n") | ConvertFrom-Json
        $backend = [string]$json.BackendState
        $online = [bool]$json.Self.Online
    } catch {}
}
$text = (& $exe status 2>&1 | Out-String)
Write-Host "Tailscale 状态：backend=$backend online=$online"
if ($text -match 'logged out|NeedsLogin|not logged in') {
    Write-Host 'Tailscale 登录态：已丢失' -ForegroundColor Red
} elseif ($backend -eq 'NoState' -or $backend -eq 'Starting') {
    Write-Host 'Tailscale 登录态：服务卡在 NoState/Starting' -ForegroundColor Yellow
}

$funnel = (& $exe funnel status 2>&1 | Out-String)
$funnelOn = ($funnel -match 'Funnel on' -and $funnel -match [regex]::Escape($Url))
Write-Host ("Funnel：" + $(if ($funnelOn) { '已启用' } else { '未启用' }))

$keyFile = Join-Path $root 'data\secrets\tailscale-authkey.txt'
if (Test-Path $keyFile) {
    $metaFile = Join-Path $root 'data\secrets\tailscale-authkey.meta.json'
    try {
        $meta = Get-Content $metaFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $days = ([DateTimeOffset]::FromUnixTimeSeconds([int64]$meta.expires_at) - [DateTimeOffset]::Now).TotalDays
        if ($days -le 0) { Write-Host 'Tailscale auth key：已过期' -ForegroundColor Red }
        elseif ($days -le 14) { Write-Host ("Tailscale auth key：还有约 {0:N0} 天过期" -f $days) -ForegroundColor Yellow }
        else { Write-Host ("Tailscale auth key：约 {0:N0} 天后过期" -f $days) -ForegroundColor Gray }
    } catch {
        Write-Host 'Tailscale auth key：已配置，但缺少到期日记录' -ForegroundColor Yellow
    }
}

$localOk = $false; $publicOk = $false
try { $r = Invoke-WebRequest 'http://127.0.0.1:8787/health' -UseBasicParsing -TimeoutSec 5; $localOk = ($r.StatusCode -eq 200) } catch {}
try { $r = Invoke-WebRequest ($Url + '/health') -UseBasicParsing -TimeoutSec 20; $publicOk = ($r.StatusCode -eq 200) } catch {}
Write-Host ("本地 8787：" + $(if ($localOk) { '正常' } else { '不通' }))
Write-Host ("公网 /health：" + $(if ($publicOk) { '正常' } else { '不通' }))

if ($Repair -and (-not $localOk -or -not $publicOk -or -not $funnelOn -or $backend -ne 'Running' -or -not $online)) {
    if (-not (Is-Admin)) {
        Write-Host '当前不是管理员，无法自动重启 Tailscale 服务。' -ForegroundColor Yellow
        Show-Actions
        exit 2
    }
    if ($backend -ne 'Running' -or -not $online) {
        Write-Host '正在重启 Tailscale 服务……' -ForegroundColor Cyan
        Restart-Service Tailscale -Force
        Start-Sleep -Seconds 10
        $raw = & $exe status --json 2>$null
        try { $json = ($raw -join "`n") | ConvertFrom-Json; $backend = [string]$json.BackendState; $online = [bool]$json.Self.Online } catch {}
        Write-Host "重启后：backend=$backend online=$online"
    }
    if ($backend -ne 'Running' -or -not $online) {
        if (Test-Path (Join-Path $root 'data\secrets\tailscale-authkey.txt')) {
            $key = (Get-Content (Join-Path $root 'data\secrets\tailscale-authkey.txt') -Raw -Encoding UTF8).Trim()
            if ($key -like 'tskey-auth-*') {
                Write-Host '尝试用本机 auth key 自动登录……' -ForegroundColor Cyan
                $env:TS_AUTHKEY = $key
                & $exe up --hostname=ink-y9000p | Out-Null
                Remove-Item Env:\TS_AUTHKEY -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 8
                $raw = & $exe status --json 2>$null
                try { $json = ($raw -join "`n") | ConvertFrom-Json; $backend = [string]$json.BackendState; $online = [bool]$json.Self.Online } catch {}
                Write-Host "auth key 后：backend=$backend online=$online"
            }
        }
    }
    if ($backend -eq 'Running' -and $online) {
        $funnel = (& $exe funnel status 2>&1 | Out-String)
        if ($funnel -notmatch 'Funnel on' -or $funnel -notmatch [regex]::Escape($Url)) {
            Write-Host '正在启用 Funnel……' -ForegroundColor Cyan
            & $exe funnel --bg --yes 8787
        }
        Start-Sleep -Seconds 2
        try { $r = Invoke-WebRequest ($Url + '/health') -UseBasicParsing -TimeoutSec 30; $publicOk = ($r.StatusCode -eq 200) } catch { $publicOk = $false }
        Write-Host ("修复后公网 /health：" + $(if ($publicOk) { '正常' } else { '仍不通' }))
    }
}

if ($localOk -and $publicOk -and $funnelOn) {
    Write-Host ''
    Write-Host "公网正常：$Url" -ForegroundColor Green
    exit 0
}
Write-Host ''
Write-Host '公网未完全就绪。' -ForegroundColor Red
if (-not $localOk) {
    Write-Host '本地 8787 不通：先确认 share 服务已启动（start-all / start-share）。' -ForegroundColor Yellow
}
if (-not $localOk -or -not $publicOk -or -not $funnelOn) { Show-Actions }
exit 1
