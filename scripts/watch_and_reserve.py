"""만석인 KORAIL 열차를 지켜보다가, 자리가 열리면 **딱 한 번** 잡습니다.

취소표를 기다리는 일을 사람 대신 합니다. 조회를 되풀이하고, 조건에 맞는 열차의
좌석 코드가 예매 가능(``"11"``)으로 바뀌는 순간 결제 전 홀드를 하나 만든 뒤
**즉시 멈춥니다.** 결제는 하지 않습니다.

안전 자세
--------
* **기본은 아무것도 바꾸지 않습니다.** ``--reserve`` 없이 돌리면 조회만 하고,
  자리가 열리면 실제로 나갈 폼을 dry-run 미리보기로 보여 준 뒤 끝냅니다.
  라이브러리의 ``MutationConsent(dry_run=True)`` 가 그 요청을 보내지 않습니다.
* **실제로 잡으려면 스위치 세 개가 다 필요합니다** — ``KORAIL_MOBILE_API_LIVE=1``
  (패키지 전체의 라이브 스위치), ``KORAIL_LIVE_MUTATION=1``(이 실행이 상태를
  바꿔도 된다), 그리고 ``--reserve``. 하나라도 빠지면 예약 요청은 나가지
  않습니다.
* **성공하면 그 자리에서 끝납니다.** 홀드를 하나 만든 뒤에는 다른 열차를 다시
  시도하지 않습니다. 재시도한 예약은 중복 예약이기 때문입니다 — 라이브러리가
  스스로 재시도하지 않는 이유와 같습니다.
* **결제 범주의 consent 를 만들지 않습니다.** 이 파일이 만드는 consent 는
  ``allow_reserve`` 하나뿐이라, 카드가 이 프로세스에 들어올 일이 없습니다.
  결제와 취소는 코레일 앱에서 하거나 ``reserve_pay_refund_roundtrip.py`` 로
  합니다.
* **자격증명은 환경변수에서만** 옵니다(``KORAIL_MEMBER_NO``,
  ``KORAIL_PASSWORD``). 파일에서도, 명령줄 인자에서도 읽지 않습니다 — argv 는
  ``ps`` 로 남에게 보입니다.
* **요청은 두 겹으로 늦춰집니다.** HTTP 훅의 최소 간격(기본 1.5초)과 조회
  주기(기본 30초, 하한 10초)입니다. KORAIL 은 매크로성 트래픽에 IP 를 막습니다.
  주기에는 ±15% 흔들림을 줍니다 — 정확히 일정한 간격 자체가 봇 신호입니다.
* **import 는 안전합니다.** 이 모듈을 import 하는 것만으로는 I/O 도, 환경변수
  읽기도, 클라이언트 생성도 일어나지 않습니다. 전부 :func:`main` 아래입니다.

이건 매크로입니다. 본인 계정으로, 본인이 탈 표를 기다리는 데 쓰라고 만든
것입니다. 사람 대신 줄을 서 주지만, 서버가 그 줄을 어떻게 볼지는 KORAIL 이
정합니다.

무엇을 잡나
----------
* 기본은 **좌석 예매**(``txtJobId=1101``). 고른 객실 등급의 예약 코드가 정확히
  ``"11"`` 일 때만 시도합니다. 그것이 라이브러리가 예약 폼을 만들어 주는
  유일한 상태입니다.
* ``--standby`` 를 주면 좌석이 끝내 안 나올 때 **예약대기**(``1102``)로
  넘어갑니다. 예약대기는 일반실에만 있고, 열차의 ``h_wait_rsv_flg`` 가
  대기 가능 값일 때만 성립합니다. 홀드가 잡히면 ``confirm_standby_hold`` 로
  마무리합니다.
* **입석+좌석 병합(``1202``)은 넣지 않았습니다.** 첫 홀드는 라이브로 확인됐지만
  두 번째 호출인 ``reserve_merge`` 는 실서버에 나간 적이 없습니다. 반쯤 검증된
  경로로 진짜 예약을 만들지 않습니다.

환경변수
-------
``KORAIL_MOBILE_API_LIVE``
    ``1`` 이어야 아무것도 시작합니다. 조회만 하는 실행에도 필요합니다.
``KORAIL_MEMBER_NO`` / ``KORAIL_PASSWORD``
    로그인 자격증명. 없으면 로그인 전에 멈춥니다.
``KORAIL_LIVE_MUTATION``
    ``--reserve`` 로 진짜 홀드를 만들 때만 ``1``.
``KORAIL_DYNAPATH_DEVICE_ID`` / ``KORAIL_DYNAPATH_OS_VERSION`` /
``KORAIL_DYNAPATH_DEVICE_MODEL``
    실기기 값. 셋을 다 주면 그 값으로 고정하고, 셋 다 없으면 합성 값으로
    돕니다(둘 다 로그인됩니다). 셋 중 일부만 주면 거절합니다.

예시
----
::

    export KORAIL_MOBILE_API_LIVE=1
    export KORAIL_MEMBER_NO=... KORAIL_PASSWORD=...

    # 1) 지켜보기만. 자리가 열리면 나갈 폼을 보여 주고 멈춥니다.
    python3 scripts/watch_and_reserve.py --from 서울 --to 부산 \\
        --date 20260810 --after 08:00 --before 12:00

    # 2) 진짜로 잡기.
    export KORAIL_LIVE_MUTATION=1
    python3 scripts/watch_and_reserve.py --from 서울 --to 부산 \\
        --date 20260810 --after 08:00 --before 12:00 --adult 2 --reserve
"""

