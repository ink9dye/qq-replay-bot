@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
call "%~dp0find-python.bat"
if errorlevel 1 (
    pause
    exit /b 1
)
"%BOT_PYTHON%" -m bot.setup
echo.
pause
