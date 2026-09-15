"""기록 한 줄을 화면에 어떻게 그릴지 정하는 **순수 계산**.

Tkinter 를 부르지 않습니다. 화면 없이 시험할 수 있어야 하기 때문입니다 —
:mod:`korail_booker.ui` 는 tkinter 를 import 하므로 시험 환경에서 아예 읽히지
않습니다.

규칙은 셋뿐이고 전부 **글의 모양만** 봅니다. 뜻을 짐작해 색을 고르지 않습니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


#: ``[3] ...`` 처럼 회차로 시작하는 줄. 그 앞에서 한 줄 띄웁니다.
#:
#: 여러 묶음을 따로 돌릴 수 있게 되면서 앞에 ``[A]`` 같은 꼬리표가 붙습니다.
#: 꼬리표가 있어도 회차 머리로 알아봐야 덩어리가 계속 끊깁니다.
POLL_ROUND = re.compile(r"(?:\[[A-Z]\] )?\[\d+\]")

#: 시각 자리 ``[00:00:00] `` 의 폭. 이어지는 줄을 여기에 맞춰 밉니다.
STAMP_WIDTH = len("[00:00:00] ")


@dataclass(frozen=True)
class LogEntry:
    """기록 한 덩어리를 그리는 방법.

    :attr:`pieces` 는 ``(글, 태그)`` 짝의 나열입니다. 태그는 화면 쪽이 색으로
    바꿉니다 — 여기서는 이름만 정합니다.
    """

    #: 앞에 빈 줄을 넣을지. 회차가 바뀌는 자리에서만 참입니다.
    blank_before: bool
    pieces: tuple[tuple[str, str], ...]


def format_entry(message: str, *, stamp: str, level: str = "info") -> LogEntry | None:
    """기록 한 덩어리를 조각으로 나눕니다. 빈 메시지면 ``None``.

    * 앞이 공백으로 시작하는 줄은 **곁가지**입니다(``AutoBooker`` 가 그렇게
      씁니다). 시각을 다시 찍지 않고 ``·`` 를 달아 흐리게 들여씁니다 — 같은
      시각이 열 줄씩 반복되면 정작 시각이 안 보입니다.
    * 여러 줄짜리 메시지의 둘째 줄부터는 시각 자리만큼 밀어 맞춥니다. 예전에는
      왼쪽 끝에 붙어 새 기록처럼 보였습니다.
    * 회차로 시작하는 줄 앞에는 빈 줄을 넣어 덩어리로 끊습니다.
    """
    body = message.rstrip("\n")
    if not body.strip():
        return None
    # **두 칸 이상**이어야 곁가지입니다. ``AutoBooker`` 는 네 칸으로 씁니다.
    # 한 칸으로 보면, 값이 빈 f-string 때문에 우연히 공백으로 시작한 평범한
    # 줄까지 시각을 잃고 흐리게 들여써집니다.
    detail = body.startswith("  ")
    lines = [line.strip() for line in body.split("\n")]
    tag = "detail" if detail and level == "info" else level
    pad = " " * STAMP_WIDTH
    pieces: list[tuple[str, str]] = []
    if detail:
        pieces.append((f"{pad}· ", "detail"))
    else:
        pieces.append((f"[{stamp}] ", "stamp"))
    pieces.append((f"{lines[0]}\n", tag))
    for line in lines[1:]:
        pieces.append((f"{pad}  {line}\n", tag))
    return LogEntry(
        blank_before=POLL_ROUND.match(lines[0]) is not None,
        pieces=tuple(pieces),
    )