from __future__ import annotations

import argparse
import os
import random
import time
from dataclasses import dataclass, field

from korail_mobile_api import (
    KORAIL_STANDBY_HOLD_MESSAGE_CODE,
    BaseKorailResponse,
    KorailAppError,
    KorailAppUpdateRequiredError,
    KorailAuthError,
    KorailClient,
    KorailConfig,
    KorailDynaPathError,
    KorailNoResultsError,
    KorailPassengerCounts,
    KorailProtocolError,
    KorailReservationJobType,
    KorailReservationRefusedError,
    KorailSeatClass,
    KorailSeatUnavailableError,
    KorailSessionExpiredError,
    KorailSoldOutError,
    KorailTransportError,
    MutationConsent,
    MutationPreview,
    ReservationHoldResponse,
    TrainSearchQuery,
    TrainSummary,
)

# 이 상수는 __all__ 에 없다. 공개면 규칙이 "설정 필드의 기본값으로 이미 닿는
# 상수"를 내보내지 않기 때문이지, 값이 흔들려서가 아니다. 같은 저장소 안의
# 스크립트라 하드코딩하는 대신 정의된 곳에서 가져온다 — 그래야 값이 바뀌면
# 여기도 같이 바뀐다.
from korail_mobile_api.constants import KORAIL_STANDBY_WAIT_FLAG
from korail_mobile_api.live import (
    build_config_from_env,
    live_enabled,
    read_credentials_from_env,
)


LIVE_MUTATION_ENV = "KORAIL_LIVE_MUTATION"
DEVICE_ID_ENV = "KORAIL_DYNAPATH_DEVICE_ID"
OS_VERSION_ENV = "KORAIL_DYNAPATH_OS_VERSION"
DEVICE_MODEL_ENV = "KORAIL_DYNAPATH_DEVICE_MODEL"

#: HTTP 훅에 거는 요청 간 최소 간격. 조회 한 번이 여러 요청으로 갈라져도
#: 이 간격은 지켜집니다.
DEFAULT_MIN_INTERVAL_S = 1.5
#: 조회 주기의 기본값과 하한. 하한은 취향이 아니라 방어선입니다 — 그 아래로는
#: 트래픽이 사람의 새로고침과 구별되지 않습니다.
DEFAULT_POLL_INTERVAL_S = 30.0
MIN_POLL_INTERVAL_S = 10.0
#: 주기에 주는 흔들림. 정확히 일정한 간격은 그 자체로 자동화 신호입니다.
POLL_JITTER = 0.15
#: 기본 감시 시간(분). ``0`` 이면 무제한입니다.
DEFAULT_WATCH_MINUTES = 60
#: 한 번의 조회에서 넘겨 볼 페이지 수 상한. 페이지마다 요청이 하나 더 나갑니다.
DEFAULT_MAX_PAGES = 3
#: 세션 만료로 다시 로그인하는 횟수 상한.
MAX_RELOGIN = 3
#: 연속 전송 실패 허용 횟수. 넘으면 멈춥니다 — 네트워크가 아니라 우리가 막힌
#: 것일 수 있습니다.
MAX_CONSECUTIVE_TRANSPORT_FAILURES = 5
#: "이 객실에 예매 가능한 자리가 있다"는 유일한 값. 라이브러리의 예약 폼도
#: 같은 값만 받아들입니다(``mutation_payloads._assert_leg_is_bookable``).
AVAILABLE_SEAT_CODE = "11"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ABORTED = 2
EXIT_NOT_FOUND = 3


