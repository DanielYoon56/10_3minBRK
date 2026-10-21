@echo off
chcp 65001 > nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\evening_scan.ps1" -Remove
pause
