"""윈도우 작업 표시줄 트레이 아이콘 — 창을 닫아도 자동예매가 백그라운드에서
계속되게 합니다.

Tkinter 는 여기서 import 하지 않습니다 — ``ui.py`` 와 같은 이유로, 시험
환경에 tkinter 가 없어도 이 파일은 그대로 import 되고 시험할 수 있어야
합니다.

**윈도우 전용입니다.** 트레이(시스템 알림 영역) 통합은 운영체제마다 다른
백엔드를 쓰는데, 이 파일이 쓰는 ``pystray`` 라이브러리 자신의 소스를 읽어
세 백엔드의 신뢰도가 서로 크게 다르다는 것을 실측으로 확인했습니다.

* **win32**: 진짜 스레드 하나를 새로 만들어 그 안에서 윈도우 메시지 루프를
  돕니다(``pystray/_win32.py`` 의 ``Icon._run_detached`` — 그냥
  ``threading.Thread(target=lambda: self._run()).start()`` 입니다). 메인
  스레드가 필요 없습니다 — Tkinter 의 메인 루프와 부딪히지 않습니다.
* **xorg (Linux)**: 마찬가지로 스레드를 새로 만들지만, 이 컴퓨터에 시스템
  트레이를 실제로 보여 주는 프로그램(트레이 매니저)이 없으면 **예외 하나
  없이 조용히 실패**합니다 — 직접 Xvfb 로 띄워서 확인했습니다: 아이콘이
  어디에도 안 보이는데 ``icon.visible`` 은 그래도 ``True`` 를 돌려줍니다.
  창을 숨겼는데 트레이가 안 보이면 되찾을 길이 없어지므로, 이 기능은
  **윈도우가 아니면 켜지 않습니다.**
* **darwin (macOS)**: ``run_detached()`` 문서 자신이 말하길, macOS 에서는
  ``NSApplication`` 인스턴스를 직접 넘겨 그 쪽 메인 루프와 엮어야 합니다.
  이 프로그램은 순수 Tkinter 라 그 엮음이 없고, 엮지 않으면
  ``Icon._run_detached`` 가 그냥 준비만 하고 아무 것도 돌리지 않습니다
  (``pystray/_darwin.py``) — 마찬가지로 켜지 않습니다.

그래서 :func:`create_tray_icon` 은 ``sys.platform`` 이 ``"win32"`` 가
아니면 ``pystray`` 를 아예 import 하지 않고 곧장 ``None`` 을 돌려주고,
그 값을 받는 ``ui.py`` 는 그 경우 지금까지처럼 창을 닫으면 곧장 끝냅니다
(:meth:`~korail_booker.ui.BookerApp.on_close`).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class _PystrayModule(Protocol):
    """``_build_icon`` 이 실제로 쓰는 ``pystray`` 의 부분만 적은 것.

    시험에서 진짜 ``pystray`` (윈도우가 아니면 없을 수 있습니다) 대신
    이 모양을 흉내 낸 가짜 모듈을 넣어, 메뉴 구성 로직 자체는 이 프로그램이
    실제로 도는 컴퓨터(윈도우)가 아니어도 확인할 수 있게 합니다.
    """

    Icon: Any
    Menu: Any
    MenuItem: Any


@dataclass(frozen=True)
class TrayHandlers:
    """트레이 메뉴가 부를 동작들.

    **전부 pystray 자신의 스레드(위 docstring 의 win32 스레드)에서
    불립니다** — 그래서 여기 담긴 함수는 Tkinter 위젯을 직접 만지면 안
    됩니다. 부르는 쪽(``ui.py``)이 큐(``self.events``)에 넣는 식으로 감싸
    Tk 메인 스레드에서 실제 동작이 돌게 해야 합니다. 상태 글
    (``status_text``/``holds_text``)과 켜짐 여부(``is_running``/
    ``is_logged_in``)도 마찬가지로, 목록을 그 자리에서 훑지 않고 Tk
    스레드가 미리 계산해 둔 문자열/불 값 하나만 읽도록 감싸야 합니다 —
    ``self.watches``/``self.holds`` 같은 목록을 다른 스레드에서 직접
    훑는 것은 이 프로그램의 나머지 부분과 다른, 새로운 종류의 스레드
    안전성 문제라 만들지 않습니다.
    """

    open_window: Callable[[], None]
    quit_app: Callable[[], None]
    stop_all: Callable[[], None]
    refresh_reservations: Callable[[], None]
    status_text: Callable[[], str]
    holds_text: Callable[[], str]
    is_running: Callable[[], bool]
    is_logged_in: Callable[[], bool]


def _load_icon_image() -> Any:
    """트레이 아이콘 그림. ``packaging/`` 의 것이 있으면 그것을, 없으면
    간단한 대체 그림을 그려 씁니다 — 아이콘 파일이 없다고 트레이 기능
    전체를 포기할 이유가 없습니다(``exe 만들기 (Windows).bat`` 이 exe
    아이콘을 만들 때 쓰는 규칙과 같습니다).
    """
    from PIL import Image, ImageDraw

    root = Path(__file__).resolve().parent.parent.parent
    for name in ("packaging/icon.png", "packaging/icon.ico"):
        path = root / name
        if path.exists():
            try:
                return Image.open(path)
            except Exception:
                continue
    # 대체 그림 — 파란 바탕에 흰 원. 이 프로그램 나머지 화면이 쓰는
    # 파란색(#1f6feb, 예: 예매 대상의 감시 중 표시)과 맞췄습니다.
    image = Image.new("RGBA", (64, 64), (31, 111, 235, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((14, 14, 50, 50), fill=(255, 255, 255, 255))
    return image


def _build_icon(pystray_module: _PystrayModule, handlers: TrayHandlers) -> Any:
    """``pystray.Icon`` 하나. **아직 돌리지는 않습니다** —
    ``icon.run_detached()`` 는 부르는 쪽(``ui.py``)이 합니다.

    ``pystray_module`` 을 인자로 받는 것은 시험을 위해서입니다 — 진짜
    ``pystray`` 없이도(또는 윈도우가 아닌 컴퓨터에서도) 메뉴 구성 자체
    (어떤 항목이 몇 개, 어느 동작에 연결됐는지, 상태 글이 진짜 함수를
    부르는지)는 확인할 수 있게 합니다.
    """
    image = _load_icon_image()
    # 상태 두 줄은 눌러도 아무 일도 없는 안내 줄입니다 — 켜고 끌 수 없으니
    # enabled=False 로 그려지게(회색으로) 둡니다.
    menu = pystray_module.Menu(
        pystray_module.MenuItem(
            "뉴레일 열기", handlers.open_window, default=True,
        ),
        pystray_module.Menu.SEPARATOR,
        pystray_module.MenuItem(
            lambda _item: handlers.status_text(), None, enabled=False,
        ),
        pystray_module.MenuItem(
            lambda _item: handlers.holds_text(), None, enabled=False,
        ),
        pystray_module.Menu.SEPARATOR,
        pystray_module.MenuItem(
            "전체 중지", handlers.stop_all,
            enabled=lambda _item: handlers.is_running(),
        ),
        pystray_module.MenuItem(
            "서버에서 예약 다시 불러오기", handlers.refresh_reservations,
            enabled=lambda _item: handlers.is_logged_in(),
        ),
        pystray_module.Menu.SEPARATOR,
        pystray_module.MenuItem("종료", handlers.quit_app),
    )
    return pystray_module.Icon("newrail", image, "뉴레일", menu)


def create_tray_icon(handlers: TrayHandlers) -> Any:
    """트레이 아이콘을 만듭니다. 아직 돌리지는 않습니다 —
    ``icon.run_detached()`` 는 부르는 쪽이 합니다.

    윈도우가 아니거나, ``pystray`` 가 없거나(선택 의존성이라 설치 안
    했을 수 있습니다), 무엇이 됐든 만드는 도중 문제가 생기면 ``None`` 을
    돌려줍니다 — **이 함수는 절대 예외를 던지지 않습니다.** 트레이가
    없어도 프로그램은 지금까지처럼(창을 닫으면 곧장 끝나는) 그대로
    돌아야 하기 때문입니다.
    """
    if sys.platform != "win32":
        return None
    try:
        import pystray
    except Exception:
        return None
    try:
        return _build_icon(pystray, handlers)
    except Exception:
        return None
