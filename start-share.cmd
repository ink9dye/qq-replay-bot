@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo 独立分享服务（只读）：/s/ 与 /stats/ 由本窗口提供，关闭本窗口即停。
echo 仅当 config.json 的 share.mode=process 时启动；inline 模式仍由 watch 窗口内联提供。
"%PY%" -m bot.share_service
pause
