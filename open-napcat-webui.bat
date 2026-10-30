@echo off
setlocal
set "NAPCAT_WEBUI_CONFIG=%~dp0NapCat.Shell.Windows.OneKey\NapCat.52230.Shell\versions\9.9.33-52230\resources\app\napcat\config\webui.json"
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; try { $cfg=Get-Content -Raw -Encoding UTF8 -LiteralPath $env:NAPCAT_WEBUI_CONFIG | ConvertFrom-Json; $url='http://127.0.0.1:6099/webui?token='+[Uri]::EscapeDataString([string]$cfg.token); Start-Process $url } catch { Write-Host 'Cannot open NapCat WebUI. Start NapCat first and check its local configuration.'; exit 1 }"
if errorlevel 1 pause
