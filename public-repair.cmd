@echo off
chcp 65001 >nul
setlocal
powershell.exe -NoLogo -NoProfile -Command "Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoLogo','-NoProfile','-NoExit','-ExecutionPolicy','Bypass','-File','%~dp0tools\public_status.ps1','-Repair'"
echo 已请求管理员权限；UAC 确认后会打开检查/修复窗口。
pause
