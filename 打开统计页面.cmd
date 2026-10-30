@echo off
"%~dp0.venv\Scripts\python.exe" "%~dp0open_statistics.py"
if errorlevel 1 pause
