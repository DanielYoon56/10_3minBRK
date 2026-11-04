@echo off
chcp 65001 > nul
cd /d "%~dp0"
rem 20시 매집 확인 스캔: 결과는 텔레그램으로 전송, 끝나면 창 자동 종료 (창 유지: evening_scan.bat --pause)
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" call setup.bat
".venv\Scripts\python.exe" "python_app\evening_scan.py" %*
