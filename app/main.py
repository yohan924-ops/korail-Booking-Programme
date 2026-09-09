"""GUI 런처. 저장소에서 바로 실행합니다::

    python3 app/main.py

import 만으로는 아무 일도 하지 않습니다 — 창은 :func:`main` 에서만 열립니다.
"""

from __future__ import annotations

import sys
from pathlib import Path


#: 라이브러리가 요구하는 것과 같습니다(``pyproject.toml`` 의 ``dependencies``).
#: 이름이 어긋나면 아래 안내가 틀린 명령을 알려 주게 되므로 시험이 대조합니다.
DEPENDENCIES = ("httpx", "cryptography")


def main() -> int:
    # 저장소 체크아웃에서 그대로 돌 수 있게 경로를 붙입니다. 설치하지 않고
    # 쓰는 프로그램이라 이 두 줄이 유일한 배선입니다.
    root = Path(__file__).resolve().parent
    for path in (str(root), str(root.parent / "src")):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        from korail_booker.ui import run
    except ImportError as exc:  # pragma: no cover - 환경 문제를 사람 말로 알린다
        # 빠진 것이 무엇인지는 스택트레이스가 아니라 한 줄로 말해 줍니다.
        # 여기서 걸리는 사람은 대개 설치 단계를 건너뛴 것뿐입니다.
        missing = (getattr(exc, "name", "") or str(exc)).split(".")[0]
        if missing == "tkinter":
            print(
                "Tkinter 가 없습니다.\n"
                "  Windows/macOS: python.org 설치본을 쓰세요"
                "(Windows 설치 시 'tcl/tk and IDLE' 체크).\n"
                "  Linux: sudo apt install python3-tk",
                file=sys.stderr,
            )
            return 2
        if missing in DEPENDENCIES:
            print(
                f"{missing} 가 설치돼 있지 않습니다. 먼저 이것부터 실행하세요:\n"
                f"  Windows:  py -m pip install {' '.join(DEPENDENCIES)}\n"
                f"  macOS/Linux:  python3 -m pip install {' '.join(DEPENDENCIES)}",
                file=sys.stderr,
            )
            return 2
        raise
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
