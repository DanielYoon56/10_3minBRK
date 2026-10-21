@echo off
chcp 65001 > nul
cd /d "%~dp0"
rem 문제 확인용: 콘솔 창에 로그를 보면서 실행
if not exist ".venv\Scripts\python.exe" call setup.bat
".venv\Scripts\python.exe" "python_app\main.py"
pause