class WatchAborted(RuntimeError):
    """사용자가 고칠 수 있는 이유로 멈춥니다. 스택트레이스 없이 보고합니다."""


class _Console:
    """타임스탬프를 붙여 stdout 에 씁니다. 파일에는 아무것도 쓰지 않습니다."""

    def say(self, message: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)

    def banner(self, lines: tuple[str, ...]) -> None:
        rule = "=" * 72
        print("", flush=True)
        print(rule, flush=True)
        for line in lines:
            print(f"  {line}", flush=True)
        print(rule, flush=True)
        print("", flush=True)


class _Pacer:
    """요청 사이의 최소 간격을 강제합니다."""

    def __init__(self, min_interval_s: float) -> None:
        self.min_interval_s = min_interval_s
        self._last: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self._last is not None:
            remaining = self.min_interval_s - (now - self._last)
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()


def _install_pacing(client: KorailClient, pacer: _Pacer) -> None:
    """클라이언트가 스스로 부르는 요청까지 포함해 전부 늦춥니다."""
    inner = client.http._client
    hooks = dict(inner.event_hooks)
    hooks["request"] = [
        *hooks.get("request", []),
        lambda request: pacer.wait(),
    ]
    inner.event_hooks = hooks


# --- 감시 계획 ----------------------------------------------------------------


@dataclass(frozen=True)
class WatchPlan:
    """한 번의 감시 실행이 무엇을 찾는지. 전부 실행 전에 정해집니다."""

    query: TrainSearchQuery
    passengers: KorailPassengerCounts
    seat_class: KorailSeatClass
    #: 앞의 0 을 뗀 열차번호. 비어 있으면 번호로 거르지 않습니다.
    train_numbers: frozenset[str] = frozenset()
    #: ``HHMMSS``. 빈 문자열이면 그쪽 끝을 제한하지 않습니다.
    depart_after: str = ""
    depart_before: str = ""
    #: ``h_trn_clsf_nm`` 부분일치(예: ``"KTX"``). 빈 문자열이면 전부.
    train_name: str = ""
    allow_standby: bool = False
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    max_pages: int = DEFAULT_MAX_PAGES
    #: 감시 마감(``time.monotonic`` 기준). ``None`` 이면 무제한.
    deadline: float | None = field(default=None)


def normalize_train_no(train_no: str) -> str:
    """``"00123"`` 과 ``"123"`` 을 같은 것으로 봅니다.

    검색 행의 ``h_trn_no`` 는 0 으로 채워져 오는데, 사람은 앱 화면에 보이는
    대로 ``123`` 이라고 적습니다.
    """
    return train_no.strip().lstrip("0")


def departure_time_of(train: TrainSummary) -> str:
    """비교할 수 있는 ``HHMMSS``. 서버가 앞의 0 을 떨어뜨린 값을 되살립니다.

    ``h_dpt_tm`` 은 JSON 숫자로 오기도 해서 ``"063000"`` 이 ``"63000"`` 으로
    도착합니다(``models._train_scalar`` 의 주석 참조). 문자열로 그냥 비교하면
    새벽 열차가 시간창 밖으로 밀려납니다.
    """
    raw = (train.departure_time or "").strip()
    return raw.zfill(6) if raw else ""


def cabin_reservation_code(
    train: TrainSummary,
    seat_class: KorailSeatClass,
) -> str | None:
    """고른 객실의 예약 가능 코드. 앱도 탭에 따라 다른 필드를 봅니다."""
    if seat_class is KorailSeatClass.SPECIAL:
        return train.special_reservation_code
    return train.general_reservation_code


def cabin_availability_name(
    train: TrainSummary,
    seat_class: KorailSeatClass,
) -> str:
    """앱이 화면에 찍는 문구(``"매진"``, ``"좌석부족"`` …). 로그용입니다."""
    if seat_class is KorailSeatClass.SPECIAL:
        return (train.special_availability_name or "").strip()
    return (train.general_availability_name or "").strip()


def is_reservable(train: TrainSummary, seat_class: KorailSeatClass) -> bool:
    return cabin_reservation_code(train, seat_class) == AVAILABLE_SEAT_CODE


