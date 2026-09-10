"""잡아 둔 예약 하나와, 결제 기한까지 남은 시간.

Tkinter 를 부르지 않습니다 — 화면 없이 시험할 수 있어야 하기 때문입니다.

**기한은 서버가 준 값만 씁니다.** 서버가 주지 않으면 "모름" 이라고 적지,
10분 같은 숫자를 지어내지 않습니다. 그 값이 틀리면 사람이 표를 잃습니다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from korail_mobile_api import ReservationHoldResponse


#: 기한이 이만큼 남으면 급한 것으로 봅니다. 화면이 색을 바꾸는 기준입니다.
URGENT_SECONDS = 3 * 60

#: 서버가 주는 기한은 **한국 시각**입니다. 컴퓨터의 시계가 다른 시간대면
#: 그대로 빼는 순간 카운트다운이 몇 시간씩 틀립니다 — 여행 중이거나 시간대를
#: 바꿔 둔 노트북에서 실제로 그렇습니다. 한국은 서머타임이 없으므로 고정
#: 오프셋이면 충분하고, ``zoneinfo`` 를 들이지 않아도 됩니다(윈도우에서는
#: ``tzdata`` 가 따로 필요합니다).
KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """지금을 한국 시각으로. 서버가 준 기한과 같은 자로 재기 위한 것입니다."""
    return datetime.now(KST).replace(tzinfo=None)


def parse_deadline(date_text: str | None, time_text: str | None) -> datetime | None:
    """``h_ntisu_lmt_dt`` 와 ``h_ntisu_lmt_tm`` 을 시각 하나로.

    앱이 하는 것과 같습니다 — 둘을 이어 ``yyyyMMddHHmmss`` 로 읽습니다
    (``S4/C0816p.java:64-70``). 모양이 어긋나면 ``None`` 입니다. 반쯤 읽어
    엉뚱한 시각을 만드는 것보다 모른다고 하는 편이 낫습니다.
    """
    date = (date_text or "").strip()
    clock = (time_text or "").strip()
    if len(date) != 8 or not date.isdigit():
        return None
    # 서버는 이 값을 JSON 숫자로도 보냅니다 — 09:30:00 이 93000 으로 옵니다.
    # 앞의 0 을 되살리지 않으면 오전 열 시 이전의 기한을 통째로 "모름" 으로
    # 버리게 됩니다. 그런데 그 시간대가 밤새 잡은 예약의 기한입니다.
    #
    # 네 자리는 초가 빠진 ``HHMM`` 으로 읽습니다(예전부터 그랬고 시험이 그것을
    # 못박아 두었습니다). 다섯 자리는 앞이 떨어진 여섯 자리입니다.
    #
    # **이 읽기가 이 프로그램의 유일한 기한 해석입니다.** 화면에 찍는 글도
    # 이 함수를 거칩니다(``autobook.payment_deadline_text``). 예전에는 그쪽이
    # 따로 읽어서, ``"0016"`` 하나가 한쪽에서는 00:16 이고 다른 쪽에서는
    # 16:00 이었습니다 — 두 칸이 12시간 다른 기한을 말한 셈입니다.
    if not clock.isdigit() or len(clock) not in (4, 5, 6):
        return None
    if len(clock) == 4:
        clock += "00"
    clock = clock.zfill(6)
    try:
        return datetime.strptime(date + clock, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def remaining_seconds(deadline: datetime | None, now: datetime) -> int | None:
    if deadline is None:
        return None
    # 올림입니다. int() 는 0 쪽으로 자르므로 0.4초가 0 이 되어, 기한이 아직
    # 남았는데도 "기한 지남" 으로 회색이 되는 순간이 생깁니다.
    return math.ceil((deadline - now).total_seconds())


def remaining_text(deadline: datetime | None, now: datetime) -> str:
    """남은 시간을 사람이 읽는 말로. 모르면 모른다고 적습니다."""
    left = remaining_seconds(deadline, now)
    if left is None:
        return "기한 모름"
    if left <= 0:
        return "기한 지남"
    hours, rest = divmod(left, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}시간 {minutes}분 남음"
    if minutes:
        return f"{minutes}분 {seconds}초 남음"
    return f"{seconds}초 남음"


def is_urgent(deadline: datetime | None, now: datetime) -> bool:
    left = remaining_seconds(deadline, now)
    return left is not None and 0 < left <= URGENT_SECONDS


def is_expired(deadline: datetime | None, now: datetime) -> bool:
    left = remaining_seconds(deadline, now)
    return left is not None and left <= 0


@dataclass(frozen=True)
class Held:
    """잡아 둔 예약 하나. 화면의 '잡은 예약' 목록에 한 줄로 들어갑니다.

    표에 적는 값은 :class:`~korail_mobile_api.ReservationHoldResponse` 에서
    뽑은 문자열입니다 — 화면 없이 시험하는 것은 그 문자열들만 봅니다.
    ``hold_response`` 는 취소 기능을 위해 원본을 함께 들고 다니는
    자리이고, 표시·시험 어느 쪽도 그것을 열어 보지 않습니다.
    """

    #: 가는 편/오는 편, 또는 편도면 빈 문자열.
    label: str
    #: 여정 한 줄(``Journey.summary()``).
    summary: str
    pnr: str
    fare: str
    #: 서버가 준 결제 기한. 주지 않았으면 ``None``.
    deadline: datetime | None
    #: 기한을 사람이 읽는 모양으로. 서버가 문장으로만 준 경우도 여기 담깁니다.
    deadline_text: str
    #: 좌석 예약인지 예약대기인지.
    kind: str = "좌석 예약"
    #: 이 예약이 어느 방향의 것인지 — ``(출발, 도착, 날짜)``.
    #:
    #: 화면이 "이 방향은 이미 잡았다" 를 판단하는 데 씁니다. 예전에는 여정 한
    #: 줄(:attr:`summary`)을 예매 대상 목록과 맞춰 봤는데, 그 줄을 빼는 순간
    #: 맞출 것이 사라져 같은 구간에 두 번째 예약이 나갔습니다. 서버가 준 값이
    #: 아니라 이 프로그램이 아는 값이므로 지어내는 것이 아닙니다.
    direction: tuple[str, str, str] = ("", "", "")
    #: 서버 응답 그대로. 취소하려면 **이 정확한 객체**가 있어야 합니다 —
    #: 라이브러리의 취소 폼이 ``type(response) is ReservationHoldResponse`` 를
    #: 그대로 요구합니다(다시 만든 값이나 하위클래스는 거절합니다). 화면 없이
    #: 시험하는 데는 안 쓰이므로 ``None`` 이어도 이 조각의 나머지는 그대로
    #: 돌아갑니다 — 취소 버튼만 못 씁니다.
    hold_response: ReservationHoldResponse | None = None

    def row(self, now: datetime) -> tuple[str, ...]:
        """표 한 줄. 남은 시간은 부를 때마다 다시 셉니다."""
        return (
            self.label or "편도",
            self.kind,
            self.summary,
            self.pnr,
            self.fare,
            self.deadline_text,
            remaining_text(self.deadline, now),
        )

    def tag(self, now: datetime) -> str:
        if is_expired(self.deadline, now):
            return "expired"
        if is_urgent(self.deadline, now):
            return "urgent"
        return "held"
