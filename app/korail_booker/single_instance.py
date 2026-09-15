"""이 프로그램이 이미 돌고 있으면 새 창을 띄우는 대신 그 창을 앞으로
불러옵니다.

exe 를 두 번 실행하면(트레이에 숨겨 둔 채로 잊고 다시 누르는 것을 포함해)
지금까지는 창이 하나 더 늘었습니다 — 감시도 따로, 예약도 따로 돌아
헷갈리기 쉬웠습니다. localhost 전용 TCP 소켓 하나를 "이 컴퓨터에서 이
프로그램은 하나만" 이라는 잠금 겸 신호 통로로 씁니다.

* 첫 실행은 정해 둔 포트를 **잡습니다**(bind) — 그 소켓을 들고 있는 동안은
  이 인스턴스가 "원본" 입니다.
* 이미 원본이 떠 있으면 그 포트는 잡혀 있으므로 bind 가 실패합니다 —
  그러면 **그 포트로 신호만 보내고**(connect) 곧장 끝냅니다. 창을 새로
  열지 않습니다.
* 신호를 받은 원본은(:func:`listen_for_duplicate_launches` 가 돌리는
  스레드에서) 창을 앞으로 불러옵니다.

Tkinter 는 여기서 import 하지 않습니다 — ``tray.py`` 와 같은 이유로,
디스플레이가 없는 시험 환경에서도 이 파일은 그대로 import 되고 시험할 수
있어야 합니다.

포트를 고정해 둔 것은 여러 사본이 서로를 찾을 공통의 이름이 필요해서고,
127.0.0.1 로 묶은 것은 이 컴퓨터 밖에서는 아무도 이 포트에 닿지 못하게
하기 위해서입니다 — 다른 컴퓨터의 뉴레일과는 애초에 엮이지 않습니다.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable


#: 49152~65535(동적/사설 포트 구간)에서 고른 값. 등록된 서비스와 부딪힐
#: 일이 없습니다. 이 값 자체에 특별한 뜻은 없습니다 — 이 프로그램의
#: 사본끼리만 같은 값을 쓰면 됩니다.
DEFAULT_PORT = 51823

#: 신호를 보내는 쪽이 원본의 accept 를 기다리는 한도. 같은 컴퓨터 안의
#: 루프백 연결이라 이 시간이면 넉넉합니다 — 그래도 원본이 먹통이면
#: 무한정 붙잡혀 있지 않도록 반드시 둡니다.
_SIGNAL_TIMEOUT_S = 0.5


def _try_claim_port(port: int) -> socket.socket | None:
    """이 포트를 잡아 봅니다. 성공하면 그 소켓(원본이 됐다는 뜻)을,
    이미 누가 쓰고 있으면 ``None`` 을 돌려줍니다.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # 방금 끝난 사본이 남긴 TIME_WAIT 상태 때문에 곧장 다시 뜬 새
        # 사본이 괜히 "이미 떠 있다" 고 오판하지 않게 합니다.
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(4)
    except OSError:
        server.close()
        return None
    return server


def _signal_existing_instance(port: int) -> bool:
    """이미 떠 있는 원본에게 "창을 앞으로" 신호를 보냅니다. 로컬호스트
    안의 루프백 연결이라, 연결이 됐다는 것 자체가 사실상 충분한 신호입니다.
    """
    try:
        with socket.create_connection(
            ("127.0.0.1", port), timeout=_SIGNAL_TIMEOUT_S
        ) as sock:
            sock.sendall(b"show")
        return True
    except OSError:
        return False


def negotiate(port: int = DEFAULT_PORT) -> tuple[bool, socket.socket | None]:
    """이 프로세스가 창을 열어야 하는지 정합니다.

    돌려주는 첫 값이 ``False`` 면 이미 떠 있는 원본에게 신호를 보냈다는
    뜻입니다 — 이 프로세스는 창을 열지 말고 곧장 끝나야 합니다. ``True``
    면 이 프로세스가 창을 열어야 합니다 — 두 번째 값이 소켓이면 그것을
    :func:`listen_for_duplicate_launches` 에 넘겨, 나중에 뜨는 사본의
    신호를 받게 하세요.

    포트를 잡지도 못하고(다른 원본이 있다는 뜻) 신호도 못 보내면(그
    원본이 막 죽는 도중이거나 하는, 극히 드문 경우) — 창을 아예 안
    띄우는 것보다는 낫다고 보고 잠금 없이 창을 엽니다.
    """
    server = _try_claim_port(port)
    if server is not None:
        return True, server
    if _signal_existing_instance(port):
        return False, None
    return True, None


def listen_for_duplicate_launches(
    server: socket.socket, on_signal: Callable[[], None]
) -> None:
    """``server`` 로 들어오는 신호를 받아 ``on_signal`` 을 부르는 백그라운드
    스레드를 시작합니다.

    **``on_signal`` 은 이 스레드에서 불립니다** — Tkinter 위젯을 직접
    만지면 안 됩니다. 부르는 쪽이 큐에 넣는 식으로 감싸야 합니다
    (``tray.py`` 의 트레이 핸들러와 같은 규칙).
    """

    def accept_loop() -> None:
        while True:
            try:
                conn, _addr = server.accept()
            except OSError:
                return  # 소켓이 닫혔다 — 이 프로그램이 끝나는 중입니다.
            try:
                conn.recv(16)
            except OSError:
                pass
            finally:
                conn.close()
            on_signal()

    threading.Thread(
        target=accept_loop, name="single-instance-listener", daemon=True
    ).start()
