"""자동예매 엔진 — 고른 여정이 열릴 때까지 지켜보다 **한 번만** 잡습니다.

화면과 떨어져 있습니다. Tkinter 를 import 하지 않고, 로그는 콜백으로 내보내며,
중단은 :class:`threading.Event` 로 받습니다. 그래서 네트워크 없이 시험됩니다.

지키는 것은 CLI 쪽(``scripts/watch_and_reserve.py``)과 같습니다.

* 잡으면 그 자리에서 끝납니다. 재시도한 예약은 중복 예약입니다.
* 만드는 consent 는 한 번에 한 범주입니다 — 예약은 ``reserve``, 장바구니는
  ``cart``. 결제·환불·취소 범주는 이 파일 어디에서도 열지 않습니다.
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
    CartAddRequest,
    KorailAppError,
    KorailClient,
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

from .journeys import Journey, SeatPreference, format_duration
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
    #: 홀드를 잡은 뒤 장바구니에도 담습니다.
    add_to_cart: bool = False
    #: 거짓이면 예약 요청을 보내지 않고 미리보기만 받습니다.
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


def reserve_consent(*, live: bool) -> MutationConsent:
    """예약 하나만 여는 consent."""
    consent = MutationConsent(allow_reserve=True, dry_run=not live)
    assert not consent.allow_payment
    assert not consent.allow_cancel
    assert not consent.allow_refund
    return consent


def cart_consent(*, live: bool) -> MutationConsent:
    """장바구니 하나만 여는 consent. 예약 consent 와 섞지 않습니다."""
    consent = MutationConsent(allow_cart=True, dry_run=not live)
    assert not consent.allow_reserve
    assert not consent.allow_payment
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
    clock = (hold.payment_deadline_time or "").strip()
    if len(date) == 8 and len(clock) >= 4:
        return (
            f"{date[:4]}-{date[4:6]}-{date[6:]} "
            f"{clock[:2]}:{clock[2:4]}:{clock[4:6] or '00'}"
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
    ) -> None:
        if not targets:
            raise ValueError("자동예매에는 열차를 하나 이상 골라야 합니다")
        self.client = client
        self.targets = tuple(targets)
        self.options = options
        self._log = log
        self._notify = notify
        self._relogin = relogin
        self._relogins = 0
        #: 방향마다 하나씩만 잡습니다. 잡힌 방향은 여기 들어가고 더는 보지
        #: 않습니다 — 같은 방향을 두 번 잡으면 중복 예약입니다.
        self._settled: dict[tuple[str, str, str], ReservationHoldResponse | None] = {}
        #: 예약 폼 자체가 만들어지지 않는 대상. 되풀이해도 달라지지 않으므로
        #: 한 번 걸리면 빼고 갑니다(서버가 그 행에 필요한 값을 안 준 경우).
        self._unusable: set[tuple[tuple[str, str, str, str], ...]] = set()

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

    def run(self, stop_event: threading.Event | None = None) -> BookingResult:
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
                return BookingResult(Outcome.STOPPED, "중지했습니다", polls=polls)
            if deadline is not None and time.monotonic() >= deadline:
                return BookingResult(
                    Outcome.TIMEOUT, "감시 시간이 끝났습니다", polls=polls
                )
            polls += 1
            try:
                fresh = self._poll()
            except KorailSessionExpiredError:
                if not self._try_relogin():
                    return BookingResult(
                        Outcome.FAILED,
                        f"세션이 {MAX_RELOGIN}번 넘게 끊겼습니다",
                        polls=polls,
                    )
                continue
            except KorailTransportError as exc:
                transport_failures += 1
                if transport_failures >= MAX_CONSECUTIVE_TRANSPORT_FAILURES:
                    return BookingResult(
                        Outcome.FAILED,
                        f"전송이 연속 {transport_failures}번 실패했습니다: {exc}",
                        polls=polls,
                    )
                self.say(f"[{polls}] 전송 실패({transport_failures}회 연속)")
                self._sleep(stop, deadline)
                continue
            except KorailAppError as exc:
                return BookingResult(
                    Outcome.FAILED,
                    f"서버가 실패로 답했습니다({exc.code}): {exc.message}",
                    polls=polls,
                )
            transport_failures = 0
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
        for direction in dict.fromkeys(target.direction for target in pending):
            same = [target for target in pending if target.direction == direction]
            found = search_journeys(self.client, same[0].request)
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

    def _finish(self, kind: str) -> BookingResult | None:
        """방향이 다 끝났으면 마무리합니다. 남았으면 계속 지켜봅니다."""
        if len(self._settled) < len(self.directions):
            remaining = len(self.directions) - len(self._settled)
            self.say(f"    남은 방향 {remaining}개를 계속 지켜봅니다")
            return None
        holds = tuple(hold for hold in self._settled.values() if hold is not None)
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
        self.say(f"    자리가 열렸습니다 — {target.describe()} {label} 예약 시도")
        consent = reserve_consent(live=self.options.live)
        try:
            if journey.is_transfer:
                result = self.client.reserve_transfer(
                    journey.legs,
                    consent=consent,
                    passengers=target.request.passengers,
                    seat_classes=[seat_class] * len(journey.legs),
                )
            else:
                result = self.client.reserve(
                    journey.first,
                    consent=consent,
                    passengers=target.request.passengers,
                    seat_class=seat_class,
                    job_type=KorailReservationJobType.IMMEDIATE,
                )
        except (KorailSeatUnavailableError, KorailSoldOutError) as exc:
            self.say(f"    놓쳤습니다({exc.code}). 계속 지켜봅니다")
            return None
        except KorailSessionExpiredError:
            self._try_relogin()
            return None
        except KorailProtocolError as exc:
            # 서버가 이 행에 예약에 필요한 값을 주지 않았습니다. 자리가 열려도
            # 폼이 만들어지지 않으므로 되풀이할 이유가 없습니다.
            return self._drop_unusable(target, str(exc))
        return self._settle(result, target, journey, kind="좌석 예약")

    def _drop_unusable(self, target: Target, reason: str) -> BookingResult | None:
        self._unusable.add(target.journey.key())
        self.say(
            f"    이 열차는 예약 폼을 만들 수 없어 감시에서 뺍니다 — {reason}"
        )
        if self._pending():
            return None
        return BookingResult(
            Outcome.FAILED,
            "담긴 열차를 모두 예약할 수 없습니다. 서버가 그 행에 예약에 필요한 "
            f"값을 주지 않았습니다({reason}). 수서 출발처럼 KORAIL 예매 대상이 "
            "아닌 열차가 그렇게 옵니다.",
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
        settled = self._settle(result, target, journey, kind="예약대기")
        if isinstance(result, ReservationHoldResponse):
            self._confirm_standby(result)
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
        result: MutationPreview | BaseKorailResponse,
        target: Target,
        journey: Journey,
        *,
        kind: str,
    ) -> BookingResult | None:
        """한 방향이 끝났습니다. 남은 방향이 있으면 계속 지켜봅니다."""
        if isinstance(result, MutationPreview):
            self._settled[target.direction] = None
            self.say(
                f"{kind} 가능 — 하지만 아무것도 보내지 않았습니다(미리보기).\n"
                f"{target.describe()}\n보낼 곳: {result.route}"
            )
            return self._finish(kind)
        hold = result if isinstance(result, ReservationHoldResponse) else None
        pnr = (hold.pnr_no if hold else None) or "(응답에 PNR 이 없습니다)"
        if hold is not None and self.options.add_to_cart:
            self._add_to_cart(hold)
        self._settled[target.direction] = hold
        self.announce(
            f"🚆 {kind} 성공 (아직 결제 전)\n"
            f"{target.describe()}\n"
            f"PNR {pnr}\n"
            f"금액 {fare_text(hold)}\n"
            f"결제 기한 {payment_deadline_text(hold)}\n"
            f"소요 {format_duration(journey.total_minutes)}\n"
            "결제는 코레일 앱에서 기한 안에 하세요."
        )
        finished = self._finish(kind)
        if finished is not None:
            return replace(finished, journey=journey)
        return None

    def _add_to_cart(self, hold: ReservationHoldResponse) -> None:
        pnr = (hold.pnr_no or "").strip()
        if not pnr:
            self.say("    PNR 이 없어 장바구니에 담지 못했습니다")
            return
        try:
            self.client.add_to_cart(
                CartAddRequest(pnr_no=pnr),
                consent=cart_consent(live=self.options.live),
            )
        except KorailAppError as exc:
            self.say(f"    장바구니 담기 실패({exc.code}). 홀드는 그대로입니다")
            return
        self.say("    장바구니에도 담았습니다")

    # -- 곁가지 --------------------------------------------------------------

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
