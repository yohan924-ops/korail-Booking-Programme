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

rem --- 전용 환경이 없으면 여기서 만듭니다 -------------------------------------
rem  `실행 (Windows).bat` 을 먼저 누르라고 시키지 않습니다. 친구에게 줄 파일
rem  하나를 만들려고 온 사람에게 "다른 것부터 누르세요" 는 단계 하나가 더
rem  느는 것일 뿐입니다.
rem  괄호로 묶지 않고 goto 로 건너뜁니다. cmd 는 괄호 블록을 통째로 한 번에
rem  해석하면서 %PY% 를 그 자리에서 펼치므로, 블록 안에서 방금 set 한 값을
rem  같은 블록에서 읽으면 빈 값이 나옵니다.
if exist "%VPY%" goto haveenv

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY goto nopython

echo.
echo   전용 환경을 만듭니다. 1~2분 걸립니다.
echo.
%PY% -m venv "%VENV%" || goto setupfailed
"%VPY%" -m pip install --upgrade pip >nul 2>&1
"%VPY%" -m pip install httpx cryptography || goto setupfailed

:haveenv

echo.
echo   PyInstaller 를 준비합니다.
echo.
rem  pillow 는 아이콘 때문입니다. PyInstaller 는 .ico 가 아닌 그림을 받으면
rem  pillow 로 바꿔 넣습니다. 아이콘을 안 쓰면 그냥 안 쓰이고 맙니다.
"%VPY%" -m pip install pyinstaller pillow
if errorlevel 1 (
  echo.
  echo   PyInstaller 를 받지 못했습니다. 인터넷 연결을 확인하세요.
  echo.
  pause
  exit /b 1
)

rem --- 아이콘 (있으면 씁니다) --------------------------------------------------
rem  packaging\icon.png 또는 icon.ico 를 넣어 두면 그것이 exe 아이콘이 됩니다.
rem  없으면 PyInstaller 기본 아이콘으로 나옵니다 — 없다고 빌드가 멈추지는
rem  않습니다.
set "ICON="
if exist "packaging\icon.ico" set "ICON=--icon packaging\icon.ico"
if not defined ICON if exist "packaging\icon.png" set "ICON=--icon packaging\icon.png"
if defined ICON (
  echo   아이콘을 넣습니다.
) else (
  echo   packaging\icon.png 이 없어 기본 아이콘으로 만듭니다.
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
  %ICON% ^
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
exit /b 0

:nopython
echo.
echo   파이썬이 없습니다.
echo.
echo   https://www.python.org/downloads/windows/ 에서 받아 설치하세요.
echo   설치 화면에서 두 가지를 꼭 켜야 합니다:
echo     - Add python.exe to PATH
echo     - tcl/tk and IDLE          ^(이게 없으면 창이 안 뜹니다^)
echo.
pause
exit /b 1

:setupfailed
echo.
echo   준비에 실패했습니다. 인터넷 연결을 확인하고 다시 하세요.
echo   반쯤 만들어진 환경은 지웁니다.
echo.
rmdir /s /q "%VENV%" 2>nul
pause
exit /b 1