def is_standby_available(train: TrainSummary) -> bool:
    """예약대기가 열려 있는가. 이 플래그 하나가 정합니다."""
    return train.wait_reservation_flag == KORAIL_STANDBY_WAIT_FLAG


def matches(train: TrainSummary, plan: WatchPlan) -> bool:
    """좌석 상태와 무관하게, 이 열차가 애초에 감시 대상인지."""
    if plan.train_numbers:
        if normalize_train_no(train.train_no or "") not in plan.train_numbers:
            return False
    if plan.train_name:
        name = (train.train_class_name or "").strip()
        if plan.train_name.casefold() not in name.casefold():
            return False
    departure = departure_time_of(train)
    if plan.depart_after and departure and departure < plan.depart_after:
        return False
    return not (plan.depart_before and departure and departure > plan.depart_before)


def collect_candidates(client: KorailClient, plan: WatchPlan) -> list[TrainSummary]:
    """조건에 맞는 열차를 모읍니다. 좌석이 있든 없든 전부 돌려줍니다.

    페이지는 ``plan.max_pages`` 까지만 넘깁니다. 시간창의 끝을 지난 페이지가
    나오면 거기서 멈춥니다 — 검색은 시간순이라 더 넘겨도 뒤쪽 열차뿐입니다.
    """
    found: list[TrainSummary] = []
    continuation = None
    for _ in range(plan.max_pages):
        result = client.search_trains(plan.query, continuation=continuation)
        found.extend(train for train in result.trains if matches(train, plan))
        if plan.depart_before and any(
            departure_time_of(train) > plan.depart_before
            for train in result.trains
            if departure_time_of(train)
        ):
            break
        continuation = result.next_page()
        if continuation is None:
            break
    return found


def reserve_consent(*, live: bool) -> MutationConsent:
    """예약 하나만 여는 consent. 결제도 취소도 열지 않습니다."""
    consent = MutationConsent(allow_reserve=True, dry_run=not live)
    assert not consent.allow_payment
    assert not consent.allow_cancel
    assert not consent.allow_refund
    return consent


# --- 감시 실행 ----------------------------------------------------------------


