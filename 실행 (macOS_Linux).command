#!/bin/sh
# =============================================================================
#  뉴레일 (코레일의 새로운 예매 도우미) — macOS / Linux 실행기
#
#  macOS 는 이 파일을 더블클릭하면 됩니다(처음 한 번은 오른쪽 클릭 → 열기).
#  Linux 는 파일 속성에서 "실행 가능"을 켜고 더블클릭하거나, 터미널에서
#  ./"실행 (macOS_Linux).command" 로 실행합니다.
#
#  Windows 실행기와 같은 일을 합니다 — 파이썬을 찾고, 이 폴더 안에 .venv 를
#  만들고, app/main.py 를 띄웁니다. 시스템 파이썬은 건드리지 않습니다.
# =============================================================================
set -eu
cd "$(dirname "$0")"

VENV=".venv"
VPY="$VENV/bin/python"

fail() {
    printf '\n  %s\n\n' "$1"
    printf '  창을 닫으려면 Enter 를 누르세요. '
    read -r _ || true
    exit 1
}

if [ ! -x "$VPY" ]; then
    PY=""
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    done
    [ -n "$PY" ] || fail "파이썬이 없습니다. macOS 는 https://www.python.org/downloads/ 에서 받으세요."

    printf '\n  처음 실행이라 준비를 합니다. 1~2분 걸립니다 (다음부터는 바로 뜹니다).\n\n'
    "$PY" -m venv "$VENV" || fail "전용 환경(.venv)을 만들지 못했습니다."
    "$VPY" -m pip install --upgrade pip >/dev/null 2>&1 || true
    if ! "$VPY" -m pip install httpx cryptography; then
        # 준비가 중간에 멈춘 .venv 는 다음 실행에서 "이미 있다" 로 오해됩니다.
        rm -rf "$VENV"
        fail "필요한 것을 내려받지 못했습니다. 인터넷 연결을 확인하고 다시 하세요."
    fi
fi

"$VPY" app/main.py || fail "프로그램이 오류로 끝났습니다. 위 내용을 그대로 알려 주세요."
