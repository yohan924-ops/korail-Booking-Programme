"""잡아 둔 예약 하나와, 결제 기한까지 남은 시간.

Tkinter 를 부르지 않습니다 — 화면 없이 시험할 수 있어야 하기 때문입니다.

**기한은 서버가 준 값만 씁니다.** 서버가 주지 않으면 "모름" 이라고 적지,
10분 같은 숫자를 지어내지 않습니다. 그 값이 틀리면 사람이 표를 잃습니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


#: 기한이 이만큼 남으면 급한 것으로 봅니다. 화면이 색을 바꾸는 기준입니다.
URGENT_SECONDS = 3 * 60


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
    if len(clock) not in (4, 6) or not clock.isdigit():
        return None
    if len(clock) == 4:
        clock += "00"
    try:
        return datetime.strptime(date + clock, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def remaining_seconds(deadline: datetime | None, now: datetime) -> int | None:
    if deadline is None:
        return None
    return int((deadline - now).total_seconds())


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

    :class:`~korail_mobile_api.ReservationHoldResponse` 를 그대로 들고 있지
    않습니다 — 화면이 쓰는 것만 문자열로 뽑아 둡니다. 그래야 이 조각을
    화면 없이 시험할 수 있습니다.
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
