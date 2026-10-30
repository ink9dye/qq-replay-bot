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
"%BOT_PYTHON%" "%~dp0watch.py" %*
set "WATCH_EXIT=%ERRORLEVEL%"
if not "%WATCH_EXIT%"=="0" pause
exit /b %WATCH_EXIT%
