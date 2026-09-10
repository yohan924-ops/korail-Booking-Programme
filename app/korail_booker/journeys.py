"""여정 하나를 화면에 보여 줄 수 있는 모양으로 바꾸는 순수 계산.

여기에는 I/O 가 없습니다. 검색 결과 행(:class:`TrainSummary`)을 받아 소요
시간·환승 시간·좌석 상태를 셈하는 것이 전부라, 네트워크 없이 시험됩니다.

시각 다루기가 이 파일의 절반입니다. KORAIL 은 ``h_dpt_tm`` 을 JSON 숫자로도
보내서 ``"063000"`` 이 ``"63000"`` 으로 도착하고(``models._train_scalar`` 의
주석), 도착이 출발보다 이르면 자정을 넘긴 열차입니다. 둘 다 여기서 처리하지
않으면 새벽 열차가 시간창 밖으로 밀려나거나 소요 시간이 음수가 됩니다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from korail_mobile_api import KorailProtocolError, KorailSeatClass, TrainSummary
from korail_mobile_api.constants import KORAIL_STANDBY_WAIT_FLAG as STANDBY_WAIT_FLAG

# 예약 폼이 요구하는 열여섯 필드의 모양을 정하는 곳. 밑줄로 시작하지만 이
# 저장소 안의 프로그램이라 규칙을 베끼는 대신 그대로 부릅니다.
from korail_mobile_api.mutation_payloads import _journey_fields


#: "이 객실에 예매 가능한 자리가 있다"는 유일한 값. 라이브러리의 예약 폼도 같은
#: 값만 받아들입니다(``mutation_payloads._assert_leg_is_bookable``).
AVAILABLE_SEAT_CODE = "11"
#: 매진. 라이브러리 주석이 이 값을 그렇게 읽습니다 — 예약대기가 열리는 열차는
#: 대개 ``"13"`` 이고(``_assert_leg_is_bookable``), 입석 판정도 일반실이
#: ``"13"`` 일 때를 매진으로 봅니다(``_standing_flag``).
SOLD_OUT_SEAT_CODE = "13"

MINUTES_PER_DAY = 24 * 60

#: 여정 하나를 다시 알아보는 열쇠의 모양 — 구간마다
#: ``(열차번호, 출발일, 출발시각, 출발역, 도착역)``.
JourneyKey = tuple[tuple[str, str, str, str, str], ...]


class SeatPreference(Enum):
    """사용자가 고른 객실. ``ANY`` 는 "구별 없이"입니다."""

    GENERAL = "general"
    SPECIAL = "special"
    ANY = "any"

    @property
    def label(self) -> str:
        return {"general": "일반실", "special": "특실", "any": "무관"}[self.value]

    def seat_classes(self) -> tuple[KorailSeatClass, ...]:
        """시도할 등급을 우선순위대로. ``ANY`` 는 일반실을 먼저 봅니다."""
        if self is SeatPreference.GENERAL:
            return (KorailSeatClass.GENERAL,)
        if self is SeatPreference.SPECIAL:
            return (KorailSeatClass.SPECIAL,)
        return (KorailSeatClass.GENERAL, KorailSeatClass.SPECIAL)


class JourneySource(Enum):
    """이 여정이 어디서 왔는지. 화면의 경고 문구가 여기서 갈립니다."""

    DIRECT = "direct"
    #: ``search_transfer_trains`` 가 짝지어 준 여정. 라이브 검증된 예약 경로.
    SERVER_TRANSFER = "server_transfer"
    #: 사용자가 환승역을 지정해 이 프로그램이 직접 붙인 조합. 예약 폼은
    #: 만들어지지만 **서버가 받아들이는지는 확인된 바 없습니다.**
    CUSTOM_TRANSFER = "custom_transfer"


def normalize_clock(value: str | None) -> str:
    """``HHMMSS`` 여섯 자리로. 서버가 떨어뜨린 앞의 0 을 되살립니다."""
    raw = (value or "").strip()
    if not raw or not raw.isdigit():
        return ""
    return raw.zfill(6)


def clock_to_minutes(value: str | None) -> int | None:
    """``"063000"`` → ``390``. 시각이 아니면 ``None``."""
    clock = normalize_clock(value)
    if len(clock) != 6:
        return None
    hours, minutes = int(clock[:2]), int(clock[2:4])
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def elapsed_minutes(start: str | None, end: str | None) -> int | None:
    """두 시각 사이의 분. 끝이 시작보다 이르면 자정을 넘긴 것으로 봅니다.

    날짜를 함께 보지 않는 것은 검색 행이 **출발일만** 주기 때문입니다
    (``h_dpt_dt``). 도착일은 응답에 없으므로, 앱이 화면에 그러듯 하루를
    넘기는 것까지만 셈합니다.
    """
    first = clock_to_minutes(start)
    second = clock_to_minutes(end)
    if first is None or second is None:
        return None
    delta = second - first
    return delta if delta >= 0 else delta + MINUTES_PER_DAY


def format_clock(value: str | None) -> str:
    clock = normalize_clock(value)
    return f"{clock[:2]}:{clock[2:4]}" if len(clock) == 6 else "--:--"


def format_duration(minutes: int | None) -> str:
    if minutes is None:
        return "-"
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours}시간 {rest}분"
    if hours:
        return f"{hours}시간"
    return f"{rest}분"


#: 라이브러리의 거절 문구에서 어느 필드가 문제인지 뽑습니다. 그 이름은
#: :class:`TrainSummary` 의 속성 이름과 같습니다(``_journey_fields`` 가 그렇게
#: 씁니다). 문구가 바뀌어 못 뽑으면 그냥 원문을 보여 줍니다.
_FIELD_IN_MESSAGE = re.compile(r"train field ([a-z_]+) ")


#: 환승 대기가 이보다 짧으면 화면이 빨갛게 경고합니다(분).
#:
#: **코레일의 최소 환승 허용 시간이 몇 분인지는 확인하지 못했습니다.** 서버는
#: 너무 촉박한 조합의 예약을 ``ERR911193 환승최소허용시간 미달`` 로 거절합니다
#: — 실제로 받은 응답입니다. 그런데 그 기준값을 알려 주는 필드도, 이 저장소가
#: 확인한 문서도 없습니다. 그래서 10 은 **코레일의 기준이 아니라 화면이 눈에
#: 띄게 해 주는 선**입니다. 이 선을 넘겼다고 예약이 된다는 뜻이 아니고,
#: 밑돌았다고 반드시 거절된다는 뜻도 아닙니다.
TIGHT_TRANSFER_MINUTES = 10


def is_tight_transfer(journey: Journey) -> bool:
    """환승 대기가 :data:`TIGHT_TRANSFER_MINUTES` 미만인가.

    직통이거나 대기 시간을 계산할 수 없으면 거짓입니다 — 모르는 것을 경고로
    바꾸지 않습니다.
    """
    minutes = journey.transfer_minutes
    return minutes is not None and minutes < TIGHT_TRANSFER_MINUTES


def books_as_one_reservation(journey: Journey) -> bool:
    """이 여정을 **한 건(PNR 하나)** 으로 살 수 있는가.

    직통은 당연히 한 건입니다. 서버 추천 환승도 한 건입니다 — 코레일이 짝지어
    준 조합이고, ``reserve_transfer`` 가 두 구간을 한 요청으로 보냅니다.

    **직접 조합은 아닙니다.** 서버가 검증한 조합이 아니라, 환승 예약으로
    보내면 서버가 ``ERR911193 환승최소허용시간 미달`` 로 거절하는 일이
    있습니다(실제로 받았습니다). 그런 조합은 구간마다 따로 삽니다 — 각 구간은
    그냥 직통 열차 한 편이라 보통의 예약 길이고, 서버가 환승 조합으로 심사할
    일이 없습니다.

    **따로 사면 예약도 따로입니다** — PNR 이 둘, 결제도 둘이고, 한쪽만 잡히고
    다른 쪽을 놓치는 일이 생길 수 있습니다. 그 위험은 화면이 말해 줍니다.
    """
    return not journey.is_transfer or journey.source is not JourneySource.CUSTOM_TRANSFER


def first_leg_key(journey: Journey) -> tuple[str, str, str]:
    """1구간을 알아보는 값 — 열차 번호와 출발·도착 시각.

    직접 조합 환승은 같은 1구간에 2구간만 다른 조합이 여럿 나옵니다. 그것을
    묶으려면 "같은 1구간" 을 정의해야 합니다.
    """
    train = journey.first
    return (
        (train.train_no or "").strip(),
        normalize_clock(train.departure_time),
        normalize_clock(train.arrival_time),
    )


def group_by_first_leg(
    journeys: Sequence[Journey],
) -> list[tuple[Journey, list[int]]]:
    """1구간이 같은 여정끼리 묶습니다. **차례는 그대로** 둡니다.

    돌려주는 것은 ``(그 묶음의 첫 여정, 원래 번호들)`` 의 나열입니다. 화면은
    번호로 원래 목록을 되짚으므로, 묶으면서 번호를 잃으면 안 됩니다.

    직통과 서버 추천 환승은 묶지 않습니다 — 직통은 1구간이 곧 여정이고,
    서버 추천은 코레일이 이미 골라 준 조합이라 수가 적습니다. 묶어서 접을
    값어치가 있는 것은 **경우의 수가 곱으로 늘어나는 직접 조합**뿐입니다.
    """
    order: list[tuple[str, str, str]] = []
    buckets: dict[tuple[str, str, str], list[int]] = {}
    for index, journey in enumerate(journeys):
        if journey.source is not JourneySource.CUSTOM_TRANSFER:
            # 묶지 않는 것은 저마다 혼자인 묶음이 됩니다 — 부르는 쪽이 갈래를
            # 나누지 않아도 되게.
            key = ("", str(index), "")
        else:
            key = first_leg_key(journey)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(index)
    return [(journeys[buckets[key][0]], buckets[key]) for key in order]


def unbookable_reason(journey: Journey) -> str | None:
    """예약 폼을 **만들 수 있는지** 미리 봅니다. 못 만들면 그 이유.

    좌석이 있느냐와는 다른 이야기입니다. 서버가 검색 행에 예약 폼이 요구하는
    값을 채워 주지 않으면 자리가 열려도 폼 자체가 만들어지지 않습니다.

    **왜 그런 행이 오는지는 확인된 바가 없습니다.** 여기서 알 수 있는 것은
    "이 행으로는 폼을 만들 수 없다" 뿐이고, 그 이상을 이 함수가 지어내지
    않습니다. 무엇이 어떻게 왔는지는 :func:`unbookable_detail` 이 보여
    줍니다.

    규칙을 여기서 다시 쓰지 않고 라이브러리의 검사를 그대로 부릅니다 — 열여섯
    필드의 모양을 두 곳에서 관리하면 반드시 어긋납니다.
    """
    for train in journey.legs:
        try:
            _journey_fields(train)
        except KorailProtocolError as exc:
            return str(exc)
        except Exception:
            return None
    return None


def unbookable_detail(journey: Journey) -> str | None:
    """폼을 못 만드는 이유를 **서버가 보낸 값과 함께** 적습니다.

    원인을 추측해서 적어 두면 그 추측이 그대로 사실처럼 읽힙니다. 대신 어느
    구간의 어느 필드가 무슨 값으로 왔는지를 그대로 보여 줍니다 — 그것이
    지어내지 않고 말할 수 있는 전부이고, 원인을 찾으려면 어차피 그 값이
    필요합니다.
    """
    for train in journey.legs:
        try:
            _journey_fields(train)
        except KorailProtocolError as exc:
            message = str(exc)
            found = _FIELD_IN_MESSAGE.search(message)
            if found is None:
                return message
            name = found.group(1)
            train_text = one_line(
                f"{train.train_class_name or '?'} {train.train_no or '?'}"
            )
            return (
                f"{train_text} 구간의 '{name}' 값이 {getattr(train, name, None)!r} "
                f"로 왔습니다 — 예약 폼은 이 자리에 숫자를 요구합니다.\n"
                f"({message})"
            )
        except Exception:
            return None
    return None


@dataclass(frozen=True)
class SeatState:
    """한 여정의 한 객실 등급이 지금 어떤 상태인지."""

    available: bool
    #: 앱이 화면에 찍는 문구를 이은 것(``"매진"``, ``"여유"``…). 구간마다
    #: 다르면 ``" · "`` 로 잇습니다. 그 문구는 표시용이고, 예약 여부를
    #: 정하는 것은 :attr:`available` 입니다.
    label: str
    #: 이 등급이 이 여정에 아예 없을 때(특실 없는 열차 등) 참.
    absent: bool = False
    #: 모든 구간이 매진 코드일 때 참. 자동예매가 노리는 것이 이 상태입니다.
    sold_out: bool = False

    @property
    def status(self) -> str:
        """한 낱말로 줄인 상태. 표의 맨 앞에 이것이 옵니다."""
        if self.available:
            return "예약가능"
        if self.sold_out:
            return "매진"
        if self.absent:
            return "-"
        return "불가"


def _reservation_code(train: TrainSummary, seat_class: KorailSeatClass) -> str | None:
    if seat_class is KorailSeatClass.SPECIAL:
        return train.special_reservation_code
    return train.general_reservation_code


def one_line(text: str) -> str:
    """줄바꿈과 잇단 공백을 공백 하나로. 표의 한 줄에 들어가게 만듭니다.

    서버의 좌석 문구는 한 줄이 아닙니다 — 운임 아래에 적립 안내가 줄바꿈으로
    붙어 옵니다(``"37,200원\n5%적립 …"``). 표의 행은 한 줄 높이라 둘째 줄이
    잘려서, 화면에는 글자가 반쯤 잘린 것처럼 보입니다.
    """
    return " ".join(text.split())


def _availability_name(train: TrainSummary, seat_class: KorailSeatClass) -> str:
    if seat_class is KorailSeatClass.SPECIAL:
        return one_line(train.special_availability_name or "")
    return one_line(train.general_availability_name or "")


@dataclass(frozen=True)
class Journey:
    """예약 단위 하나 — 직통이면 열차 한 편, 환승이면 두 구간.

    ``legs`` 는 탑승 순서대로이며 그대로
    :meth:`~korail_mobile_api.client.KorailClient.reserve_transfer` 에 넘길 수
    있습니다.
    """

    legs: tuple[TrainSummary, ...]
    source: JourneySource

    def __post_init__(self) -> None:
        if not self.legs:
            raise ValueError("a journey needs at least one leg")

    @property
    def is_transfer(self) -> bool:
        return len(self.legs) > 1

    @property
    def first(self) -> TrainSummary:
        return self.legs[0]

    @property
    def last(self) -> TrainSummary:
        return self.legs[-1]

    @property
    def departure_clock(self) -> str:
        return normalize_clock(self.first.departure_time)

    @property
    def arrival_clock(self) -> str:
        return normalize_clock(self.last.arrival_time)

    @property
    def departure_date(self) -> str:
        return (self.first.departure_date or "").strip()

    @property
    def total_minutes(self) -> int | None:
        """첫 구간 출발부터 마지막 구간 도착까지. 환승 대기가 포함됩니다."""
        return elapsed_minutes(self.first.departure_time, self.last.arrival_time)

    def crosses_midnight(self, index: int) -> bool:
        """이 구간이 자정을 넘겨 도착하는가.

        검색 행은 **출발일만** 줍니다(``h_dpt_dt``). 도착이 출발보다 이르면
        하루를 넘긴 것이라고 볼 수밖에 없고, 앱이 화면에 그러는 것과 같습니다.
        """
        train = self.legs[index]
        start = clock_to_minutes(train.departure_time)
        end = clock_to_minutes(train.arrival_time)
        return start is not None and end is not None and end < start

    @property
    def transfer_minutes(self) -> int | None:
        """환승역에서 기다리는 시간. 직통이면 ``None``."""
        if not self.is_transfer:
            return None
        return elapsed_minutes(self.legs[0].arrival_time, self.legs[1].departure_time)

    @property
    def transfer_station_name(self) -> str | None:
        """환승역 이름. 내리는 역과 타는 역이 다르면 ``None`` — 실제로 그런
        여정이 옵니다(``models.TransferItinerary`` 참조)."""
        if not self.is_transfer:
            return None
        arrival = (self.legs[0].arrival_station_name or "").strip()
        departure = (self.legs[1].departure_station_name or "").strip()
        return arrival if arrival and arrival == departure else None

    def leg_minutes(self, index: int) -> int | None:
        train = self.legs[index]
        return elapsed_minutes(train.departure_time, train.arrival_time)

    def train_numbers(self) -> tuple[str, ...]:
        return tuple((train.train_no or "").strip() for train in self.legs)

    def train_names(self) -> tuple[str, ...]:
        return tuple((train.train_class_name or "").strip() for train in self.legs)

    def seat_state(self, seat_class: KorailSeatClass) -> SeatState:
        """이 등급으로 지금 예약할 수 있는지. **모든 구간**이 열려 있어야 합니다."""
        codes = [_reservation_code(train, seat_class) for train in self.legs]
        names = [_availability_name(train, seat_class) for train in self.legs]
        absent = all(code is None or not code.strip() for code in codes)
        available = all(code == AVAILABLE_SEAT_CODE for code in codes)
        # 한 구간이라도 매진이면 그 여정은 못 탑니다.
        sold_out = any(code == SOLD_OUT_SEAT_CODE for code in codes)
        shown = [name for name in names if name]
        label = " · ".join(dict.fromkeys(shown))
        return SeatState(
            available=available, label=label, absent=absent, sold_out=sold_out
        )

    def remaining_seats(self, seat_class: KorailSeatClass) -> int | None:
        """남은 좌석 수. 환승이면 **가장 적은 구간**의 수입니다.

        ``h_std_rest_seat_cnt``/``h_fst_rest_seat_cnt`` 를 그대로 읽습니다.
        서버가 이 값을 늘 보내 주지는 않으므로 ``None`` 이 흔합니다 — 없는 것을
        0 으로 바꾸지 않습니다(0 은 "자리가 없다"는 뜻이라 뜻이 달라집니다).
        """
        counts: list[int] = []
        for train in self.legs:
            raw = (
                train.first_class_remaining_seat_count
                if seat_class is KorailSeatClass.SPECIAL
                else train.standard_remaining_seat_count
            )
            text = (raw or "").strip()
            if not text.isdigit():
                return None
            counts.append(int(text))
        return min(counts) if counts else None

    def extras(self) -> tuple[str, ...]:
        """좌석 말고 달리 탈 수 있는 길. 모든 구간에 열려 있을 때만 셉니다.

        코드가 ``"11"`` 이면 열린 것으로 봅니다 — 객실 예약 코드와 같은 규칙이고
        (``_standing_flag`` 가 입석을 그렇게 읽습니다), 예약대기만 플래그가
        따로입니다(``h_wait_rsv_flg``).
        """
        tokens: list[str] = []
        if all(
            train.standing_reservation_code == AVAILABLE_SEAT_CODE
            for train in self.legs
        ):
            tokens.append("입석")
        if all(
            train.free_reservation_code == AVAILABLE_SEAT_CODE for train in self.legs
        ):
            tokens.append("자유석")
        # 예약대기는 직통에만 있습니다(환승은 라이브러리가 거절합니다).
        if not self.is_transfer and self.first.wait_reservation_flag == STANDBY_WAIT_FLAG:
            tokens.append("예약대기")
        return tuple(tokens)

    def seat_text(self, seat_class: KorailSeatClass) -> str:
        """표의 한 칸. **상태를 맨 앞에** 두고 서버 문구와 잔여석을 붙입니다.

        서버 문구는 운임과 적립 안내라서, 그것만으로는 매진인지 아닌지가 한눈에
        보이지 않습니다. 자동예매가 노리는 것이 매진이므로 그 한 낱말이 맨 앞에
        와야 합니다.
        """
        state = self.seat_state(seat_class)
        parts = [state.status]
        if state.label and state.label != state.status:
            parts.append(state.label)
        remaining = self.remaining_seats(seat_class)
        if remaining is not None:
            parts.append(f"{remaining}석")
        return " · ".join(parts)

    def bookable_seat_class(
        self,
        preference: SeatPreference,
    ) -> KorailSeatClass | None:
        """지금 잡을 수 있는 등급 하나. 없으면 ``None``.

        ``ANY`` 는 일반실을 먼저 봅니다. 환승이라도 **두 구간을 같은 등급**으로
        만 잡습니다 — 라이브러리는 구간별로 다른 등급을 받지만, 그렇게 섞은
        예약이 서버에서 확인된 적이 없습니다.
        """
        for seat_class in preference.seat_classes():
            if self.seat_state(seat_class).available:
                return seat_class
        return None

    def key(self) -> JourneyKey:
        """다시 조회했을 때 같은 여정인지 알아보는 값.

        열차번호만으로는 부족합니다 — 같은 번호가 날짜와 구간을 달리해 옵니다.
        **날짜가 들어 있어야 합니다.** 없으면 같은 열차를 오늘과 내일 두 줄로
        담았을 때 둘이 같은 열쇠가 되어, 둘째 감시가 "이미 보고 있다" 로 조용히
        시작되지 않습니다. 예약 폼을 못 만들어 뺀 열차도 그 번호의 다른 날짜까지
        함께 빠집니다.
        """
        return tuple(
            (
                (train.train_no or "").strip(),
                (train.departure_date or "").strip(),
                normalize_clock(train.departure_time),
                (train.departure_station_code or train.departure_station_name or ""),
                (train.arrival_station_code or train.arrival_station_name or ""),
            )
            for train in self.legs
        )

    def train_label(self) -> str:
        """열차를 사람이 알아보는 이름으로. ``KTX-산천 387 + ITX-새마을 1111``.

        번호만 적으면(``00387+01111``) 무슨 조합인지 알 수 없습니다 — 환승은
        어느 종별을 갈아타는지가 곧 갈아타는 값어치입니다.
        """
        parts = []
        for train in self.legs:
            name = (train.train_class_name or "").strip()
            number = (train.train_no or "").strip().lstrip("0") or "?"
            parts.append(f"{name} {number}".strip())
        return " + ".join(parts)

    def leg_summary(self, index: int) -> str:
        """구간 하나를 한 줄로. 환승 목록의 자식 줄이 씁니다."""
        train = self.legs[index]
        return (
            f"{train.departure_station_name}→{train.arrival_station_name} "
            f"{format_clock(normalize_clock(train.departure_time))}-"
            f"{format_clock(normalize_clock(train.arrival_time))} "
            f"({format_duration(self.leg_minutes(index))})"
        )

    def leg_hold_label(self, index: int, *, partial: bool = False) -> str:
        """구간별로 따로 산 예약 하나를 **잡은 예약 목록**에 적을 한 줄.

        여정 전체 요약(:meth:`summary`)을 모든 구간의 '여정' 칸에 그대로
        쓰면, 구간마다 PNR 도 운임도 다른데 그 칸만 똑같아 보여 사람이
        중복 예약으로 오인합니다 — 실제로 그런 신고가 있었습니다. 그래서
        **이 구간이 무엇인지를 앞에 적고, 전체 여정은 참고로만** 붙입니다.

        ``partial`` 은 뒤 구간이 실패해 이 구간만 남았을 때 씁니다 — "구간만"
        이라는 말이 "나머지는 못 잡았다" 를 뜻하기 때문에, 전부 성공했을 때와
        문구를 가릅니다.
        """
        ordinal = f"{index + 1}구간"
        tag = f"[{ordinal}만]" if partial else f"[{ordinal}]"
        note = "원래 여정" if partial else "전체 여정"
        return f"{tag} {self.leg_summary(index)}  ({note}: {self.summary()})"

    def summary(self) -> str:
        """로그·알림·예매 대상에 쓰는 한 줄.

        **총 소요는 환승 대기를 포함합니다** — 첫 구간 출발부터 마지막 구간
        도착까지입니다. 그 사실이 안 보이면 사람이 두 값을 더해 보게 되므로,
        환승이면 대기 시간을 괄호 안에 함께 적습니다.
        """
        route = f"{self.first.departure_station_name}→{self.last.arrival_station_name}"
        times = f"{format_clock(self.departure_clock)}-{format_clock(self.arrival_clock)}"
        total = format_duration(self.total_minutes)
        if not self.is_transfer:
            return f"{self.train_label()}  {route} {times}  총 {total}"
        station = self.transfer_station_name or "환승역"
        legs = " / ".join(
            format_duration(self.leg_minutes(index)) for index in range(len(self.legs))
        )
        return (
            f"{self.train_label()}  {route} {times}  "
            f"총 {total} (구간 {legs} + {station} 대기 "
            f"{format_duration(self.transfer_minutes)})"
        )