class Watcher:
    """조회 → (자리가 있으면) 예약 → 종료. 성공하면 두 번 시도하지 않습니다."""

    def __init__(
        self,
        client: KorailClient,
        console: _Console,
        plan: WatchPlan,
        *,
        live: bool,
    ) -> None:
        self.client = client
        self.console = console
        self.plan = plan
        self.live = live
        self._relogins = 0

    # -- 로그인 --------------------------------------------------------------

    def _login(self) -> None:
        member_no, password = read_credentials_from_env()
        self.client.login(member_no, password)
        self.console.say("로그인했습니다.")

    def _relogin(self) -> None:
        self._relogins += 1
        if self._relogins > MAX_RELOGIN:
            raise WatchAborted(
                f"세션이 {MAX_RELOGIN}번 넘게 끊겼습니다. 계정이나 서버 쪽 "
                "문제일 수 있으니 여기서 멈춥니다."
            )
        self.console.say(f"세션이 끊겨 다시 로그인합니다({self._relogins}회차).")
        self._login()

    # -- 한 바퀴 -------------------------------------------------------------

    def run(self) -> int:
        self._login()
        poll = 0
        transport_failures = 0
        while True:
            if self._out_of_time():
                self.console.say("감시 시간이 끝났습니다. 잡지 못했습니다.")
                return EXIT_NOT_FOUND
            poll += 1
            try:
                candidates = collect_candidates(self.client, self.plan)
            except KorailSessionExpiredError:
                self._relogin()
                continue
            except KorailNoResultsError as exc:
                # 직통이 없거나(WRD000061) 조건에 맞는 것이 없는 상태. 요청은
                # 정상이므로 계속 지켜봅니다.
                self.console.say(f"[{poll}] 조회 결과 없음 ({exc.code}).")
                candidates = []
            except KorailTransportError as exc:
                transport_failures += 1
                if transport_failures >= MAX_CONSECUTIVE_TRANSPORT_FAILURES:
                    raise WatchAborted(
                        f"전송이 연속 {transport_failures}번 실패했습니다: {exc}"
                    ) from exc
                self.console.say(f"[{poll}] 전송 실패({transport_failures}회 연속): {exc}")
                self._sleep()
                continue
            else:
                transport_failures = 0

            outcome = self._act_on(poll, candidates)
            if outcome is not None:
                return outcome
            self._sleep()

    def _act_on(self, poll: int, candidates: list[TrainSummary]) -> int | None:
        """이번 조회 결과로 할 수 있는 일을 합니다. 끝났으면 종료 코드."""
        open_trains = [
            train for train in candidates if is_reservable(train, self.plan.seat_class)
        ]
        self._report(poll, candidates, open_trains)
        if open_trains:
            outcome = self._try_reserve(open_trains)
            if outcome is not None:
                return outcome
        if self.plan.allow_standby:
            standby = [train for train in candidates if is_standby_available(train)]
            if standby:
                return self._try_standby(standby[0])
        return None

    def _report(
        self,
        poll: int,
        candidates: list[TrainSummary],
        open_trains: list[TrainSummary],
    ) -> None:
        if not candidates:
            return
        states = ", ".join(
            f"{train.train_no}({departure_time_of(train)[:4] or '????'}"
            f":{cabin_availability_name(train, self.plan.seat_class) or '?'})"
            for train in candidates[:6]
        )
        more = "" if len(candidates) <= 6 else f" 외 {len(candidates) - 6}편"
        self.console.say(f"[{poll}] {len(candidates)}편 감시 중 — {states}{more}")
        if open_trains:
            numbers = ", ".join(train.train_no for train in open_trains)
            self.console.say(f"    자리가 열렸습니다: {numbers}")

    # -- 예약 ----------------------------------------------------------------

    def _try_reserve(self, open_trains: list[TrainSummary]) -> int | None:
        """열린 열차를 앞에서부터 시도합니다. 하나라도 잡히면 끝입니다.

        좌석이 그 사이에 사라진 것(``KorailSeatUnavailableError``,
        ``KorailSoldOutError``)만 다음 열차로 넘어갈 이유가 됩니다. 그 밖의 앱
        오류는 이유를 모른 채 되풀이하면 안 되므로 그대로 올립니다.
        """
        for train in open_trains:
            self.console.say(f"    {train.train_no} 예약 시도")
            try:
                result = self.client.reserve(
                    train,
                    consent=reserve_consent(live=self.live),
                    passengers=self.plan.passengers,
                    seat_class=self.plan.seat_class,
                    job_type=KorailReservationJobType.IMMEDIATE,
                )
            except (KorailSeatUnavailableError, KorailSoldOutError) as exc:
                self.console.say(f"    {train.train_no} 놓쳤습니다 ({exc.code}).")
                continue
            except KorailSessionExpiredError:
                self._relogin()
                return None
            return self._settle(result, train, kind="좌석 예약")
        return None

    def _try_standby(self, train: TrainSummary) -> int | None:
        """예약대기(1102). 일반실에서만, 그리고 대기 플래그가 설 때만."""
        if self.plan.seat_class is not KorailSeatClass.GENERAL:
            self.console.say("    예약대기는 일반실에만 있습니다. 건너뜁니다.")
            return None
        self.console.say(f"    {train.train_no} 예약대기 시도")
        try:
            result = self.client.reserve(
                train,
                consent=reserve_consent(live=self.live),
                passengers=self.plan.passengers,
                seat_class=KorailSeatClass.GENERAL,
                job_type=KorailReservationJobType.STANDBY,
            )
        except (KorailSeatUnavailableError, KorailSoldOutError) as exc:
            self.console.say(f"    예약대기 실패 ({exc.code}).")
            return None
        except KorailProtocolError as exc:
            # 대기 플래그가 그 사이에 내려갔습니다. 계속 지켜봅니다.
            self.console.say(f"    예약대기 조건이 아닙니다: {exc}")
            return None
        outcome = self._settle(result, train, kind="예약대기")
        if outcome == EXIT_OK and isinstance(result, ReservationHoldResponse):
            self._confirm_standby(result)
        return outcome

    def _confirm_standby(self, hold: ReservationHoldResponse) -> None:
        """예약대기 화면을 여는 마무리 호출.

        앱은 ``IRR000014`` 를 받았을 때만 그 화면으로 넘어갑니다. 코드가 다르면
        홀드는 그대로 두고 사람에게 넘깁니다 — 잡힌 것을 잃는 것보다 낫습니다.
        """
        if hold.h_msg_cd != KORAIL_STANDBY_HOLD_MESSAGE_CODE:
            self.console.say(
                f"    대기 확인 코드가 {hold.h_msg_cd} 입니다"
                f"({KORAIL_STANDBY_HOLD_MESSAGE_CODE} 가 아님). "
                "확인 호출은 보내지 않습니다 — 앱에서 확인하세요."
            )
            return
        try:
            self.client.confirm_standby_hold(
                hold,
                consent=reserve_consent(live=self.live),
            )
        except KorailAppError as exc:
            self.console.say(f"    대기 옵션 기록 실패 ({exc.code}). 홀드는 남아 있습니다.")
            return
        self.console.say("    대기 옵션을 기록했습니다.")

    def _settle(
        self,
        result: MutationPreview | BaseKorailResponse,
        train: TrainSummary,
        *,
        kind: str,
    ) -> int:
        """예약 호출의 결과를 사람이 읽을 것으로 바꾸고 실행을 끝냅니다."""
        if isinstance(result, MutationPreview):
            self.console.banner(
                (
                    f"{kind} 가능 — 하지만 아무것도 보내지 않았습니다(dry-run).",
                    f"열차 {train.train_no}, {train.departure_station_name}"
                    f"→{train.arrival_station_name}, "
                    f"{train.departure_date} {departure_time_of(train)}",
                    f"보낼 곳: {result.route}",
                    f"폼 항목 {len(result.payload)}개 (마스킹된 미리보기)",
                    "",
                    "진짜로 잡으려면: KORAIL_LIVE_MUTATION=1 과 --reserve",
                )
            )
            return EXIT_OK
        hold = result if isinstance(result, ReservationHoldResponse) else None
        pnr = (hold.pnr_no if hold else None) or "(응답에 PNR 이 없습니다)"
        self.console.banner(
            (
                f"{kind} 성공. 아직 결제 전입니다.",
                f"PNR: {pnr}",
                f"열차 {train.train_no}, {train.departure_station_name}"
                f"→{train.arrival_station_name}, "
                f"{train.departure_date} {departure_time_of(train)}",
                f"금액: {self._fare_text(hold)}",
                f"결제 기한: {self._deadline_text(hold)}",
                "",
                "결제는 이 프로그램이 하지 않습니다. 코레일 앱이나 홈페이지에서",
                "기한 안에 결제하세요. 취소도 앱에서 하면 됩니다.",
            )
        )
        return EXIT_OK

    @staticmethod
    def _fare_text(hold: ReservationHoldResponse | None) -> str:
        if hold is None:
            return "알 수 없음"
        amount = hold.received_amount or hold.total_fare or hold.total_price
        if not amount:
            return "알 수 없음"
        try:
            return f"{int(amount):,}원"
        except ValueError:
            return amount

    @staticmethod
    def _deadline_text(hold: ReservationHoldResponse | None) -> str:
        """서버가 준 기한만 씁니다. 지어내지 않습니다."""
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

    # -- 시간 ----------------------------------------------------------------

    def _out_of_time(self) -> bool:
        return self.plan.deadline is not None and time.monotonic() >= self.plan.deadline

    def _sleep(self) -> None:
        base = self.plan.poll_interval_s
        delay = base * (1.0 + random.uniform(-POLL_JITTER, POLL_JITTER))
        if self.plan.deadline is not None:
            remaining = self.plan.deadline - time.monotonic()
            if remaining <= 0:
                return
            delay = min(delay, remaining)
        time.sleep(delay)


