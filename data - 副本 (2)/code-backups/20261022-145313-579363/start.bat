@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
    echo 没有找到 python。请先安装 Python 3.9 或更高版本，并加入 PATH。
    pause
    exit /b 1
)
if not exist "config.json" (
    echo 还没有 config.json。
    echo 请把 config.example.json 复制为 config.json，然后填写：
    echo   mycard.account 和 mycard.password
    echo   qq.access_token
    echo   qq.admins 或 qq.users 里的 QQ 号
    echo 给好友打开录像时，还要填写 share.public_base_url。
    echo 不要把 http://127.0.0.1:8787 当作公网地址。
    pause
    exit /b 1
)
python -m bot.main
echo.
echo 程序已退出，代码 %ERRORLEVEL%。
pause
