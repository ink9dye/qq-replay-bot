@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
call "%~dp0find-python.bat"
if errorlevel 1 (
    echo 没有找到可用的 Python，请检查 find-python.bat 中的路径。
    pause
    exit /b 1
)
if /i "%~1"=="--check-python" (
    "%BOT_PYTHON%" -c "import sys,sqlite3,bot.main; db=sqlite3.connect(':memory:'); assert db.execute('select 1').fetchone()==(1,); db.close(); print('Python, SQLite and bot imports OK:', sys.executable)"
    exit /b
)
if not exist "config.json" (
    echo 还没有 config.json。
    echo 请先双击 setup.bat，按提示完成本机配置。
    pause
    exit /b 1
)
"%BOT_PYTHON%" -m bot.pycharm_link
echo.
echo 程序已退出，代码 %ERRORLEVEL%。
pause