# --- 입력 ---------------------------------------------------------------------


def parse_clock(text: str, *, flag: str) -> str:
    """``"08:00"``/``"0800"``/``"080000"`` 을 ``HHMMSS`` 로. 빈 값은 빈 값."""
    raw = text.strip().replace(":", "")
    if not raw:
        return ""
    if not raw.isdigit() or len(raw) not in (4, 6):
        raise WatchAborted(f"{flag} 는 HH:MM 이나 HHMMSS 여야 합니다: {text!r}")
    padded = raw if len(raw) == 6 else raw + "00"
    if int(padded[:2]) > 23 or int(padded[2:4]) > 59 or int(padded[4:]) > 59:
        raise WatchAborted(f"{flag} 가 시각이 아닙니다: {text!r}")
    return padded


def passengers_from_args(args: argparse.Namespace) -> KorailPassengerCounts:
    try:
        return KorailPassengerCounts(
            adult=args.adult,
            teenager=args.teenager,
            child=args.child,
            infant=args.infant,
            senior=args.senior,
        )
    except ValueError as exc:
        raise WatchAborted(f"승객 인원이 잘못됐습니다: {exc}") from exc


def build_config() -> tuple[KorailConfig, str]:
    """기기 신원을 정합니다. 셋 다 있으면 실기기 값, 셋 다 없으면 합성 값."""
    values = {
        name: os.environ.get(name, "")
        for name in (DEVICE_ID_ENV, OS_VERSION_ENV, DEVICE_MODEL_ENV)
    }
    present = [name for name, value in values.items() if value]
    if len(present) == len(values):
        return build_config_from_env(), "환경변수의 실기기 값"
    if present:
        missing = ", ".join(name for name, value in values.items() if not value)
        raise WatchAborted(
            f"기기 값은 셋을 다 주거나 다 비워야 합니다. 빠진 것: {missing}"
        )
    return KorailConfig(enable_dynapath=True), "합성 기기 값"


