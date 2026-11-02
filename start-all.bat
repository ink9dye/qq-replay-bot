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
if "%STARTUP_EXIT%"=="0" (
    if exist "%~dp0tools\ensure_public.py" (
        echo.
        echo 正在检查公网入口……
        "%BOT_PYTHON%" "%~dp0tools\ensure_public.py"
        if errorlevel 1 (
            echo 公网入口未就绪，正在打开检查窗口……
            start "公网检查" cmd /k "%~dp0public-status.cmd"
        )
    )
)
echo.
if "%STARTUP_EXIT%"=="0" (
    echo 启动检查完成。已运行的服务会继续运行。
) else (
    echo 启动未全部完成，退出代码 %STARTUP_EXIT%。正在打开连接检查窗口……
    start "启动检查" cmd /k "%~dp0check-status.bat"
)
echo 此窗口是运行窗口：关闭它会停止 QQ/NapCat、share 和 8788，只保留 watch。
pause
exit /b %STARTUP_EXIT%
