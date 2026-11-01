@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo 半自动切换分享模式 inline -^> process。会先请你在 PyCharm 点 Stop（本工具不 taskkill）。
echo 不加 --yes 只做预检；失败会自动恢复 inline 并重新请求 watch。
"%PY%" "%~dp0tools\switch_share_mode.py" %*
pause