def build_plan(args: argparse.Namespace) -> WatchPlan:
    depart_after = parse_clock(args.after, flag="--after")
    depart_before = parse_clock(args.before, flag="--before")
    if depart_after and depart_before and depart_after > depart_before:
        raise WatchAborted("--after 가 --before 보다 늦습니다")
    if len(args.date) != 8 or not args.date.isdigit():
        raise WatchAborted("--date 는 8자리 YYYYMMDD 여야 합니다")
    if args.date < time.strftime("%Y%m%d"):
        raise WatchAborted(f"--date {args.date} 는 지난 날짜입니다")
    if args.interval < MIN_POLL_INTERVAL_S:
        raise WatchAborted(
            f"--interval 은 {MIN_POLL_INTERVAL_S:g}초 아래로 내릴 수 없습니다. "
            "KORAIL 은 매크로성 트래픽에 IP 를 막습니다."
        )
    if args.min_interval < 1.0:
        raise WatchAborted("--min-interval 이 1.0초 미만이면 IP 차단 위험이 큽니다")
    if args.minutes < 0:
        raise WatchAborted("--minutes 는 음수일 수 없습니다")
    passengers = passengers_from_args(args)
    seat_class = (
        KorailSeatClass.SPECIAL if args.seat_class == "special" else KorailSeatClass.GENERAL
    )
    query = TrainSearchQuery(
        departure_station_code=args.departure,
        arrival_station_code=args.arrival,
        departure_date=args.date,
        # 검색은 이 시각 "이후" 를 줍니다. 시간창의 시작을 그대로 쓰면 필요 없는
        # 앞쪽 페이지를 넘기지 않아도 됩니다.
        departure_time=depart_after or "000000",
        passengers=passengers.total,
    )
    return WatchPlan(
        query=query,
        passengers=passengers,
        seat_class=seat_class,
        train_numbers=frozenset(
            normalize_train_no(value) for value in args.train_no if value.strip()
        ),
        depart_after=depart_after,
        depart_before=depart_before,
        train_name=args.train_name.strip(),
        allow_standby=args.standby,
        poll_interval_s=args.interval,
        max_pages=args.max_pages,
        deadline=(
            None if args.minutes == 0 else time.monotonic() + args.minutes * 60.0
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "만석인 KORAIL 열차를 지켜보다가 자리가 열리면 한 번만 잡습니다. "
            "결제는 하지 않습니다."
        )
    )
    parser.add_argument("--from", dest="departure", required=True, help="출발역 이름 또는 코드")
    parser.add_argument("--to", dest="arrival", required=True, help="도착역 이름 또는 코드")
    parser.add_argument("--date", required=True, help="출발일 YYYYMMDD")
    parser.add_argument("--after", default="", help="이 시각 이후 출발 (HH:MM)")
    parser.add_argument("--before", default="", help="이 시각 이전 출발 (HH:MM)")
    parser.add_argument(
        "--train-no",
        action="append",
        default=[],
        help="열차번호를 못 박습니다. 여러 번 줄 수 있습니다",
    )
    parser.add_argument("--train-name", default="", help="열차종별 부분일치 (예: KTX)")
    parser.add_argument(
        "--seat-class",
        choices=("general", "special"),
        default="general",
        help="일반실(general) 또는 특실(special)",
    )
    parser.add_argument("--adult", type=int, default=1, help="어른 인원 (기본 1)")
    parser.add_argument("--teenager", type=int, default=0, help="청소년 인원")
    parser.add_argument("--child", type=int, default=0, help="어린이 인원")
    parser.add_argument("--infant", type=int, default=0, help="유아 인원")
    parser.add_argument("--senior", type=int, default=0, help="경로 인원")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_S,
        help=f"조회 주기(초). 하한 {MIN_POLL_INTERVAL_S:g}초",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=DEFAULT_MIN_INTERVAL_S,
        help="요청 사이 최소 간격(초)",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=DEFAULT_WATCH_MINUTES,
        help=f"몇 분 동안 지켜볼지. 0 이면 무제한 (기본 {DEFAULT_WATCH_MINUTES})",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"조회 한 번에 넘겨 볼 페이지 수 (기본 {DEFAULT_MAX_PAGES})",
    )
    parser.add_argument(
        "--standby",
        action="store_true",
        help="좌석이 없으면 예약대기(일반실 전용)도 시도합니다",
    )
    parser.add_argument(
        "--reserve",
        action="store_true",
        help=(
            "진짜로 홀드를 만듭니다. KORAIL_MOBILE_API_LIVE=1 과 "
            f"{LIVE_MUTATION_ENV}=1 이 함께 있어야 합니다"
        ),
    )
    return parser


