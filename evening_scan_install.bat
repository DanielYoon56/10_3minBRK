@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 평일 18:00 매집 확인 스캔(결과 표시 + 텔레그램)을 Windows 작업 스케줄러에 등록합니다.
if not exist ".venv\Scripts\python.exe" call setup.bat
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\evening_scan.ps1"
echo.
echo 확인: 작업 스케줄러 - 작업 스케줄러 라이브러리 - Korea3minTrader_EveningScan
pause
