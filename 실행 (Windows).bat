@echo off
rem ============================================================================
rem  코레일 예매 도우미 — Windows 실행기
rem
rem  이 파일을 더블클릭하면 됩니다. 처음 한 번만 준비 과정이 돌고(1~2분),
rem  그 다음부터는 바로 창이 뜹니다.
rem
rem  하는 일은 셋뿐입니다.
rem    1. 파이썬을 찾는다 (없으면 어디서 받는지 알려 주고 멈춘다)
rem    2. 이 폴더 안에 .venv 를 만들고 httpx, cryptography 를 넣는다
rem       — 컴퓨터에 이미 설치된 파이썬 환경은 건드리지 않는다
rem    3. app\main.py 를 실행한다
rem
rem  창이 그냥 닫히면 안 됩니다. 무엇이 잘못됐는지 읽을 수 있어야 하므로
rem  실패하는 모든 갈래가 pause 로 끝납니다.
rem ============================================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VENV=%CD%\.venv"
set "VPY=%VENV%\Scripts\python.exe"

rem --- 이미 준비돼 있으면 곧장 실행 -------------------------------------------
if exist "%VPY%" goto run

rem --- 파이썬 찾기 ------------------------------------------------------------
rem  py 런처를 먼저 봅니다. python.org 설치본이 함께 넣어 주고, 여러 버전이
rem  깔린 컴퓨터에서도 옳은 것을 고릅니다. 없으면 python 을 봅니다 — 단,
rem  Microsoft Store 의 가짜 python(설치 안내만 띄우고 끝나는 것)이 잡히면
rem  버전 확인에서 걸러집니다.
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)

if not defined PY (
  echo.
  echo   파이썬이 없습니다.
  echo.
  echo   https://www.python.org/downloads/windows/ 에서 받아 설치하세요.
  echo   설치 화면에서 두 가지를 꼭 켜야 합니다:
  echo     - Add python.exe to PATH
  echo     - tcl/tk and IDLE          ^(이게 없으면 창이 안 뜹니다^)
  echo.
  echo   설치한 뒤 이 파일을 다시 더블클릭하세요.
  echo.
  pause
  exit /b 1
)

rem --- 처음 한 번: 전용 환경 만들기 -------------------------------------------
echo.
echo   처음 실행이라 준비를 합니다. 1~2분 걸립니다 ^(다음부터는 바로 뜹니다^).
echo.
%PY% -m venv "%VENV%"
if errorlevel 1 (
  echo.
  echo   전용 환경^(.venv^)을 만들지 못했습니다.
  echo   파이썬이 제대로 설치됐는지 확인하세요.
  echo.
  pause
  exit /b 1
)

"%VPY%" -m pip install --upgrade pip >nul 2>&1
"%VPY%" -m pip install httpx cryptography
if errorlevel 1 (
  echo.
  echo   필요한 것을 내려받지 못했습니다. 인터넷 연결을 확인하고 다시 하세요.
  echo   준비가 중간에 멈췄으므로 .venv 폴더를 지우고 다시 시작합니다.
  echo.
  rmdir /s /q "%VENV%" 2>nul
  pause
  exit /b 1
)

rem --- 실행 -------------------------------------------------------------------
:run
"%VPY%" "%CD%\app\main.py"
if errorlevel 1 (
  echo.
  echo   프로그램이 오류로 끝났습니다. 위 내용을 그대로 알려 주세요.
  echo.
  pause
  exit /b 1
)
endlocal
