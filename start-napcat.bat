@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0find-python.bat"
if errorlevel 1 exit /b 1
"%BOT_PYTHON%" -m bot.napcat_start %*
exit /b %ERRORLEVEL%
