@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 평일 08:50 자동 실행을 Windows 작업 스케줄러에 등록합니다.
if not exist ".venv\Scripts\pythonw.exe" call setup.bat
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\autostart.ps1"
echo.
echo 확인: 작업 스케줄러 - 작업 스케줄러 라이브러리 - Korea3minTrader_AutoStart
pause
