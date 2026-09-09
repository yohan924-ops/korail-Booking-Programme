"""조회 한 번을 여정 목록으로 바꿉니다 — 직통, 서버 환승, 직접 조합 환승.

:class:`SearchRequest` 하나가 화면의 조회 조건 전부를 담고,
:func:`search_journeys` 가 그것을 :class:`~korail_booker.journeys.Journey` 목록으로
바꿉니다. 클라이언트는 인자로 받으므로 이 모듈은 세션도 설정도 쥐지 않습니다.

환승에는 두 길이 있고, 둘의 근거가 다릅니다.

``TRANSFER_SERVER``
    ``search_transfer_trains`` 가 짝지어 준 여정. 앱이 하는 그대로이고,
    ``reserve_transfer`` 로 한 PNR 에 잡히는 것이 실서버에서 확인됐습니다
    (2026-07-31 서울→오송→여수EXPO).

``TRANSFER_CUSTOM``
    사용자가 환승역을 지정하면 이 프로그램이 두 구간을 **각각 직통으로 조회해**
    붙입니다. 서버에 "이 역에서 갈아타는 여정을 달라"고 물을 방법이 없기
    때문입니다 — 환승 검색 폼에는 환승역 필드가 없습니다. 예약 폼은 만들어지고
    라이브러리도 막지 않지만, 앱이 스스로 짝지은 적 없는 조합을 서버가
    받아들이는지는 **확인된 바 없습니다.** 화면이 그렇게 표시합니다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, TypeVar

from korail_mobile_api import (
    KorailApiError,
    KorailAuthError,
    KorailClient,
    KorailDynaPathError,
    KorailNoResultsError,
    KorailPassengerCounts,
    TrainSearchQuery,
    TrainSearchResult,
    TrainSummary,
    TransferSearchResult,
)

from .journeys import (
    Journey,
    JourneySource,
    SeatPreference,
    clock_to_minutes,
    elapsed_minutes,
    normalize_clock,
)


#: 조회 한 번에 물어볼 횟수의 상한. 한 번마다 요청이 하나 나갑니다.
#:
#: 2 였습니다. 그래서 동탄→대구(2026-09-12)를 00:00~23:30 으로 조회했을 때
#: 13:08 출발까지 열 편만 나오고 그 뒤가 통째로 빠졌습니다 — 앱에는 22:00 까지
#: 스물넷이 나오는 구간입니다. 서버가 첫 페이지에서 "다음 있음"(``h_next_pg_flg``)
#: 을 주지 않으면 커서로는 더 갈 수 없고, 예전에는 거기서 끝냈습니다.
#: :func:`_walk` 가 그때 **시각을 밀어** 다시 묻습니다.
DEFAULT_MAX_PAGES = 8
#: 직접 조합 환승은 역마다 조회가 둘입니다. 여기까지 8번씩 물으면 역 셋에
#: 열여섯 요청이 됩니다. 어차피 :data:`MAX_CUSTOM_LEGS_PER_SIDE` 로 자르므로
#: 앞쪽만 봅니다.
CUSTOM_LEG_MAX_PAGES = 2
#: 직접 조합 환승에서 한 구간당 들고 갈 후보 수. 조합이 곱으로 늘기 때문에
#: 자릅니다.
MAX_CUSTOM_LEGS_PER_SIDE = 12
#: 직접 조합 환승이 만들어 낼 여정 수의 상한.
MAX_CUSTOM_JOURNEYS = 40

TransferMode = str
TRANSFER_SERVER: TransferMode = "server"
TRANSFER_CUSTOM: TransferMode = "custom"

Logger = Callable[[str], None]


@dataclass(frozen=True)
class SearchRequest:
    """화면의 조회 조건 하나. 그대로 자동예매에도 다시 쓰입니다."""

    departure: str
    arrival: str
    #: ``YYYYMMDD``.
    date: str
    #: ``HHMMSS``. 빈 문자열이면 그쪽 끝을 제한하지 않습니다.
    depart_after: str = ""
    depart_before: str = ""
    #: ``h_trn_clsf_nm`` 부분일치 목록(예: ``("KTX", "무궁화")``). 여러 개를
    #: 고르면 그중 **하나라도** 맞으면 통과입니다. 환승이면 **모든 구간**이
    #: 그 조건을 넘어야 합니다. 비어 있으면 전부.
    train_names: tuple[str, ...] = ()
    seat_preference: SeatPreference = SeatPreference.ANY
    include_direct: bool = True
    include_transfer: bool = False
    transfer_mode: TransferMode = TRANSFER_SERVER
    #: 환승역 이름들. ``TRANSFER_SERVER`` 에서는 결과를 거르는 필터이고,
    #: ``TRANSFER_CUSTOM`` 에서는 조회할 역 그 자체입니다.
    transfer_stations: tuple[str, ...] = ()
    min_transfer_minutes: int = 0
    max_transfer_minutes: int = 0  # 0 이면 위쪽 제한 없음
    passengers: KorailPassengerCounts = field(default_factory=KorailPassengerCounts)
    max_pages: int = DEFAULT_MAX_PAGES

    def query(
        self,
        departure: str | None = None,
        arrival: str | None = None,
        *,
        departure_time: str | None = None,
    ) -> TrainSearchQuery:
        """이 조건으로 만드는 검색 질의. 구간과 시작 시각만 갈아 끼웁니다."""
        return TrainSearchQuery(
            departure_station_code=departure or self.departure,
            arrival_station_code=arrival or self.arrival,
            departure_date=self.date,
            departure_time=departure_time or self.depart_after or "000000",
            passengers=self.passengers.total,
        )


def return_request(
    outbound: SearchRequest,
    *,
    date: str,
    depart_after: str = "",
    depart_before: str = "",
) -> SearchRequest:
    """오는 편 조회 조건. 구간을 뒤집고 날짜와 **자기 시간대**를 붙입니다.

    가는 편의 시간대를 물려받지 않습니다 — 아침에 가서 저녁에 오는 것이 보통인데
    같은 시간창을 쓰면 오는 편이 통째로 걸러집니다.

    환승역도 비웁니다. 환승역 후보는 구간마다 다르므로, 가는 편에서 고른 역을
    그대로 물리면 있지도 않은 역으로 거르게 됩니다.
    """
    if date < outbound.date:
        raise ValueError("오는 날이 가는 날보다 빠릅니다")
    return replace(
        outbound,
        departure=outbound.arrival,
        arrival=outbound.departure,
        date=date,
        depart_after=depart_after,
        depart_before=depart_before,
        transfer_stations=(),
    )


def station_code_index(client: KorailClient) -> dict[str, str]:
    """역 이름 → 역 코드. 환승역 조회가 이름이 아니라 코드를 받습니다."""
    return {
        station.name: station.code
        for station in client.get_station_data().stations
        if station.name and station.code
    }


def filter_station_names(
    names: Iterable[str],
    query: str,
    *,
    limit: int = 30,
) -> list[str]:
    """친 글자로 역 이름을 좁힙니다. 앞에서 맞는 것을 먼저 놓습니다.

    ``"동"`` 이면 ``동대구`` 가 ``광주송정`` 보다 먼저 옵니다 — 사람은 대개
    이름의 앞을 치기 때문입니다. 빈 질의는 전부를 그대로 돌려줍니다.
    """
    needle = query.strip().casefold()
    if not needle:
        return list(names)[:limit]
    starts: list[str] = []
    contains: list[str] = []
    for name in names:
        folded = name.casefold()
        if folded.startswith(needle):
            starts.append(name)
        elif needle in folded:
            contains.append(name)
    return (starts + contains)[:limit]


def resolve_station_code(
    reference: str,
    index: Mapping[str, str],
) -> str | None:
    """이름이면 코드로 바꾸고, 이미 코드면 그대로. 모르는 역이면 ``None``."""
    value = reference.strip()
    if not value:
        return None
    if value.isdigit():
        return value
    return index.get(value)


def transfer_station_candidates(
    client: KorailClient,
    departure: str,
    arrival: str,
    *,
    index: Mapping[str, str] | None = None,
) -> list[str]:
    """이 구간에서 갈아탈 수 있는 역 이름들.

    ``qry.chtnStn.do`` 가 구간마다 답해 주는 목록입니다. 전국 역을 다 보여
    주는 것과 다릅니다 — 여기 없는 역은 이 구간의 환승역이 아닙니다.

    빈 목록은 오류가 아니라 "이 구간에는 환승역이 없다"는 답입니다.
    """
    resolved = index if index is not None else station_code_index(client)
    departure_code = resolve_station_code(departure, resolved)
    arrival_code = resolve_station_code(arrival, resolved)
    if departure_code is None or arrival_code is None:
        raise ValueError("출발역이나 도착역의 코드를 찾지 못했습니다")
    response = client.get_transfer_stations(departure_code, arrival_code)
    names = [
        station.station_name.strip()
        for station in response.stations
        if station.station_name and station.station_name.strip()
    ]
    return list(dict.fromkeys(names))


def _matches_window(journey: Journey, request: SearchRequest) -> bool:
    departure = journey.departure_clock
    if not departure:
        return True
    after = normalize_clock(request.depart_after)
    before = normalize_clock(request.depart_before)
    if after and departure < after:
        return False
    return not (before and departure > before)


def _matches_train_name(journey: Journey, request: SearchRequest) -> bool:
    wanted = tuple(
        name.strip().casefold() for name in request.train_names if name.strip()
    )
    if not wanted:
        return True
    return all(
        any(pattern in name.casefold() for pattern in wanted)
        for name in journey.train_names()
    )


def _matches_transfer(journey: Journey, request: SearchRequest) -> bool:
    if not journey.is_transfer:
        return True
    minutes = journey.transfer_minutes
    if minutes is None:
        return False
    if minutes < request.min_transfer_minutes:
        return False
    if request.max_transfer_minutes and minutes > request.max_transfer_minutes:
        return False
    wanted = tuple(name.strip() for name in request.transfer_stations if name.strip())
    if wanted and request.transfer_mode == TRANSFER_SERVER:
        station = journey.transfer_station_name
        if station is None or station not in wanted:
            return False
    return True


def accepts(journey: Journey, request: SearchRequest) -> bool:
    """조회 조건에 맞는 여정인지. 좌석 상태는 보지 않습니다 — 만석도 보여 줍니다."""
    return (
        _matches_window(journey, request)
        and _matches_train_name(journey, request)
        and _matches_transfer(journey, request)
    )


def rejection_lines(
    journeys: Iterable[Journey],
    request: SearchRequest,
    *,
    samples: int = 3,
) -> list[str]:
    """무엇이 몇 편을 걸러 냈는지, 그리고 걸러진 열차 몇 개의 실물.

    "조건이 전부 걸러 냈습니다" 만으로는 어느 칸을 고쳐야 할지 알 수 없습니다.
    조건별로 세고, 실제로 걸러진 열차를 몇 개 보여 줍니다 — 서버가 준 열차의
    출발 시각과 종별을 보면 대개 어느 조건이 문제인지 바로 보입니다.
    """
    rejected: list[tuple[Journey, list[str]]] = []
    counts = {"시간대": 0, "열차 종류": 0, "환승 조건": 0}
    for journey in journeys:
        failed: list[str] = []
        if not _matches_window(journey, request):
            failed.append("시간대")
        if not _matches_train_name(journey, request):
            failed.append("열차 종류")
        if not _matches_transfer(journey, request):
            failed.append("환승 조건")
        if failed:
            rejected.append((journey, failed))
            for name in failed:
                counts[name] += 1
    if not rejected:
        return []
    tally = ", ".join(f"{name} {count}편" for name, count in counts.items() if count)
    lines = [f"걸러진 이유: {tally}"]
    for journey, failed in rejected[:samples]:
        names = " ".join(dict.fromkeys(name for name in journey.train_names() if name))
        numbers = "+".join(journey.train_numbers())
        lines.append(
            f"  · {names} {numbers} "
            f"{journey.departure_clock[:2]}:{journey.departure_clock[2:4]}"
            f" 출발 — {', '.join(failed)} 때문에 빠짐"
        )
    if len(rejected) > samples:
        lines.append(f"  · 그 밖 {len(rejected) - samples}편")
    return lines


class _Paged(Protocol):
    """페이지를 가진 검색 결과. 직통과 환승이 커서 모양만 다르고 이것은 같습니다."""

    def next_page(self) -> Any: ...


ResultT = TypeVar("ResultT", bound=_Paged)


def _walk(
    fetch: Callable[[TrainSearchQuery, Any], ResultT],
    query: TrainSearchQuery,
    *,
    max_pages: int,
    last_clock: Callable[[ResultT], str | None],
    on_empty: Callable[[KorailNoResultsError, TrainSearchQuery], None] | None = None,
) -> Iterator[ResultT]:
    """검색 결과를 끝까지 훑습니다. **두 가지 방법으로** 이어 갑니다.

    1. 서버가 준 커서(``h_next_pg_flg`` 가 ``"Y"`` 일 때만 나옵니다). 앱과 같은
       게이트입니다.
    2. 커서가 없으면 **마지막 행의 출발 시각부터 새로 조회**합니다. 사람이
       "이 시각 이후"를 다시 묻는 것과 같은 요청이고, 새 규약을 가정하지
       않습니다.

    2번이 필요한 이유: 동탄→대구(2026-09-12)를 00:00~23:30 으로 조회하니
    13:08 출발까지 열 편만 오고 그 뒤가 통째로 빠졌습니다. 그 응답의
    ``h_next_pg_flg`` 가 ``"Y"`` 가 아니어서 커서로는 갈 데가 없었고, 그러면
    "더 없다" 로 끝내는 수밖에 없었습니다 — 앱에는 22:00 까지 나오는데도.

    **왜 그 응답에 커서가 없었는지는 확인하지 못했습니다.** 여기서 하는 일은
    원인을 고치는 것이 아니라, 같은 질문을 시각만 바꿔 다시 던지는 것입니다.

    시각이 나아가지 않으면 멈춥니다(같은 시각을 다시 묻지 않습니다). 그래서
    최악이라도 요청은 ``max_pages`` 번입니다. 겹쳐 오는 행은 부르는 쪽의
    :func:`deduplicate` 가 걷어냅니다.
    """
    continuation = None
    current = query
    asked: set[str] = {query.departure_time}
    for _ in range(max(1, max_pages)):
        try:
            result = fetch(current, continuation)
        except KorailNoResultsError as exc:
            # 첫 물음이 비면 답이 없는 것이고, 이어 가다 비면 끝에 닿은
            # 것입니다. 사람에게 알릴 것은 앞쪽뿐입니다.
            if on_empty is not None and current is query and continuation is None:
                on_empty(exc, current)
            return
        yield result
        continuation = result.next_page()
        if continuation is not None:
            continue
        clock = last_clock(result)
        if clock is None or clock in asked:
            return
        asked.add(clock)
        # 마지막 행의 시각을 **그대로** 씁니다. 1분을 더하면 같은 분에 떠나는
        # 다른 여정을 건너뜁니다 — 실제로 그런 줄이 옵니다(같은 열차 309 가
        # 뒤 구간만 다르게 두 번).
        current = replace(query, departure_time=clock)


def _latest_departure(clocks: Iterable[str | None]) -> str | None:
    """이 페이지에서 가장 늦은 출발 시각. 없으면 ``None``."""
    valid = [normalize_clock(clock) for clock in clocks if clock]
    usable = [clock for clock in valid if clock and clock.isdigit()]
    return max(usable) if usable else None


def _direct_pages(
    client: KorailClient,
    query: TrainSearchQuery,
    *,
    max_pages: int,
    log: Logger | None = None,
) -> Iterator[TrainSearchResult]:
    """직통 검색 결과 페이지. 결과 없음은 빈 흐름입니다."""

    def on_empty(exc: KorailNoResultsError, asked: TrainSearchQuery) -> None:
        # 직통 없음(``WRD000061``)과 결과 없음은 실패가 아니라 답입니다.
        # 다만 조용히 비면 사람은 프로그램이 고장 난 줄 압니다 — 서버가
        # 뭐라고 답했는지 남깁니다.
        if log:
            log(
                f"직통 조회에 결과가 없습니다 (서버 코드 {exc.code}): "
                f"{asked.departure_station_code}→{asked.arrival_station_code} "
                f"{asked.departure_date} {asked.departure_time} 이후"
            )

    return _walk(
        lambda asked, cursor: client.search_trains(asked, continuation=cursor),
        query,
        max_pages=max_pages,
        last_clock=lambda result: _latest_departure(
            train.departure_time for train in result.trains
        ),
        on_empty=on_empty,
    )


def _transfer_pages(
    client: KorailClient,
    query: TrainSearchQuery,
    *,
    max_pages: int,
    log: Logger | None = None,
) -> Iterator[TransferSearchResult]:
    """환승 검색 결과 페이지. 커서가 직통과 다르므로 따로 돕니다."""

    def on_empty(exc: KorailNoResultsError, _asked: TrainSearchQuery) -> None:
        if log:
            log(f"환승 조회에 결과가 없습니다 (서버 코드 {exc.code})")

    return _walk(
        lambda asked, cursor: client.search_transfer_trains(asked, continuation=cursor),
        query,
        max_pages=max_pages,
        # 여정의 시각은 **첫 구간** 것입니다. 다음 조회의 시작점도 그것이어야
        # 합니다 — 뒤 구간 시각으로 밀면 그 사이 여정을 건너뜁니다.
        last_clock=lambda result: _latest_departure(
            itinerary.legs[0].departure_time
            for itinerary in result.itineraries
            if itinerary.legs
        ),
        on_empty=on_empty,
    )


def search_direct(
    client: KorailClient,
    request: SearchRequest,
    *,
    log: Logger | None = None,
) -> list[Journey]:
    journeys: list[Journey] = []
    for result in _direct_pages(
        client, request.query(), max_pages=request.max_pages, log=log
    ):
        journeys.extend(
            Journey(legs=(train,), source=JourneySource.DIRECT)
            for train in result.trains
        )
    return journeys


def search_server_transfer(
    client: KorailClient,
    request: SearchRequest,
    *,
    log: Logger | None = None,
) -> list[Journey]:
    journeys: list[Journey] = []
    for result in _transfer_pages(
        client, request.query(), max_pages=request.max_pages, log=log
    ):
        journeys.extend(
            Journey(
                legs=itinerary.legs,
                source=JourneySource.SERVER_TRANSFER,
            )
            for itinerary in result.itineraries
        )
    return journeys


def _first_leg_candidates(
    client: KorailClient,
    request: SearchRequest,
    station: str,
) -> list[TrainSummary]:
    trains: list[TrainSummary] = []
    for result in _direct_pages(
        client, request.query(arrival=station), max_pages=CUSTOM_LEG_MAX_PAGES
    ):
        trains.extend(result.trains)
    return trains[:MAX_CUSTOM_LEGS_PER_SIDE]


def _second_leg_candidates(
    client: KorailClient,
    request: SearchRequest,
    station: str,
    earliest_arrival: str,
) -> list[TrainSummary]:
    trains: list[TrainSummary] = []
    for result in _direct_pages(
        client,
        request.query(departure=station, departure_time=earliest_arrival),
        max_pages=CUSTOM_LEG_MAX_PAGES,
    ):
        trains.extend(result.trains)
    return trains[:MAX_CUSTOM_LEGS_PER_SIDE]


def search_custom_transfer(
    client: KorailClient,
    request: SearchRequest,
    *,
    log: Logger | None = None,
) -> list[Journey]:
    """환승역을 사용자가 지정한 조합. 서버가 짝지어 준 것이 아닙니다.

    역마다 두 번 조회합니다 — 출발역→환승역, 환승역→도착역. 두 번째 조회는
    첫 구간의 가장 이른 도착 시각부터 묻습니다.
    """
    journeys: list[Journey] = []
    for station in request.transfer_stations:
        name = station.strip()
        if not name or name in (request.departure, request.arrival):
            continue
        try:
            first_legs = _first_leg_candidates(client, request, name)
        except KorailApiError as exc:
            if log:
                log(f"{name} 경유 조회 실패({type(exc).__name__}): {exc}")
            continue
        if not first_legs:
            if log:
                log(f"{name} 경유: 첫 구간이 없습니다")
            continue
        arrivals = [
            normalize_clock(train.arrival_time)
            for train in first_legs
            if normalize_clock(train.arrival_time)
        ]
        earliest = min(arrivals) if arrivals else request.depart_after or "000000"
        second_legs = _second_leg_candidates(client, request, name, earliest)
        if not second_legs:
            if log:
                log(f"{name} 경유: 두 번째 구간이 없습니다")
            continue
        for first in first_legs:
            for second in second_legs:
                wait = elapsed_minutes(first.arrival_time, second.departure_time)
                if wait is None:
                    continue
                # 하루를 넘겨 붙는 조합은 환승이 아닙니다. 자정을 넘긴 값은
                # elapsed_minutes 가 +24시간으로 돌려주므로 여기서 자릅니다.
                if wait >= 12 * 60:
                    continue
                journeys.append(
                    Journey(
                        legs=(first, second),
                        source=JourneySource.CUSTOM_TRANSFER,
                    )
                )
    return journeys


def sort_key(journey: Journey) -> tuple[int, int, str]:
    """출발 시각, 그 다음 소요 시간, 그 다음 열차번호."""
    departure = clock_to_minutes(journey.departure_clock)
    total = journey.total_minutes
    return (
        departure if departure is not None else 24 * 60,
        total if total is not None else 24 * 60,
        "+".join(journey.train_numbers()),
    )


def deduplicate(journeys: Iterable[Journey]) -> list[Journey]:
    """같은 여정이 두 경로로 들어왔을 때 앞의 것을 남깁니다.

    서버 환승과 직접 조합이 같은 두 구간을 낼 수 있습니다. 그때 남는 것은
    먼저 온 쪽 — 호출자가 검증된 것을 앞에 놓습니다.
    """
    seen: set[tuple[tuple[str, str, str, str], ...]] = set()
    unique: list[Journey] = []
    for journey in journeys:
        key = journey.key()
        if key in seen:
            continue
        seen.add(key)
        unique.append(journey)
    return unique


def _isolated(
    label: str,
    produce: Callable[[], list[Journey]],
    log: Logger | None,
) -> list[Journey]:
    """한 갈래가 실패해도 다른 갈래의 결과를 버리지 않습니다.

    직통과 환승은 별개의 조회입니다. 예전에는 둘이 한 덩어리라 환승 쪽에서
    예외가 나면(짝이 어긋난 환승 응답, 서버 오류, 알 수 없는 환승역) 이미 받아
    둔 직통 목록까지 통째로 사라졌습니다 — 화면에서는 "환승 조건을 건드리니
    직통이 안 나온다"로 보입니다.
    """
    try:
        return produce()
    except (KorailAuthError, KorailDynaPathError):
        # 이 둘은 삼키면 안 됩니다. 세션이 끊긴 것은 위에서 다시 로그인해야 하고
        # (자동예매가 그렇게 이어 갑니다), DynaPath 거절은 자동화로 표시됐다는
        # 뜻이라 되풀이할수록 나빠집니다.
        raise
    except (KorailApiError, ValueError) as exc:
        if log:
            log(
                f"{label} 조회가 실패했습니다({type(exc).__name__}): {exc} "
                "— 나머지 결과는 그대로 보여 줍니다."
            )
        return []


def search_journeys(
    client: KorailClient,
    request: SearchRequest,
    *,
    log: Logger | None = None,
) -> list[Journey]:
    """조회 조건 하나를 화면에 그릴 여정 목록으로.

    검증된 것을 먼저 담습니다 — 직통, 서버 환승, 그다음 직접 조합. 중복은 앞의
    것이 남으므로 같은 두 구간이 양쪽에서 나오면 서버가 짝지은 쪽이 이깁니다.
    """
    journeys: list[Journey] = []
    if request.include_direct:
        found = _isolated("직통", lambda: search_direct(client, request, log=log), log)
        journeys.extend(found)
        if log:
            log(f"직통 {len(found)}편")
    if request.include_transfer:
        if request.transfer_mode == TRANSFER_CUSTOM:
            found = _isolated(
                "환승(직접 지정)",
                lambda: search_custom_transfer(client, request, log=log)[
                    :MAX_CUSTOM_JOURNEYS
                ],
                log,
            )
        else:
            found = _isolated(
                "환승",
                lambda: search_server_transfer(client, request, log=log),
                log,
            )
        journeys.extend(found)
        if log:
            log(f"환승 {len(found)}편")
    unique = deduplicate(journeys)
    kept = [journey for journey in unique if accepts(journey, request)]
    if log and unique and len(kept) < len(unique):
        # 서버는 열차를 줬는데 화면에 덜 나오는 경우입니다. 어느 조건이 몇 편을
        # 걸렀는지 말해 주지 않으면 어느 칸을 고쳐야 할지 알 수 없습니다.
        log(f"서버가 준 {len(unique)}편 중 {len(kept)}편이 조건에 맞습니다.")
        for line in rejection_lines(unique, request):
            log(line)
    kept.sort(key=sort_key)
    return kept
