@echo off
chcp 65001 > nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" call setup.bat
if not exist ".venv\Scripts\pythonw.exe" exit /b 1
start "" ".venv\Scripts\pythonw.exe" "python_app\main.py"
