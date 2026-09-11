"""PyInstaller 가 얼려 넣는 진입점.

``app/main.py`` 와 나뉘어 있는 이유가 있습니다. 그쪽은 **저장소 체크아웃에서
바로 실행**하기 위한 것이라 ``sys.path`` 를 실행 중에 손봅니다. PyInstaller 는
소스를 정적으로 훑어 무엇을 담을지 정하므로, 실행 중에 붙인 경로는 보지
못합니다 — 그래서 얼릴 때는 평범한 최상위 import 하나만 두고, 경로는 빌드
명령의 ``--paths`` 로 넘깁니다(``.github/workflows/desktop-build.yml``).

여기서 아무것도 하지 않습니다. 창은 :func:`run` 이 엽니다.
"""

from __future__ import annotations

import sys

from korail_booker.ui import run


if __name__ == "__main__":
    sys.exit(run())
