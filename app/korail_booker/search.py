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
from dataclasses import dataclass, field

from korail_mobile_api import (
    KorailClient,
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


#: 조회 한 번에 넘겨 볼 페이지 수의 기본값. 페이지마다 요청이 하나 더 나갑니다.
DEFAULT_MAX_PAGES = 2
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
    #: ``h_trn_clsf_nm`` 부분일치(예: ``"KTX"``). 환승이면 **모든 구간**에
    #: 적용됩니다. 빈 문자열이면 전부.
    train_name: str = ""
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


def station_code_index(client: KorailClient) -> dict[str, str]:
    """역 이름 → 역 코드. 환승역 조회가 이름이 아니라 코드를 받습니다."""
    return {
        station.name: station.code
        for station in client.get_station_data().stations
        if station.name and station.code
    }


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
    wanted = request.train_name.strip().casefold()
    if not wanted:
        return True
    return all(wanted in name.casefold() for name in journey.train_names())


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


def _direct_pages(
    client: KorailClient,
    query: TrainSearchQuery,
    *,
    max_pages: int,
    log: Logger | None = None,
) -> Iterator[TrainSearchResult]:
    """직통 검색 결과 페이지. 결과 없음은 빈 흐름입니다."""
    continuation = None
    for _ in range(max(1, max_pages)):
        try:
            result = client.search_trains(query, continuation=continuation)
        except KorailNoResultsError as exc:
            # 직통 없음(``WRD000061``)과 결과 없음은 실패가 아니라 답입니다.
            # 다만 조용히 비면 사람은 프로그램이 고장 난 줄 압니다 — 서버가
            # 뭐라고 답했는지 남깁니다.
            if log:
                log(
                    f"직통 조회에 결과가 없습니다 (서버 코드 {exc.code}): "
                    f"{query.departure_station_code}→{query.arrival_station_code} "
                    f"{query.departure_date} {query.departure_time} 이후"
                )
            return
        yield result
        continuation = result.next_page()
        if continuation is None:
            return


def _transfer_pages(
    client: KorailClient,
    query: TrainSearchQuery,
    *,
    max_pages: int,
    log: Logger | None = None,
) -> Iterator[TransferSearchResult]:
    """환승 검색 결과 페이지. 커서가 직통과 다르므로 따로 돕니다."""
    continuation = None
    for _ in range(max(1, max_pages)):
        try:
            result = client.search_transfer_trains(query, continuation=continuation)
        except KorailNoResultsError as exc:
            if log:
                log(f"환승 조회에 결과가 없습니다 (서버 코드 {exc.code})")
            return
        yield result
        continuation = result.next_page()
        if continuation is None:
            return


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
        client, request.query(arrival=station), max_pages=request.max_pages
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
        max_pages=request.max_pages,
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
        first_legs = _first_leg_candidates(client, request, name)
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
        journeys.extend(search_direct(client, request, log=log))
        if log:
            log(f"직통 {len(journeys)}편")
    if request.include_transfer:
        before = len(journeys)
        if request.transfer_mode == TRANSFER_CUSTOM:
            journeys.extend(
                search_custom_transfer(client, request, log=log)[:MAX_CUSTOM_JOURNEYS]
            )
        else:
            journeys.extend(search_server_transfer(client, request, log=log))
        if log:
            log(f"환승 {len(journeys) - before}편")
    unique = deduplicate(journeys)
    kept = [journey for journey in unique if accepts(journey, request)]
    if log and unique and not kept:
        # 서버는 열차를 줬는데 화면이 비는 경우입니다. 조건 탓이라고 말해 주지
        # 않으면 프로그램이 고장 난 것처럼 보입니다.
        log(
            f"서버는 {len(unique)}편을 줬지만 조회 조건(시간대·열차 종류·"
            "환승시간)이 전부 걸러 냈습니다."
        )
    kept.sort(key=sort_key)
    return kept
