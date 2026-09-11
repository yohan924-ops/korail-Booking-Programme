@echo off
rem ============================================================================
rem  뉴레일 (코레일의 새로운 예매 도우미) — Windows 실행 파일(exe) 만들기
rem
rem  파이썬이 없는 사람에게 프로그램을 주고 싶을 때 씁니다. 다 되면
rem  dist\NewRail.exe 하나가 나오고, 그 파일만 있으면 파이썬도 이 폴더도
rem  없는 컴퓨터에서 더블클릭으로 돕니다.
rem
rem  이 파일은 Windows 에서만 뜻이 있습니다 — 실행 파일은 만드는 컴퓨터의
rem  운영체제 것으로 나오고, 다른 운영체제 것을 대신 만들어 주지 못합니다.
rem
rem  평소에 쓸 때는 이것이 아니라 `실행 (Windows).bat` 을 누르면 됩니다.
rem
rem  --utf8 로 저 자신을 한 번 다시 부르는 것은 장식이 아닙니다. cmd 는 이
rem  파일을 미리 한 뭉치 읽어 두는데, chcp 를 이 줄에서 걸어도 그 뭉치 안의
rem  한글 섞인 줄들은 **이전 코드 페이지로 이미 읽힌 채로 남습니다.** 그래서
rem  화면에 "'습니다' 는 내부 또는 외부 명령이 아닙니다" 같은 게 뜬 적이
rem  있습니다 — 그 줄의 앞부분이 코드 페이지가 안 맞아 다른 낱말로 읽힌
rem  것입니다. 새 cmd 를 하나 더 열어 이 파일을 처음부터 다시 읽게 하면,
rem  그때는 코드 페이지가 이미 65001 이라 처음부터 제대로 읽힙니다.
rem
rem  괄호 블록이 아니라 goto 로 짭니다. 괄호 안에서 cmd 의 종료 코드를 바로
rem  읽으면, delayed expansion 없이는 그 값이 그 줄이 도는 시점이 아니라
rem  괄호 블록이 펼쳐지는 시점의 것으로 굳어 진짜 종료 코드를 놓칩니다.
if /I "%~1"=="--utf8" goto reinvoked
chcp 65001 >nul
cmd /d /c ""%~f0" --utf8"
exit /b %errorlevel%

:reinvoked
shift
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
rem  절대 경로로 넣습니다. PyInstaller 는 --specpath 를 CWD 와 다르게 두면
rem  (이 스크립트는 build\ 로 둡니다) 상대 경로 아이콘을 CWD 가 아니라
rem  workpath(build\) 기준으로 다시 찾아, "build\packaging\icon.png 없음" 으로
rem  실패합니다 — 실제로 그랬습니다.
set "ICON="
if exist "packaging\icon.ico" set ICON=--icon "%CD%\packaging\icon.ico"
if not defined ICON if exist "packaging\icon.png" set ICON=--icon "%CD%\packaging\icon.png"
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
  --name NewRail ^
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
echo   다 됐습니다:  %CD%\dist\NewRail.exe
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
