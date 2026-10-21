@echo off
chcp 65001 > nul
cd /d "%~dp0"
rem 18시 매집 확인 스캔: 결과를 이 창에 표시하고 텔레그램으로 전송
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" call setup.bat
".venv\Scripts\python.exe" "python_app\evening_scan.py" --pause %*
