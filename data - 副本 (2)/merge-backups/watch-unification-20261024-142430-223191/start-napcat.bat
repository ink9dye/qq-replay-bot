@echo off
setlocal
chcp 65001 >nul
set "NAPCAT_DIR=%~dp0NapCat.Shell.Windows.OneKey\NapCat.52230.Shell"
if not exist "%NAPCAT_DIR%\NapCatWinBootMain.exe" (
    echo Cannot find NapCatWinBootMain.exe in:
    echo "%NAPCAT_DIR%"
    pause
    exit /b 2
)
pushd "%NAPCAT_DIR%"
if errorlevel 1 exit /b 2
if /i "%~1"=="--check-path" (
    echo NapCat launcher and working directory OK.
    popd
    exit /b 0
)
call "%NAPCAT_DIR%\napcat.quick.bat"
set "NAPCAT_EXIT=%ERRORLEVEL%"
popd
exit /b %NAPCAT_EXIT%
