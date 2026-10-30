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
"%BOT_PYTHON%" -m bot.launcher
set "STARTUP_EXIT=%ERRORLEVEL%"
if not "%STARTUP_EXIT%"=="0" pause
exit /b %STARTUP_EXIT%