def require_opt_ins(*, live: bool) -> None:
    if not live_enabled():
        raise WatchAborted("KORAIL_MOBILE_API_LIVE=1 이 있어야 실서버에 붙습니다")
    if live and os.environ.get(LIVE_MUTATION_ENV) != "1":
        raise WatchAborted(
            f"{LIVE_MUTATION_ENV}=1 을 세워야 진짜 예약을 만듭니다. "
            "없으면 조회와 dry-run 까지만 합니다."
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = _Console()
    client: KorailClient | None = None
    try:
        live = bool(args.reserve)
        require_opt_ins(live=live)
        plan = build_plan(args)
        config, identity = build_config()
        console.banner(
            (
                (
                    "이 실행은 진짜 예약(결제 전 홀드)을 만들 수 있습니다."
                    if live
                    else "이 실행은 조회만 합니다. 예약 요청은 나가지 않습니다."
                ),
                f"{args.departure}→{args.arrival} {args.date}"
                f"{' ' + plan.depart_after[:4] if plan.depart_after else ''}"
                f"{'~' + plan.depart_before[:4] if plan.depart_before else ''}",
                f"승객 {plan.passengers.total}명, "
                f"{'특실' if plan.seat_class is KorailSeatClass.SPECIAL else '일반실'}"
                f"{', 예약대기 허용' if plan.allow_standby else ''}",
                f"{plan.poll_interval_s:g}초마다 조회, "
                f"{'무제한' if plan.deadline is None else str(args.minutes) + '분'} 동안",
                f"기기 신원: {identity}",
                "결제는 어떤 경우에도 하지 않습니다. 멈추려면 Ctrl-C.",
            )
        )
        client = KorailClient(config)
        _install_pacing(client, _Pacer(args.min_interval))
        return Watcher(client, console, plan, live=live).run()
    except WatchAborted as exc:
        console.say(f"멈춥니다: {exc}")
        return EXIT_ABORTED
    except KeyboardInterrupt:
        console.say("중단했습니다. 만들어진 홀드가 있다면 위에 PNR 이 찍혀 있습니다.")
        return EXIT_ABORTED
    except KorailDynaPathError as exc:
        console.say(
            f"DynaPath 가 이 클라이언트를 막았습니다({exc}). 스로틀이 아니라 "
            "자동화로 표시된 것이니, 다시 돌리기 전에 멈추는 게 낫습니다."
        )
        return EXIT_FAILED
    except KorailAppUpdateRequiredError as exc:
        console.say(
            f"서버가 앱 업데이트를 요구합니다({exc.code}). 로그인만 막힌다면 "
            "버전이 아니라 MACRO ERROR 일 수 있습니다."
        )
        return EXIT_FAILED
    except KorailReservationRefusedError as exc:
        console.say(f"예약이 거절됐습니다({exc.code}): {exc.message}")
        return EXIT_FAILED
    except KorailAuthError as exc:
        console.say(f"로그인에 실패했습니다: {exc}")
        return EXIT_FAILED
    except KorailAppError as exc:
        console.say(f"서버가 실패로 답했습니다({exc.code}): {exc.message}")
        return EXIT_FAILED
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
