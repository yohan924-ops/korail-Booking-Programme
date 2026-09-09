@echo off
rem ============================================================================
rem  코레일 예매 도우미 — Windows 실행 파일(exe) 만들기
rem
rem  파이썬이 없는 사람에게 프로그램을 주고 싶을 때 씁니다. 다 되면
rem  dist\KorailBooker.exe 하나가 나오고, 그 파일만 있으면 파이썬도 이 폴더도
rem  없는 컴퓨터에서 더블클릭으로 돕니다.
rem
rem  이 파일은 Windows 에서만 뜻이 있습니다 — 실행 파일은 만드는 컴퓨터의
rem  운영체제 것으로 나오고, 다른 운영체제 것을 대신 만들어 주지 못합니다.
rem
rem  평소에 쓸 때는 이것이 아니라 `실행 (Windows).bat` 을 누르면 됩니다.
rem ============================================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "VENV=%CD%\.venv"
set "VPY=%VENV%\Scripts\python.exe"

if not exist "%VPY%" (
  echo.
  echo   먼저 `실행 (Windows).bat` 을 한 번 눌러 주세요.
  echo   그것이 만드는 전용 환경^(.venv^)을 여기서 그대로 씁니다.
  echo.
  pause
  exit /b 1
)

echo.
echo   PyInstaller 를 준비합니다.
echo.
"%VPY%" -m pip install pyinstaller
if errorlevel 1 (
  echo.
  echo   PyInstaller 를 받지 못했습니다. 인터넷 연결을 확인하세요.
  echo.
  pause
  exit /b 1
)

echo.
echo   실행 파일을 만듭니다. 1~3분 걸립니다.
echo.
rem  --onefile   파일 하나로 나옵니다. 첫 실행 때 임시 폴더에 푸느라 몇 초
rem              멈춥니다 -- 고장이 아닙니다.
rem  --windowed  검은 콘솔 창을 띄우지 않습니다.
rem  --paths     desktop_entry.py 가 sys.path 를 실행 중에 만지지 않는 이유.
rem              PyInstaller 는 소스를 정적으로 훑으므로 여기서 알려 줍니다.
"%VPY%" -m PyInstaller --onefile --windowed ^
  --name KorailBooker ^
  --paths src --paths app ^
  --distpath "%CD%\dist" --workpath "%CD%\build" --specpath "%CD%\build" ^
  "%CD%\packaging\desktop_entry.py"
if errorlevel 1 (
  echo.
  echo   만들지 못했습니다. 위에 찍힌 내용을 그대로 알려 주세요.
  echo.
  pause
  exit /b 1
)

echo.
echo   다 됐습니다:  %CD%\dist\KorailBooker.exe
echo.
echo   이 파일 하나만 복사해 주면 됩니다. 다만 서명이 없어서 처음 실행할 때
echo   "Windows의 PC 보호" 경고가 뜰 수 있습니다 — [추가 정보] 를 누르면
echo   [실행] 이 나옵니다. 백신이 잡는 경우도 있습니다^(PyInstaller 로 만든
echo   파일에 흔한 오탐입니다^).
echo.
pause
endlocal
