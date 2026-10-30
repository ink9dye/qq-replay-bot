@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
where python >nul 2>&1
if errorlevel 1 (
    echo 没有找到 python。请先安装 Python 3.9 或更高版本，并加入 PATH。
    pause
    exit /b 1
)
if not exist "config.json" (
    echo 还没有 config.json。
    echo 请先双击 setup.bat，按提示完成本机配置。
    pause
    exit /b 1
)
python -m bot.main
echo.
echo 程序已退出，代码 %ERRORLEVEL%。
pause
