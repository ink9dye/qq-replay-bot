@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
call "%~dp0find-python.bat"
if errorlevel 1 (
    pause
    exit /b 1
)
title QQ机器人运行窗口（关闭即停止 NapCat/QQ）
echo 本窗口只启动本项目 NapCat 和 QQ 服务；关闭窗口会先关 Job 再退出。
"%BOT_PYTHON%" -m bot.qq_supervisor_service
set "EXIT=%ERRORLEVEL%"
echo.
echo supervisor 已退出，代码 %EXIT%。NapCat/QQ 已随 Job 结束；watch/8788 不受影响。
pause
exit /b %EXIT%
