@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo M3 collector（单写者）：只监听 127.0.0.1:8790，需 data\m3-token。
echo 当前 m3.enabled=false 时写请求返回 503；不会影响现有 watch/QQ/8788。
"%PY%" -m bot.collector_service
pause
