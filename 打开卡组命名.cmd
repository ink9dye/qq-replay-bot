@echo off
chcp 65001 >nul
"%~dp0.venv\Scripts\python.exe" "%~dp0open_statistics.py" deck-naming
if errorlevel 1 pause
