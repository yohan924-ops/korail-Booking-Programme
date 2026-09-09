"""GUI 런처. 저장소에서 바로 실행합니다::

    python3 app/main.py

import 만으로는 아무 일도 하지 않습니다 — 창은 :func:`main` 에서만 열립니다.
"""

from __future__ import annotations

import sys
from pathlib import Path


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
        if "tkinter" in str(exc):
            print(
                "Tkinter 가 없습니다. 리눅스라면 python3-tk 를 설치하세요"
                "(예: sudo apt install python3-tk).",
                file=sys.stderr,
            )
            return 2
        raise
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
