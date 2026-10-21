@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ===================================================
echo  Korea3MinBreakoutTrader - 최초 설치 (1회)
echo ===================================================
echo.

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [오류] Python이 없습니다. https://www.python.org 에서 3.11 이상 설치
    echo        설치 화면에서 "Add python.exe to PATH" 를 반드시 체크하세요.
    pause
    exit /b 1
)
%PY% --version

echo.
echo [1/2] 가상환경(.venv) 만드는 중...
if not exist ".venv\Scripts\python.exe" (
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [오류] 가상환경 생성 실패
        pause
        exit /b 1
    )
)

echo [2/2] 필요한 패키지 설치 중... (1~3분)
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r "python_app\requirements.txt"
if errorlevel 1 (
    echo [오류] 패키지 설치 실패 - 인터넷 연결을 확인하세요
    pause
    exit /b 1
)

echo.
echo 설치 완료! 이제 run.bat 으로 실행하세요.
echo.
pause
