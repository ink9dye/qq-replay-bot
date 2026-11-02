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
echo 正在检查统计网页、观战和QQ连接……
"%BOT_PYTHON%" -m bot.launcher
set "STARTUP_EXIT=%ERRORLEVEL%"
echo.
if "%STARTUP_EXIT%"=="0" (
    echo 启动检查完成。已运行的服务会继续运行。
) else (
    echo 启动未全部完成，退出代码 %STARTUP_EXIT%。请查看上方提示。
)
echo 此窗口是运行窗口：关闭它会停止 QQ/NapCat、share 和 8788，只保留 watch。
pause
exit /b %STARTUP_EXIT%
