@echo off
set "BOT_PYTHON="
call :try_python "%~dp0.venv\Scripts\python.exe"
if defined BOT_PYTHON exit /b 0
rem Explorer double-clicks do not inherit an activated Anaconda environment.
rem SQLite and SSL depend on DLLs in Library\bin.
set "PATH=D:\Anaconda3;D:\Anaconda3\Library\bin;D:\Anaconda3\Scripts;%PATH%"
call :try_python "D:\Anaconda3\python.exe"
if defined BOT_PYTHON exit /b 0
echo No working Python 3.9+ found. Expected D:\Anaconda3\python.exe or .venv\Scripts\python.exe.
exit /b 1

:try_python
if defined BOT_PYTHON exit /b 0
if not exist "%~1" exit /b 0
"%~1" -c "import sys,asyncio,sqlite3; raise SystemExit(0 if sys.version_info >= (3,9) else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
set "BOT_PYTHON=%~1"
exit /b 0
