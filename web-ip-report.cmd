@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m bot.web_ip_dashboard --root "%~dp0" %*
if errorlevel 1 (
  echo.
  echo 访问看板未能启动。请确认 8790 端口未被占用，或查看上面的错误。
  pause
)
