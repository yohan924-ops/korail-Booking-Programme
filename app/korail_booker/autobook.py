"""자동예매 엔진 — 고른 여정이 열릴 때까지 지켜보다 **한 번만** 잡습니다.

화면과 떨어져 있습니다. Tkinter 를 import 하지 않고, 로그는 콜백으로 내보내며,
중단은 :class:`threading.Event` 로 받습니다. 그래서 네트워크 없이 시험됩니다.

지키는 것은 CLI 쪽(``scripts/watch_and_reserve.py``)과 같습니다.

* 잡으면 그 자리에서 끝납니다. 재시도한 예약은 중복 예약입니다.
* 만드는 consent 는 ``reserve`` 하나뿐입니다. 결제·환불·취소·장바구니
  범주는 이 파일 어디에서도 열지 않습니다.
* ``live`` 가 거짓이면 ``dry_run=True`` 라 아무것도 나가지 않고 미리보기만
  돌아옵니다.
* 주기에 흔들림을 줍니다. 정확히 일정한 간격은 그 자체로 자동화 신호입니다.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum

from korail_mobile_api import (
    KORAIL_STANDBY_HOLD_MESSAGE_CODE,
    BaseKorailResponse,
    KorailAppError,
    KorailClient,
    KorailPassengerCounts,
    KorailProtocolError,
    KorailReservationJobType,
    KorailSeatClass,
    KorailSeatUnavailableError,
    KorailSessionExpiredError,
    KorailSoldOutError,
    KorailTransportError,
    MutationConsent,
    MutationPreview,
    ReservationHoldResponse,
)
from korail_mobile_api.constants import KORAIL_STANDBY_WAIT_FLAG

from .journeys import (
    Journey,
    JourneyKey,
    SeatPreference,
    books_as_one_reservation,
    format_duration,
    normalize_clock,
)
from .search import SearchRequest, search_journeys


#: 조회 주기의 기본값과 하한(초). 하한은 방어선입니다 — 그 아래로는 트래픽이
#: 사람의 새로고침과 구별되지 않고, KORAIL 은 매크로성 트래픽에 IP 를 막습니다.
DEFAULT_POLL_INTERVAL_S = 30.0
MIN_POLL_INTERVAL_S = 10.0
POLL_JITTER = 0.15
#: 세션 만료로 다시 로그인하는 횟수 상한.
MAX_RELOGIN = 3
#: 연속 전송 실패 허용 횟수.
MAX_CONSECUTIVE_TRANSPORT_FAILURES = 5

Logger = Callable[[str], None]
Notifier = Callable[[str], None]


class Outcome(Enum):
    """자동예매가 끝난 이유."""

    HELD = "held"
    #: dry-run. 잡을 수 있었지만 아무것도 보내지 않았습니다.
    PREVIEW = "preview"
    STOPPED = "stopped"
    TIMEOUT = "timeout"
    FAILED = "failed"


@dataclass(frozen=True)
class BookingOptions:
    seat_preference: SeatPreference = SeatPreference.ANY
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    #: 몇 분 동안 지켜볼지. ``0`` 이면 무제한.
    watch_minutes: int = 60
    #: 좌석이 안 열리면 예약대기(``1102``)도 시도합니다. 직통·일반실 전용.
    allow_standby: bool = False
    #: 거짓이면 ``dry_run=True`` 라 예약 요청을 보내지 않고 미리보기만 받습니다.
    #:
    #: 화면에는 이 스위치가 없습니다 — 켜는 것을 잊고 미리보기를 진짜로 믿는
    #: 일이 생겨서 걷어냈고, 지금 화면은 늘 참으로 부릅니다. 여기 남겨 두는
    #: 것은 시험이 "아무것도 보내지 않았다" 를 증명하는 데 쓰기 때문입니다.
    live: bool = False

    def __post_init__(self) -> None:
        if self.poll_interval_s < MIN_POLL_INTERVAL_S:
            raise ValueError(
                f"조회 주기는 {MIN_POLL_INTERVAL_S:g}초 아래로 내릴 수 없습니다"
            )
        if self.watch_minutes < 0:
            raise ValueError("감시 시간은 음수일 수 없습니다")


@dataclass(frozen=True)
class Target:
    """지켜볼 여정 하나와, 그것을 **다시 찾을** 조회 조건.

    조건을 함께 들고 다니는 것은 왕복 때문입니다. 가는 편과 오는 편은 구간도
    날짜도 다른 별개의 조회라, 여정만으로는 다시 찾을 수 없습니다.
    """

    journey: Journey
    request: SearchRequest
    #: 화면에 그대로 찍는 이름 — ``"가는 편"``, ``"오는 편"``, 편도면 빈 문자열.
    label: str = ""

    @property
    def direction(self) -> tuple[str, str, str]:
        """같은 방향인지 가르는 값. 구간과 날짜가 같으면 같은 방향입니다."""
        return (self.request.departure, self.request.arrival, self.request.date)

    def describe(self) -> str:
        return f"{self.label} {self.journey.summary()}".strip()


@dataclass(frozen=True)
class BookingResult:
    outcome: Outcome
    message: str
    journey: Journey | None = None
    hold: ReservationHoldResponse | None = None
    polls: int = 0
    #: 방향마다 하나씩. 편도면 한 건, 왕복이면 두 건입니다.
    holds: tuple[ReservationHoldResponse, ...] = ()

    @property
    def pnr_no(self) -> str | None:
        return self.hold.pnr_no if self.hold is not None else None


#: 끝난 이유마다 한 글자. 알림 목록에서 눈으로 훑을 때 씁니다.
END_MARKS = {
    Outcome.HELD: "✅",
    Outcome.PREVIEW: "👀",
    Outcome.STOPPED: "⏹️",
    Outcome.TIMEOUT: "⏰",
    Outcome.FAILED: "❌",
}


def reserve_consent(*, live: bool) -> MutationConsent:
    """예약 하나만 여는 consent."""
    consent = MutationConsent(allow_reserve=True, dry_run=not live)
    assert not consent.allow_payment
    assert not consent.allow_cancel
    assert not consent.allow_refund
    return consent


def is_standby_available(journey: Journey) -> bool:
    """예약대기가 열려 있는가.

    직통에만 있습니다 — 라이브러리도 환승 여정의 예약대기를 거절합니다
    (``mutation_payloads._build_journey_reservation_form``).
    """
    if journey.is_transfer:
        return False
    return journey.first.wait_reservation_flag == KORAIL_STANDBY_WAIT_FLAG


def payment_deadline_text(hold: ReservationHoldResponse | None) -> str:
    """서버가 준 결제 기한만 씁니다. 지어내지 않습니다."""
    if hold is None:
        return "알 수 없음"
    date = (hold.payment_deadline_date or "").strip()
    # 서버는 이 시각을 JSON 숫자로도 보냅니다 — 09:30 이 93000 으로 옵니다.
    # 자르기만 하면 "93:00:0" 같은 없는 시각이 알림과 목록에 찍히고, 카운트다운
    # (holds.parse_deadline)은 같은 값을 거절해 "기한 모름" 이라고 적습니다.
    # 둘이 어긋나면 사람이 진짜 기한을 알 방법이 없어집니다.
    clock = normalize_clock(hold.payment_deadline_time)
    if len(date) == 8 and date.isdigit() and len(clock) == 6:
        return (
            f"{date[:4]}-{date[4:6]}-{date[6:]} "
            f"{clock[:2]}:{clock[2:4]}:{clock[4:6]}"
        )
    notice = (hold.payment_deadline_notice or "").strip()
    return notice or "서버가 알려주지 않았습니다(앱에서 확인하세요)"


def fare_text(hold: ReservationHoldResponse | None) -> str:
    if hold is None:
        return "알 수 없음"
    amount = hold.received_amount or hold.total_fare or hold.total_price
    if not amount:
        return "알 수 없음"
    try:
        return f"{int(amount):,}원"
    except ValueError:
        return amount


def reserve_once(
    client: KorailClient,
    journey: Journey,
    *,
    passengers: KorailPassengerCounts,
    seat_class: KorailSeatClass,
    live: bool = True,
) -> list[MutationPreview | ReservationHoldResponse]:
    """지금 자리가 있는 여정 하나를 **한 번만** 잡습니다.

    자동예매를 걸지 않고 바로 누르는 [예약] 이 이것을 부릅니다. 되풀이하지
    않는다는 점 말고는 :class:`AutoBooker` 의 예약과 같은 길입니다 — 같은
    consent(``reserve`` 하나), 같은 갈래, 같은 즉시예약 job.

    **돌려주는 것이 목록인 이유**는 직접 조합 환승 때문입니다. 서버가 검증한
    조합이 아니면 한 건으로 사지 않고 구간마다 따로 삽니다
    (:func:`~korail_booker.journeys.books_as_one_reservation`). 그때는 예약이
    둘이고, 부르는 쪽이 둘 다 알아야 합니다. 그 밖에는 늘 한 건입니다.

    실패는 그대로 올려 보냅니다. 자동예매는 "놓쳤다" 를 적고 계속 지켜보는
    것이 맞지만, 사람이 단추를 눌렀을 때는 왜 안 됐는지 그 자리에서 말해
    주어야 합니다. **다만 구간을 따로 살 때 뒤 구간에서 실패하면 앞 구간은
    이미 잡혀 있습니다** — 그 사실을 잃지 않게
    :class:`PartialTransferError` 에 담아 올립니다.
    """
    consent = reserve_consent(live=live)
    if not books_as_one_reservation(journey):
        return _reserve_each_leg(
            client,
            journey,
            consent=consent,
            passengers=passengers,
            seat_class=seat_class,
        )
    if journey.is_transfer:
        return [
            client.reserve_transfer(
                journey.legs,
                consent=consent,
                passengers=passengers,
                seat_classes=[seat_class] * len(journey.legs),
            )
        ]
    return [
        client.reserve(
            journey.first,
            consent=consent,
            passengers=passengers,
            seat_class=seat_class,
            job_type=KorailReservationJobType.IMMEDIATE,
        )
    ]


class PartialTransferError(RuntimeError):
    """구간을 따로 사다가 뒤 구간에서 막혔습니다. **앞 구간은 잡혀 있습니다.**

    그냥 예외를 올리면 앞 구간이 잡혔다는 사실이 사라지고, 사람은 아무것도
    안 됐다고 생각한 채 결제 기한을 넘깁니다. 그래서 잡힌 것을 함께 싣습니다.
    """

    def __init__(
        self,
        held: Sequence[MutationPreview | ReservationHoldResponse],
        leg_number: int,
        reason: str,
    ) -> None:
        self.held = tuple(held)
        self.leg_number = leg_number
        self.reason = reason
        super().__init__(
            f"{leg_number}구간에서 막혔습니다({reason}). "
            f"앞 {len(self.held)}개 구간은 이미 잡혀 있습니다 — "
            "코레일 앱에서 확인해 취소하거나 결제하세요."
        )


def _reserve_each_leg(
    client: KorailClient,
    journey: Journey,
    *,
    consent: MutationConsent,
    passengers: KorailPassengerCounts,
    seat_class: KorailSeatClass,
) -> list[MutationPreview | ReservationHoldResponse]:
    """구간마다 따로 예약합니다. 각 구간은 그냥 직통 열차 한 편입니다."""
    done: list[MutationPreview | ReservationHoldResponse] = []
    for number, leg in enumerate(journey.legs, start=1):
        try:
            done.append(
                client.reserve(
                    leg,
                    consent=consent,
                    passengers=passengers,
                    seat_class=seat_class,
                    job_type=KorailReservationJobType.IMMEDIATE,
                )
            )
        except Exception as exc:
            if not done:
                raise
            raise PartialTransferError(
                done, number, f"{type(exc).__name__}: {exc}"
            ) from exc
    return done


class AutoBooker:
    """고른 여정들을 지켜보다 먼저 열리는 것 하나를 잡습니다."""

    def __init__(
        self,
        client: KorailClient,
        targets: Sequence[Target],
        options: BookingOptions,
        *,
        log: Logger | None = None,
        notify: Notifier | None = None,
        relogin: Callable[[], None] | None = None,
        on_hold: Callable[[str, str, str, ReservationHoldResponse], None] | None = None,
    ) -> None:
        if not targets:
            raise ValueError("자동예매에는 열차를 하나 이상 골라야 합니다")
        self.client = client
        self.targets = tuple(targets)
        self.options = options
        self._log = log
        self._notify = notify
        self._relogin = relogin
        #: 홀드 하나가 잡힐 때마다 (구분, 여정 한 줄, 종류, 응답)으로 부릅니다.
        #: 결과에는 방향별 홀드만 남으므로 어느 여정의 것인지 잃습니다 —
        #: 화면이 목록을 만들려면 그 짝이 필요합니다.
        self._on_hold = on_hold
        self._relogins = 0
        #: 방향마다 한 여정만 잡습니다. 잡힌 방향은 여기 들어가고 더는 보지
        #: 않습니다 — 같은 방향을 두 번 잡으면 중복 예약입니다.
        #:
        #: 값이 **묶음**인 것은 직접 조합 환승 때문입니다. 서버가 검증하지 않은
        #: 조합은 구간마다 따로 사므로 한 방향에 예약이 둘이 됩니다. 빈 묶음은
        #: "미리보기라 아무것도 보내지 않았다" 는 뜻입니다.
        self._settled: dict[
            tuple[str, str, str], tuple[ReservationHoldResponse, ...]
        ] = {}
        #: 예약 폼 자체가 만들어지지 않는 대상. 되풀이해도 달라지지 않으므로
        #: 한 번 걸리면 빼고 갑니다(서버가 그 행에 필요한 값을 안 준 경우).
        self._unusable: set[JourneyKey] = set()

    @property
    def directions(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(dict.fromkeys(target.direction for target in self.targets))

    def _pending(self) -> tuple[Target, ...]:
        return tuple(
            target
            for target in self.targets
            if target.direction not in self._settled
            and target.journey.key() not in self._unusable
        )

    # -- 보고 ----------------------------------------------------------------

    def say(self, message: str) -> None:
        if self._log is not None:
            self._log(message)

    def announce(self, message: str) -> None:
        """로그와 알림 양쪽으로. 알림 실패가 예약을 죽이지 않습니다."""
        self.say(message)
        if self._notify is None:
            return
        try:
            self._notify(message)
        except Exception as exc:
            self.say(f"알림을 보내지 못했습니다: {type(exc).__name__}")

    # -- 실행 ----------------------------------------------------------------

    def watching_text(self) -> str:
        """지금 무엇을 지켜보는지 한 덩어리로. 알림이 이것을 싣습니다."""
        lines = [f"· {target.describe()}" for target in self.targets]
        window = (
            "무제한"
            if self.options.watch_minutes == 0
            else f"{self.options.watch_minutes}분"
        )
        return (
            f"{len(self.targets)}편 감시 · {self.options.poll_interval_s:g}초마다 · "
            f"{window}\n" + "\n".join(lines)
        )

    def run(self, stop_event: threading.Event | None = None) -> BookingResult:
        """한 번 돌고 결과를 돌려줍니다. **시작과 끝을 반드시 알립니다.**

        잡았을 때만 알리면, 알림이 안 오는 것이 "아직 안 잡힘" 인지 "애초에
        안 돌고 있음" 인지 구별되지 않습니다. 밤새 켜 두는 프로그램에서 그
        둘은 아주 다릅니다.

        끝나는 갈래가 여럿이라(잡음·중지·시간 끝·실패) 고리는 :meth:`_run`
        에 두고 알림은 여기서 감쌉니다 — return 마다 적어 두면 언젠가 하나를
        빠뜨립니다.
        """
        self.announce(f"▶️ 자동예매 시작\n{self.watching_text()}")
        try:
            result = self._run(stop_event)
        except BaseException as exc:
            # 여기서 새면 아래 알림이 통째로 건너뛰어집니다. 밤새 켜 둔 사람은
            # 시작 알림만 받고 아무 소식도 못 받습니다 — 그것이 "아직 안 잡힘"
            # 인지 "죽었음" 인지 구별되지 않습니다. 잡아 둔 것도 함께 싣습니다.
            result = self._result(
                Outcome.FAILED, f"예상 못 한 오류로 멈췄습니다: {type(exc).__name__}: {exc}"
            )
            self.announce(
                f"{END_MARKS[Outcome.FAILED]} 자동예매 종료 (failed)\n"
                f"{result.message}\n— {self.watching_text()}"
            )
            raise
        self.announce(
            f"{END_MARKS.get(result.outcome, '■')} 자동예매 종료 "
            f"({result.outcome.value})\n{result.message}\n"
            f"— {self.watching_text()}"
        )
        return result

    def _result(self, outcome: Outcome, message: str, *, polls: int = 0) -> BookingResult:
        """끝나는 갈래 하나. **이미 잡은 예약을 반드시 싣습니다.**

        예전에는 잡음(HELD) 갈래에서만 홀드를 실었습니다. 그래서 왕복에서 가는
        편을 잡아 둔 채 오는 편이 시간 끝을 만나면, 결과에 홀드가 하나도 없어
        화면이 그 예약을 잃었습니다 — PNR 도 결제 기한도 함께 사라집니다.
        """
        holds = tuple(hold for group in self._settled.values() for hold in group)
        return BookingResult(
            outcome,
            message if not holds else f"{message} (이미 잡은 예약 {len(holds)}건이 있습니다)",
            hold=holds[0] if holds else None,
            holds=holds,
            polls=polls,
        )

    def _run(self, stop_event: threading.Event | None = None) -> BookingResult:
        stop = stop_event or threading.Event()
        deadline = (
            None
            if self.options.watch_minutes == 0
            else time.monotonic() + self.options.watch_minutes * 60.0
        )
        polls = 0
        transport_failures = 0
        while True:
            if stop.is_set():
                return self._result(Outcome.STOPPED, "중지했습니다", polls=polls)
            if deadline is not None and time.monotonic() >= deadline:
                return self._result(
                    Outcome.TIMEOUT, "감시 시간이 끝났습니다", polls=polls
                )
            polls += 1
            try:
                fresh = self._poll()
            except KorailSessionExpiredError:
                if not self._try_relogin():
                    return self._result(
                        Outcome.FAILED,
                        f"세션이 {MAX_RELOGIN}번 넘게 끊겼습니다",
                        polls=polls,
                    )
                continue
            except KorailTransportError as exc:
                transport_failures += 1
                if transport_failures >= MAX_CONSECUTIVE_TRANSPORT_FAILURES:
                    return self._result(
                        Outcome.FAILED,
                        f"전송이 연속 {transport_failures}번 실패했습니다: {exc}",
                        polls=polls,
                    )
                self.say(f"[{polls}] 전송 실패({transport_failures}회 연속)")
                self._sleep(stop, deadline)
                continue
            except KorailAppError as exc:
                return self._result(
                    Outcome.FAILED,
                    f"서버가 실패로 답했습니다({exc.code}): {exc.message}",
                    polls=polls,
                )
            # 한 번 제대로 돌았으면 앞선 실패는 지웁니다. 밤새 도는 감시에서
            # 평범한 세션 만료 네 번에 죽는 것은 고장입니다.
            transport_failures = 0
            self._relogins = 0
            result = self._act_on(polls, fresh)
            if result is not None:
                return result
            self._sleep(stop, deadline)

    def _poll(self) -> list[tuple[Target, Journey]]:
        """아직 안 잡힌 방향만 다시 조회합니다.

        방향(구간+날짜)마다 조회는 한 번입니다. 같은 방향의 대상이 여럿이면 그
        한 번의 결과에서 골라 씁니다 — 대상마다 조회하면 요청이 곱으로 늘어납니다.
        """
        fresh: list[tuple[Target, Journey]] = []
        pending = self._pending()
        # **조회 조건이 같은 것끼리** 묶습니다. 방향(구간+날짜)만 보고 묶으면
        # 첫 대상의 조건으로만 물어보게 되는데, 승객 수나 열차 종류나 환승
        # 설정이 다른 대상은 그 결과에 없어 영영 안 잡힙니다.
        for group in dict.fromkeys(target.request for target in pending):
            same = [target for target in pending if target.request == group]
            found = search_journeys(self.client, group, log=self.say, strict=True)
            by_key = {journey.key(): journey for journey in found}
            for target in same:
                journey = by_key.get(target.journey.key())
                if journey is not None:
                    fresh.append((target, journey))
        return fresh

    def _act_on(
        self,
        poll: int,
        fresh: Sequence[tuple[Target, Journey]],
    ) -> BookingResult | None:
        if not fresh:
            self.say(f"[{poll}] 고른 열차가 조회 결과에 없습니다")
            return None
        self._report(poll, fresh)
        for target, journey in fresh:
            if target.direction in self._settled:
                continue
            seat_class = journey.bookable_seat_class(self.options.seat_preference)
            if seat_class is None:
                continue
            result = self._try_reserve(target, journey, seat_class)
            if result is not None:
                return result
        if self.options.allow_standby:
            for target, journey in fresh:
                if target.direction not in self._settled and is_standby_available(
                    journey
                ):
                    result = self._try_standby(target, journey)
                    if result is not None:
                        return result
        return None

    def _open_directions(self) -> tuple[tuple[str, str, str], ...]:
        """아직 결판이 안 난 방향. **노릴 대상이 남아 있어야** 셉니다.

        예전에는 잡힌 방향만 뺐습니다. 그래서 한 방향의 대상이 전부 '예약 폼을
        만들 수 없음' 으로 빠지면, 그 방향은 영영 안 잡히는데도 끝나지 않은
        것으로 세어 감시가 빈 목록을 들고 계속 돌았습니다.
        """
        alive = {target.direction for target in self._pending()}
        return tuple(d for d in self.directions if d not in self._settled and d in alive)

    def _finish(self, kind: str) -> BookingResult | None:
        """방향이 다 끝났으면 마무리합니다. 남았으면 계속 지켜봅니다."""
        remaining = self._open_directions()
        if remaining:
            self.say(f"    남은 방향 {len(remaining)}개를 계속 지켜봅니다")
            return None
        holds = tuple(hold for group in self._settled.values() for hold in group)
        if not holds:
            return BookingResult(
                Outcome.PREVIEW,
                f"{kind} 가능 — 하지만 아무것도 보내지 않았습니다(미리보기).",
                polls=0,
            )
        return BookingResult(
            Outcome.HELD,
            f"{len(holds)}건을 잡았습니다.",
            hold=holds[0],
            holds=holds,
        )

    def _report(self, poll: int, fresh: Sequence[tuple[Target, Journey]]) -> None:
        parts = []
        for target, journey in fresh:
            state = ", ".join(
                f"{seat_class.name[:2]}:{journey.seat_state(seat_class).label}"
                for seat_class in self.options.seat_preference.seat_classes()
            )
            prefix = f"{target.label} " if target.label else ""
            parts.append(f"{prefix}{'+'.join(journey.train_numbers())}({state})")
        self.say(f"[{poll}] {' / '.join(parts)}")

    # -- 예약 ----------------------------------------------------------------

    def _try_reserve(
        self,
        target: Target,
        journey: Journey,
        seat_class: KorailSeatClass,
    ) -> BookingResult | None:
        label = "특실" if seat_class is KorailSeatClass.SPECIAL else "일반실"
        # 다시 조회한 여정에는 **이번 조회의** 출처가 붙어 있습니다. 사는 방법은
        # 사람이 담을 때 본 것을 따릅니다 — 확인 창이 "구간마다 따로 삽니다" 라고
        # 말해 놓고 한 건으로 사면 약속이 깨집니다. 구간의 값은 방금 받은 것을
        # 그대로 씁니다(좌석 코드가 바뀌어 있을 수 있습니다).
        journey = replace(journey, source=target.journey.source)
        one_go = books_as_one_reservation(journey)
        how = "" if one_go else " (구간마다 따로)"
        self.say(
            f"    자리가 열렸습니다 — {target.describe()} {label} 예약 시도{how}"
        )
        if not one_go:
            self.say(
                "    서버가 검증한 환승 조합이 아니라 한 건으로 사지 않습니다 — "
                "구간마다 예약이 따로 생기고 결제도 따로입니다."
            )
        try:
            results = reserve_once(
                self.client,
                journey,
                passengers=target.request.passengers,
                seat_class=seat_class,
                live=self.options.live,
            )
        except PartialTransferError as exc:
            return self._settle_partial(target, journey, exc)
        except (KorailSeatUnavailableError, KorailSoldOutError) as exc:
            self.say(f"    놓쳤습니다({exc.code}). 계속 지켜봅니다")
            return None
        except KorailSessionExpiredError:
            self.say("    세션이 끊겨 예약을 못 보냈습니다. 다시 로그인합니다")
            if not self._try_relogin():
                return self._result(
                    Outcome.FAILED, f"세션이 {MAX_RELOGIN}번 넘게 끊겼습니다"
                )
            return None
        except KorailTransportError as exc:
            # **되풀이하지 않습니다.** 요청이 나간 뒤 끊긴 것인지 나가기 전에
            # 끊긴 것인지 여기서는 알 수 없습니다. 다시 보내면 서버에 이미
            # 생긴 예약 위에 하나를 더 만들 수 있습니다 — 그것이 중복 예약이고,
            # 이 프로그램은 취소를 하지 않으므로 사람이 치워야 합니다.
            return self._settle_broken(target, journey, exc)
        except KorailProtocolError as exc:
            # 서버가 이 행에 예약에 필요한 값을 주지 않았습니다. 자리가 열려도
            # 폼이 만들어지지 않으므로 되풀이할 이유가 없습니다.
            return self._drop_unusable(target, str(exc))
        except KorailAppError as exc:
            # 서버가 이 조합 자체를 거절했습니다(예: ERR911193 환승최소허용시간
            # 미달). 되풀이해도 답이 달라지지 않으므로 감시에서 뺍니다.
            return self._drop_unusable(target, f"{exc.code}: {exc.message}")
        return self._settle(
            results,
            target,
            journey,
            kind="좌석 예약" if one_go else "좌석 예약(구간별)",
        )

    def _settle_broken(
        self,
        target: Target,
        journey: Journey,
        exc: KorailTransportError,
    ) -> BookingResult | None:
        """예약 요청이 전송 중에 끊겼습니다. **결과를 알 수 없습니다.**

        서버가 그 요청을 받아 예약을 만들었는지 아닌지 판단할 근거가 없습니다.
        그래서 이 방향은 여기서 끝내고 크게 알립니다 — 다시 보내면 중복 예약이
        될 수 있고, 조용히 넘어가면 사람이 생긴 예약을 모른 채 기한을 넘깁니다.
        """
        self._settled[target.direction] = ()
        self.announce(
            "⚠️ 예약 요청이 전송 중에 끊겼습니다\n"
            f"{target.describe()}\n"
            f"{type(exc).__name__}: {exc}\n"
            "서버에 예약이 생겼는지 여기서는 알 수 없습니다. 다시 보내지 "
            "않습니다 — 이미 생겼다면 중복 예약이 되기 때문입니다.\n"
            "코레일 앱에서 예약 내역을 확인하세요."
        )
        return self._finish("좌석 예약")

    def _settle_partial(
        self,
        target: Target,
        journey: Journey,
        exc: PartialTransferError,
    ) -> BookingResult | None:
        """앞 구간만 잡히고 뒤 구간에서 막혔습니다.

        **다시 시도하지 않습니다.** 다음 회차에 또 돌면 이미 잡아 둔 앞 구간을
        한 번 더 잡습니다 — 그것이 중복 예약입니다. 그래서 이 방향은 여기서
        끝내고, 무슨 일이 있었는지 크게 알립니다. 앞 구간을 취소할지 다른 뒤
        구간을 잡을지는 사람이 코레일 앱에서 정할 일입니다 — 이 프로그램은
        취소 권한을 아예 열지 않습니다.
        """
        held = tuple(h for h in exc.held if isinstance(h, ReservationHoldResponse))
        self._settled[target.direction] = held
        for hold in held:
            if self._on_hold is not None:
                self._on_hold(target.label, journey.summary(), "좌석 예약(구간별)", hold)
        pnrs = ", ".join((hold.pnr_no or "?") for hold in held) or "(없음)"
        self.announce(
            "⚠️ 환승 일부만 잡혔습니다\n"
            f"{target.describe()}\n"
            f"잡힌 구간의 PNR {pnrs}\n"
            f"{exc.leg_number}구간 실패: {exc.reason}\n"
            "서버가 검증한 조합이 아니라 구간마다 따로 샀기 때문에 한쪽만 "
            "남았습니다. 코레일 앱에서 잡힌 구간을 결제하거나 취소하세요 — "
            "이 프로그램은 취소를 하지 않습니다."
        )
        return self._finish("좌석 예약(구간별)")

    def _drop_unusable(self, target: Target, reason: str) -> BookingResult | None:
        self._unusable.add(target.journey.key())
        self.say(
            f"    이 열차는 예약 폼을 만들 수 없어 감시에서 뺍니다 — {reason}"
        )
        if self._pending():
            return None
        # 이미 잡아 둔 것이 있으면 그것이 결과입니다. 하나도 없으면 "잡을 수
        # 있었지만 안 보냈다"(미리보기)가 아니라 **실패**입니다 — 애초에 폼을
        # 만들 수 없었으니까요.
        held = tuple(hold for group in self._settled.values() for hold in group)
        if held:
            return self._result(Outcome.HELD, f"{len(held)}건을 잡았습니다.")
        return self._result(
            Outcome.FAILED,
            "담긴 열차를 모두 예약할 수 없습니다. 조회 결과의 그 행에 예약 폼이 "
            f"요구하는 값이 없습니다({reason}). 왜 그렇게 오는지는 확인되지 "
            "않았습니다.",
        )

    def _try_standby(self, target: Target, journey: Journey) -> BookingResult | None:
        """예약대기(1102). 일반실 직통에서만 성립합니다."""
        if self.options.seat_preference is SeatPreference.SPECIAL:
            return None
        self.say(f"    예약대기 시도 — {target.describe()}")
        try:
            result = self.client.reserve(
                journey.first,
                consent=reserve_consent(live=self.options.live),
                passengers=target.request.passengers,
                seat_class=KorailSeatClass.GENERAL,
                job_type=KorailReservationJobType.STANDBY,
            )
        except (KorailSeatUnavailableError, KorailSoldOutError) as exc:
            self.say(f"    예약대기 실패({exc.code})")
            return None
        except KorailProtocolError as exc:
            self.say(f"    예약대기 조건이 아닙니다: {exc}")
            return None
        except KorailAppError as exc:
            # 예약대기는 곁가지입니다. 서버가 거절했다고 좌석 감시까지
            # 죽이지 않습니다.
            self.say(f"    예약대기를 서버가 거절했습니다({exc.code}): {exc.message}")
            return None
        settled = self._settle([result], target, journey, kind="예약대기")
        if isinstance(result, ReservationHoldResponse):
            # 확인 호출이 실패해도 **예약은 이미 잡혀 있습니다.** 여기서 예외가
            # 새면 그 사실이 통째로 사라집니다.
            try:
                self._confirm_standby(result)
            except Exception as exc:
                self.say(
                    f"    대기 옵션 기록에 실패했습니다({type(exc).__name__}). "
                    "홀드는 남아 있습니다 — 앱에서 확인하세요"
                )
        return settled

    def _confirm_standby(self, hold: ReservationHoldResponse) -> None:
        """대기 화면을 여는 마무리 호출. 앱은 ``IRR000014`` 에서만 넘어갑니다."""
        if hold.h_msg_cd != KORAIL_STANDBY_HOLD_MESSAGE_CODE:
            self.say(
                f"    대기 확인 코드가 {hold.h_msg_cd} 입니다. 확인 호출은 "
                "보내지 않습니다 — 앱에서 확인하세요"
            )
            return
        try:
            self.client.confirm_standby_hold(
                hold,
                consent=reserve_consent(live=self.options.live),
            )
        except KorailAppError as exc:
            self.say(f"    대기 옵션 기록 실패({exc.code}). 홀드는 남아 있습니다")
            return
        self.say("    대기 옵션을 기록했습니다")

    def _settle(
        self,
        results: Sequence[MutationPreview | BaseKorailResponse],
        target: Target,
        journey: Journey,
        *,
        kind: str,
    ) -> BookingResult | None:
        """한 방향이 끝났습니다. 남은 방향이 있으면 계속 지켜봅니다.

        ``results`` 가 여럿인 것은 구간마다 따로 산 경우입니다 — 그때는 예약이
        구간 수만큼 생기고, 결제 기한도 저마다 따로입니다.
        """
        previews = [item for item in results if isinstance(item, MutationPreview)]
        if previews:
            self._settled[target.direction] = ()
            routes = ", ".join(preview.route for preview in previews)
            self.say(
                f"{kind} 가능 — 하지만 아무것도 보내지 않았습니다(미리보기).\n"
                f"{target.describe()}\n보낼 곳: {routes}"
            )
            return self._finish(kind)
        holds = tuple(
            item for item in results if isinstance(item, ReservationHoldResponse)
        )
        self._settled[target.direction] = holds
        if not holds:
            self.say(f"{kind} 응답에 예약이 없습니다 — 코레일 앱에서 확인하세요")
            return self._finish(kind)
        split = len(holds) > 1
        for number, hold in enumerate(holds, start=1):
            if self._on_hold is not None:
                self._on_hold(target.label, journey.summary(), kind, hold)
            where = f" [{number}구간]" if split else ""
            self.announce(
                f"🚆 {kind} 성공{where} (아직 결제 전)\n"
                f"{target.describe()}\n"
                f"PNR {hold.pnr_no or '(응답에 PNR 이 없습니다)'}\n"
                f"금액 {fare_text(hold)}\n"
                f"결제 기한 {payment_deadline_text(hold)}\n"
                f"소요 {format_duration(journey.total_minutes)}\n"
                + (
                    "구간마다 따로 산 예약입니다 — 예약도 결제도 구간 수만큼 "
                    "따로입니다.\n"
                    if split
                    else ""
                )
                + "결제는 코레일 앱에서 기한 안에 하세요."
            )
        finished = self._finish(kind)
        if finished is not None:
            return replace(finished, journey=journey)
        return None

    def _try_relogin(self) -> bool:
        if self._relogin is None:
            return False
        self._relogins += 1
        if self._relogins > MAX_RELOGIN:
            return False
        self.say(f"세션이 끊겨 다시 로그인합니다({self._relogins}회차)")
        self._relogin()
        return True

    def _sleep(self, stop: threading.Event, deadline: float | None) -> None:
        base = self.options.poll_interval_s
        delay = base * (1.0 + random.uniform(-POLL_JITTER, POLL_JITTER))
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            delay = min(delay, remaining)
        stop.wait(delay)


@dataclass
class BookingSession:
    """UI 가 스레드 하나를 쥐고 있게 해 주는 얇은 껍데기."""

    booker: AutoBooker
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    result: BookingResult | None = None

    def start(self, on_done: Callable[[BookingResult], None] | None = None) -> None:
        def _run() -> None:
            try:
                self.result = self.booker.run(self.stop_event)
            except Exception as exc:
                self.result = BookingResult(
                    Outcome.FAILED, f"{type(exc).__name__}: {exc}"
                )
            if on_done is not None:
                on_done(self.result)

        self.thread = threading.Thread(target=_run, name="korail-autobook", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()
