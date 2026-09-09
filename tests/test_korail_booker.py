"""``app/`` 의 GUI 프로그램에서 화면이 아닌 부분 전부를 시험합니다.

Tkinter 는 여기서 import 하지 않습니다 — 그래서 화면 없는 CI 에서도 돕니다.
프로그램이 그렇게 나뉘어 있기 때문입니다: 계산은 ``journeys``, 조회는
``search``, 자동예매는 ``autobook``, 화면은 ``ui`` 하나.

못박는 것은 안전 계약입니다.

* 미리보기 모드에서는 예약 요청이 한 건도 나가지 않는다
* 잡으면 그 자리에서 끝난다 — 두 번째 예약 요청은 없다
* 결제·환불·취소 범주의 consent 를 만들지 않는다
* 텔레그램 토큰은 어떤 문구에도 남지 않는다
* 설정 파일에는 비밀번호를 담을 자리가 아예 없다

모든 요청은 ``httpx.MockTransport`` 를 지납니다.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import os
import re
import stat
import sys
import threading
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest
from korail_booker import journeys as J
from korail_booker import logfmt as LF
from korail_booker import notify as N
from korail_booker import search as S
from korail_booker import settings as ST
from korail_booker.autobook import (
    AutoBooker,
    BookingOptions,
    Outcome,
    Target,
    cart_consent,
    is_standby_available,
    payment_deadline_text,
    reserve_consent,
)

from korail_mobile_api import (
    KorailClient,
    KorailPassengerCounts,
    KorailSeatClass,
    KorailSession,
    KorailSessionExpiredError,
    TrainSummary,
)


REPO_ROOT = Path(__file__).parents[1]
APP_DIR = REPO_ROOT / "app"
SEARCH = "/classes/com.korail.mobile.seatMovie.ScheduleView"
RESERVE = "/classes/com.korail.mobile.certification.TicketReservation"
STANDBY_ROUTE = "/classes/com.korail.mobile.reservationWait.ReservationWait"
CART = "/classes/com.korail.mobile.cart.addCartList"
SYNTHETIC_PNR = "399999999999999"


def _ok(**extra: Any) -> dict[str, Any]:
    return {"h_msg_cd": "SYNTHETIC.OK", "h_msg_txt": "ok", "strResult": "SUCC", **extra}


def _fail(code: str, message: str = "synthetic failure") -> dict[str, Any]:
    return {"h_msg_cd": code, "h_msg_txt": message, "strResult": "FAIL"}


def _row(
    train_no: str,
    *,
    departure: str = "서울",
    arrival: str = "부산",
    departure_code: str = "0001",
    arrival_code: str = "0020",
    departure_time: Any = "080000",
    arrival_time: str = "104200",
    general: str = "13",
    name: str = "KTX",
    **extra: Any,
) -> dict[str, Any]:
    return {
        **extra,
        "h_trn_no": train_no,
        "h_trn_gp_cd": "100",
        "h_dpt_rs_stn_cd": departure_code,
        "h_arv_rs_stn_cd": arrival_code,
        "h_dpt_rs_stn_nm": departure,
        "h_arv_rs_stn_nm": arrival,
        "h_dpt_dt": "20990101",
        "h_dpt_tm": departure_time,
        "h_arv_tm": arrival_time,
        "h_run_dt": "20990101",
        "h_trn_clsf_cd": "00",
        "h_trn_clsf_nm": name,
        "h_dpt_stn_run_ordr": "1",
        "h_arv_stn_run_ordr": "2",
        "h_dpt_stn_cons_ordr": "1",
        "h_arv_stn_cons_ordr": "2",
        "h_seat_att_cd": "015",
        "h_gen_rsv_cd": general,
    }


def _summary(**overrides: Any) -> TrainSummary:
    return TrainSummary.from_raw(_row(overrides.pop("train_no", "00101"), **overrides))


def _search_reply(rows: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return _ok(trn_infos={"trn_info": rows}, **extra)


def _reserve_reply(**extra: Any) -> dict[str, Any]:
    body = _ok(
        h_pnr_no=SYNTHETIC_PNR,
        h_jrny_cnt="1",
        h_wct_no="SYNTHETIC_WCT",
        h_tmp_job_sqno1="JOB1",
        h_tmp_job_sqno2="JOB2",
        h_tot_prc="59800",
        h_tot_rcvd_amt="59800",
        h_ntisu_lmt_dt="20990101",
        h_ntisu_lmt_tm="121000",
        jrny_infos={"jrny_info": [{"h_jrny_sqno": "0001", "h_rsv_chg_no": "001"}]},
    )
    body.update(extra)
    return body


class _Recorder:
    def __init__(
        self,
        replies: dict[str, dict[str, Any]] | None = None,
        sequences: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.replies = replies or {}
        self.sequences = sequences or {}
        self.seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.seen.append(path)
        sequence = self.sequences.get(path)
        if sequence:
            body = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            return httpx.Response(200, json=body)
        body = self.replies.get(path)
        if body is None:  # pragma: no cover - 배선 실수 가드
            raise AssertionError(f"unexpected {request.method} {path}")
        return httpx.Response(200, json=body)

    def count(self, path: str) -> int:
        return self.seen.count(path)


def _client(recorder: _Recorder) -> KorailClient:
    client = KorailClient(transport=httpx.MockTransport(recorder))
    client.session.current = KorailSession(jsessionid="synthetic-session")
    return client


def _request(**overrides: Any) -> S.SearchRequest:
    values: dict[str, Any] = {
        "departure": "서울",
        "arrival": "부산",
        "date": "20990101",
        "passengers": KorailPassengerCounts(adult=1),
        "max_pages": 1,
    }
    values.update(overrides)
    return S.SearchRequest(**values)


def _journey(*legs: TrainSummary, source: J.JourneySource = J.JourneySource.DIRECT):
    return J.Journey(legs=tuple(legs), source=source)


# --- 시각과 소요 시간 ----------------------------------------------------------


def test_a_clock_that_lost_its_leading_zero_is_restored():
    """서버는 ``h_dpt_tm`` 을 JSON 숫자로도 보냅니다 — ``63000``."""
    train = _summary(departure_time=63000)
    assert J.normalize_clock(train.departure_time) == "063000"
    assert J.clock_to_minutes(train.departure_time) == 6 * 60 + 30


def test_a_train_that_crosses_midnight_has_a_positive_duration():
    assert J.elapsed_minutes("233000", "003000") == 60
    assert J.elapsed_minutes("080000", "104200") == 162
    assert J.elapsed_minutes("080000", None) is None


def test_durations_read_the_way_people_say_them():
    assert J.format_duration(162) == "2시간 42분"
    assert J.format_duration(120) == "2시간"
    assert J.format_duration(42) == "42분"
    assert J.format_duration(None) == "-"


def test_a_direct_journey_reports_its_own_duration():
    journey = _journey(_summary(departure_time="080000", arrival_time="104200"))
    assert journey.total_minutes == 162
    assert journey.transfer_minutes is None
    assert journey.transfer_station_name is None
    assert not journey.is_transfer


def test_a_transfer_journey_reports_total_leg_and_wait_times():
    first = _summary(
        train_no="00009", arrival="대전", arrival_code="0010",
        departure_time="082000", arrival_time="091500",
    )
    second = _summary(
        train_no="00503", departure="대전", departure_code="0010",
        departure_time="093700", arrival_time="110500",
    )
    journey = _journey(first, second, source=J.JourneySource.SERVER_TRANSFER)
    assert journey.total_minutes == 165          # 08:20 → 11:05
    assert journey.leg_minutes(0) == 55
    assert journey.leg_minutes(1) == 88
    assert journey.transfer_minutes == 22        # 09:15 → 09:37
    assert journey.transfer_station_name == "대전"


def test_a_journey_that_changes_station_reports_no_transfer_station():
    """한 역에 내려 다른 역에서 타는 여정이 실제로 옵니다."""
    first = _summary(arrival="용산", arrival_code="0104")
    second = _summary(departure="서울", departure_code="0001")
    journey = _journey(first, second, source=J.JourneySource.SERVER_TRANSFER)
    assert journey.transfer_station_name is None


# --- 좌석 상태 -----------------------------------------------------------------


def test_only_eleven_counts_as_available():
    for code in ("13", "10", "", "1", "111"):
        journey = _journey(_summary(general=code))
        assert not journey.seat_state(KorailSeatClass.GENERAL).available, code
    assert _journey(_summary(general="11")).seat_state(KorailSeatClass.GENERAL).available


def test_a_transfer_is_bookable_only_when_every_leg_is_open():
    open_leg = _summary(general="11")
    closed_leg = _summary(train_no="00503", general="13")
    assert not _journey(open_leg, closed_leg).seat_state(
        KorailSeatClass.GENERAL
    ).available
    assert _journey(open_leg, _summary(train_no="00503", general="11")).seat_state(
        KorailSeatClass.GENERAL
    ).available


def test_any_prefers_the_general_cabin_then_falls_back_to_the_suite():
    both = _journey(_summary(general="11", h_spe_rsv_cd="11"))
    assert both.bookable_seat_class(J.SeatPreference.ANY) is KorailSeatClass.GENERAL
    suite_only = _journey(_summary(general="13", h_spe_rsv_cd="11"))
    assert (
        suite_only.bookable_seat_class(J.SeatPreference.ANY) is KorailSeatClass.SPECIAL
    )
    assert suite_only.bookable_seat_class(J.SeatPreference.GENERAL) is None
    assert _journey(_summary()).bookable_seat_class(J.SeatPreference.ANY) is None


def test_the_availability_label_is_what_the_app_prints():
    journey = _journey(_summary(h_rsv_psb_nm="매진"))
    assert journey.seat_state(KorailSeatClass.GENERAL).label == "매진"


def test_remaining_seats_are_read_when_the_server_sends_them():
    journey = _journey(_summary(h_std_rest_seat_cnt="12", h_fst_rest_seat_cnt="3"))
    assert journey.remaining_seats(KorailSeatClass.GENERAL) == 12
    assert journey.remaining_seats(KorailSeatClass.SPECIAL) == 3
    # 안 보내 주는 것이 흔합니다. 없는 것을 0 으로 바꾸면 "자리 없음" 이 됩니다.
    assert _journey(_summary()).remaining_seats(KorailSeatClass.GENERAL) is None


def test_a_transfer_reports_the_scarcest_leg():
    journey = _journey(
        _summary(arrival="대전", arrival_code="0010", h_std_rest_seat_cnt="9"),
        _summary(train_no="00503", departure="대전", departure_code="0010",
                 h_std_rest_seat_cnt="2"),
        source=J.JourneySource.SERVER_TRANSFER,
    )
    assert journey.remaining_seats(KorailSeatClass.GENERAL) == 2


def test_standing_free_seats_and_standby_are_reported():
    """좌석이 매진이어도 입석·자유석·예약대기는 따로 열려 있을 수 있습니다."""
    journey = _journey(
        _summary(general="13", h_stnd_rsv_cd="11", h_free_rsv_cd="11",
                 h_wait_rsv_flg=" 9")
    )
    assert journey.extras() == ("입석", "자유석", "예약대기")
    assert _journey(_summary()).extras() == ()


def test_standby_is_not_offered_on_a_transfer_row():
    journey = _journey(
        _summary(arrival="대전", arrival_code="0010", h_wait_rsv_flg=" 9"),
        _summary(train_no="00503", departure="대전", departure_code="0010",
                 h_wait_rsv_flg=" 9"),
        source=J.JourneySource.SERVER_TRANSFER,
    )
    assert "예약대기" not in journey.extras()


def test_an_extra_needs_every_leg_to_offer_it():
    journey = _journey(
        _summary(arrival="대전", arrival_code="0010", h_stnd_rsv_cd="11"),
        _summary(train_no="00503", departure="대전", departure_code="0010",
                 h_stnd_rsv_cd="13"),
        source=J.JourneySource.SERVER_TRANSFER,
    )
    assert "입석" not in journey.extras()


def test_the_seat_cell_leads_with_sold_out_or_available():
    """자동예매가 노리는 것이 매진이라, 그 한 낱말이 맨 앞에 와야 합니다."""
    open_seat = _journey(
        _summary(general="11", h_rsv_psb_nm="여유", h_std_rest_seat_cnt="12")
    )
    assert open_seat.seat_text(KorailSeatClass.GENERAL) == "예약가능 · 여유 · 12석"
    sold = _journey(_summary(general="13", h_rsv_psb_nm="매진"))
    assert sold.seat_text(KorailSeatClass.GENERAL) == "매진"
    assert sold.seat_state(KorailSeatClass.GENERAL).sold_out
    assert not open_seat.seat_state(KorailSeatClass.GENERAL).sold_out


def test_a_cabin_the_train_does_not_have_reads_as_a_dash():
    """특실 없는 열차의 특실 칸은 매진이 아닙니다 — 아예 없는 것입니다."""
    train = _journey(_summary(general="11"))
    state = train.seat_state(KorailSeatClass.SPECIAL)
    assert state.absent and not state.sold_out
    assert state.status == "-"


def test_a_transfer_is_sold_out_when_any_leg_is():
    journey = _journey(
        _summary(arrival="대전", arrival_code="0010", general="11"),
        _summary(train_no="00503", departure="대전", departure_code="0010",
                 general="13"),
        source=J.JourneySource.SERVER_TRANSFER,
    )
    assert journey.seat_state(KorailSeatClass.GENERAL).sold_out


# --- 조회 조건 -----------------------------------------------------------------


def test_the_time_window_filters_on_the_first_leg():
    request = _request(depart_after="080000", depart_before="120000")
    assert S.accepts(_journey(_summary(departure_time="080000")), request)
    assert S.accepts(_journey(_summary(departure_time=63000)), request) is False
    assert not S.accepts(_journey(_summary(departure_time="120100")), request)


def test_several_train_kinds_can_be_picked_at_once():
    """여러 개를 고르면 그중 하나라도 맞으면 통과입니다."""
    request = _request(train_names=("KTX", "무궁화"))
    assert S.accepts(_journey(_summary(name="KTX-산천")), request)
    assert S.accepts(_journey(_summary(name="무궁화호")), request)
    assert not S.accepts(_journey(_summary(name="ITX-마음")), request)
    assert S.accepts(_journey(_summary(name="ITX-마음")), _request())  # 안 고르면 전부


def test_a_seat_label_that_arrives_on_two_lines_is_folded_into_one():
    """서버 문구는 운임 아래에 적립 안내가 줄바꿈으로 붙어 옵니다.

    표의 행은 한 줄 높이라, 접지 않으면 둘째 줄이 잘려 글자가 반토막으로
    보입니다 — 실제로 그렇게 보였습니다.
    """
    journey = _journey(_summary(h_rsv_psb_nm="37,200원\n5%적립 1,860원"))
    label = journey.seat_state(KorailSeatClass.GENERAL).label
    assert "\n" not in label
    assert label == "37,200원 5%적립 1,860원"
    assert J.one_line("  두   줄\n하나로  ") == "두 줄 하나로"


def test_the_train_kind_filter_applies_to_every_leg():
    request = _request(train_names=("KTX",))
    assert S.accepts(_journey(_summary(name="KTX-이음")), request)
    assert not S.accepts(_journey(_summary(name="무궁화호")), request)
    mixed = _journey(
        _summary(name="KTX", arrival="대전", arrival_code="0010"),
        _summary(name="무궁화호", departure="대전", departure_code="0010"),
        source=J.JourneySource.SERVER_TRANSFER,
    )
    assert not S.accepts(mixed, request)


def _transfer_journey(wait_minutes: int, station: str = "대전") -> J.Journey:
    arrival = 9 * 60 + 15
    departure = arrival + wait_minutes
    return _journey(
        _summary(arrival=station, arrival_code="0010", arrival_time="091500"),
        _summary(
            train_no="00503",
            departure=station,
            departure_code="0010",
            departure_time=f"{departure // 60:02d}{departure % 60:02d}00",
        ),
        source=J.JourneySource.SERVER_TRANSFER,
    )


def test_the_transfer_window_is_a_range_the_user_types():
    request = _request(min_transfer_minutes=10, max_transfer_minutes=30)
    assert not S.accepts(_transfer_journey(5), request)
    assert S.accepts(_transfer_journey(20), request)
    assert not S.accepts(_transfer_journey(45), request)
    unbounded = _request(min_transfer_minutes=10, max_transfer_minutes=0)
    assert S.accepts(_transfer_journey(240), unbounded)


def test_a_named_transfer_station_filters_server_itineraries():
    request = _request(transfer_stations=("동대구",), transfer_mode=S.TRANSFER_SERVER)
    assert not S.accepts(_transfer_journey(20, station="대전"), request)
    assert S.accepts(_transfer_journey(20, station="동대구"), request)


def test_the_log_says_which_condition_dropped_which_train():
    """0편이면 왜 0편인지 말해야 합니다 — 어느 칸을 고칠지 알 수 있게."""
    late = _journey(_summary(train_no="00777", departure_time="193000", name="무궁화호"))
    request = _request(depart_before="120000", train_names=("KTX",))
    lines = S.rejection_lines([late], request)
    assert "시간대 1편" in lines[0]
    assert "열차 종류 1편" in lines[0]
    assert "무궁화호 00777 19:30 출발" in lines[1]
    assert "시간대, 열차 종류 때문에 빠짐" in lines[1]
    assert S.rejection_lines([_journey(_summary())], _request()) == []


def test_a_partly_filtered_result_is_reported_too():
    """전부 걸러졌을 때만이 아니라, 줄어들었을 때도 말합니다."""
    recorder = _Recorder(
        {SEARCH: _search_reply([
            _row("00101", departure_time="080000"),
            _row("00103", departure_time="200000"),
        ])}
    )
    lines: list[str] = []
    found = S.search_journeys(
        _client(recorder), _request(depart_before="120000"), log=lines.append
    )
    assert [j.train_numbers()[0] for j in found] == ["00101"]
    assert any("2편 중 1편이 조건에 맞습니다" in line for line in lines)
    assert any("시간대 1편" in line for line in lines)


# --- 조회 ---------------------------------------------------------------------


def test_direct_search_returns_journeys_in_departure_order():
    recorder = _Recorder(
        {SEARCH: _search_reply([
            _row("00103", departure_time="100000"),
            _row("00101", departure_time="080000"),
        ])}
    )
    found = S.search_journeys(_client(recorder), _request())
    assert [j.train_numbers()[0] for j in found] == ["00101", "00103"]
    assert all(j.source is J.JourneySource.DIRECT for j in found)


def test_no_results_is_an_answer_not_a_failure():
    recorder = _Recorder({SEARCH: _fail("WRD000061", "직통열차가 없습니다")})
    assert S.search_journeys(_client(recorder), _request()) == []


def test_server_transfer_pairs_rows_the_way_the_app_does():
    recorder = _Recorder(
        {SEARCH: _search_reply([
            _row("00009", arrival="대전", arrival_code="0010", arrival_time="091500"),
            _row("00503", departure="대전", departure_code="0010",
                 departure_time="093700", arrival_time="110500"),
        ])}
    )
    found = S.search_journeys(
        _client(recorder),
        _request(include_direct=False, include_transfer=True),
    )
    assert len(found) == 1
    assert found[0].source is J.JourneySource.SERVER_TRANSFER
    assert found[0].transfer_station_name == "대전"
    assert found[0].transfer_minutes == 22


def test_custom_transfer_searches_each_leg_and_combines_them():
    """환승역을 지정하면 두 구간을 각각 조회해 붙입니다."""
    recorder = _Recorder(
        sequences={
            SEARCH: [
                _search_reply([
                    _row("00009", arrival="대전", arrival_code="0010",
                         arrival_time="091500"),
                ]),
                _search_reply([
                    _row("00503", departure="대전", departure_code="0010",
                         departure_time="093700", arrival_time="110500"),
                    _row("00505", departure="대전", departure_code="0010",
                         departure_time="121500", arrival_time="140000"),
                ]),
            ]
        }
    )
    found = S.search_journeys(
        _client(recorder),
        _request(
            include_direct=False,
            include_transfer=True,
            transfer_mode=S.TRANSFER_CUSTOM,
            transfer_stations=("대전",),
            max_transfer_minutes=60,
        ),
    )
    assert recorder.count(SEARCH) == 2  # 구간마다 한 번씩
    assert len(found) == 1              # 3시간 기다리는 조합은 걸러집니다
    assert found[0].source is J.JourneySource.CUSTOM_TRANSFER
    assert found[0].transfer_minutes == 22


def test_a_server_itinerary_wins_over_the_same_custom_combination():
    first = _summary(arrival="대전", arrival_code="0010", arrival_time="091500")
    second = _summary(train_no="00503", departure="대전", departure_code="0010",
                      departure_time="093700")
    server = _journey(first, second, source=J.JourneySource.SERVER_TRANSFER)
    custom = _journey(first, second, source=J.JourneySource.CUSTOM_TRANSFER)
    unique = S.deduplicate([server, custom])
    assert len(unique) == 1
    assert unique[0].source is J.JourneySource.SERVER_TRANSFER


def test_transfer_conditions_never_touch_a_direct_train():
    """환승시간·환승역은 환승 결과에만 걸립니다. 직통은 그 조건과 무관합니다."""
    direct = _journey(_summary(departure_time="080000"))
    strict = _request(
        include_transfer=True,
        min_transfer_minutes=25,
        max_transfer_minutes=30,
        transfer_stations=("동대구",),
    )
    assert S.accepts(direct, strict)


def test_a_failing_transfer_search_keeps_the_direct_results():
    """환승 조회가 깨져도 이미 받은 직통은 살아남습니다.

    예전에는 둘이 한 덩어리라, 환승 응답 하나가 예외를 내면 직통 목록까지
    통째로 사라졌습니다 — 화면에서는 "환승을 켜니 직통이 안 나온다" 로
    보입니다.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = httpx.QueryParams(request.content.decode())
        # radJobId 2 가 환승 조회입니다(앱이 되돌릴 때 쓰는 값).
        transfer = body.get("radJobId") == "2"
        calls.append("transfer" if transfer else "direct")
        if transfer:
            return httpx.Response(500, text="synthetic server failure")
        return httpx.Response(
            200, json=_search_reply([_row("00101", general="11")])
        )

    client = KorailClient(transport=httpx.MockTransport(handler))
    client.session.current = KorailSession(jsessionid="synthetic-session")
    lines: list[str] = []
    found = S.search_journeys(
        client,
        _request(include_direct=True, include_transfer=True),
        log=lines.append,
    )
    assert "transfer" in calls           # 환승도 실제로 시도했고
    assert len(found) == 1               # 직통은 남았습니다
    assert found[0].train_numbers() == ("00101",)
    assert any("환승 조회가 실패" in line for line in lines)


def test_a_session_expiry_is_never_swallowed_by_that_isolation():
    """갈래를 떼어 놓느라 세션 만료까지 삼키면 자동예매가 되살아나지 못합니다."""
    recorder = _Recorder({SEARCH: _fail("P058", "세션이 만료되었습니다")})
    with pytest.raises(KorailSessionExpiredError):
        S.search_journeys(_client(recorder), _request(), log=lambda line: None)


def test_the_return_leg_flips_the_route_and_keeps_its_own_window():
    """오는 편은 자기 시간대를 씁니다. 가는 편 시간창을 물려받지 않습니다.

    아침에 가서 저녁에 오는 것이 보통인데, 같은 시간창을 쓰면 오는 편이 통째로
    걸러집니다.
    """
    outbound = _request(
        departure="서울", arrival="부산", date="20990101",
        depart_after="080000", depart_before="120000",
        transfer_stations=("대전",),
    )
    inbound = S.return_request(
        outbound, date="20990105", depart_after="170000", depart_before="210000"
    )
    assert (inbound.departure, inbound.arrival) == ("부산", "서울")
    assert inbound.date == "20990105"
    assert (inbound.depart_after, inbound.depart_before) == ("170000", "210000")
    # 환승역 후보는 구간마다 다릅니다. 물려받으면 없는 역으로 거르게 됩니다.
    assert inbound.transfer_stations == ()
    # 나머지 조건은 그대로입니다.
    assert inbound.passengers == outbound.passengers


def test_a_return_date_before_the_outbound_one_is_refused():
    outbound = _request(date="20990105")
    with pytest.raises(ValueError, match="빠릅니다"):
        S.return_request(outbound, date="20990101")


# --- 환승역 후보 ---------------------------------------------------------------

STATION_DATA = "/classes/com.korail.mobile.common.stationdata"
TRANSFER_STATIONS = "/classes/com.korail.mobile.qry.chtnStn.do"


def test_transfer_candidates_are_the_ones_the_server_names_for_this_route():
    """전국 역 목록이 아니라 이 구간에서 갈아탈 수 있는 역만 옵니다."""
    recorder = _Recorder(
        {
            STATION_DATA: {
                "stns": {
                    "stn": [
                        {"stn_cd": "0001", "stn_nm": "서울"},
                        {"stn_cd": "0020", "stn_nm": "부산"},
                        {"stn_cd": "0010", "stn_nm": "대전"},
                    ]
                }
            },
            TRANSFER_STATIONS: _ok(
                chtnList=[
                    {"chtnRsStnCd": "0010", "chtnRsStnNm": "대전"},
                    {"chtnRsStnCd": "0015", "chtnRsStnNm": "동대구"},
                ]
            ),
        }
    )
    names = S.transfer_station_candidates(_client(recorder), "서울", "부산")
    assert names == ["대전", "동대구"]
    assert recorder.count(TRANSFER_STATIONS) == 1


def test_a_route_with_no_transfer_station_is_an_empty_answer():
    recorder = _Recorder(
        {
            STATION_DATA: {"stns": {"stn": [{"stn_cd": "0001", "stn_nm": "서울"},
                                            {"stn_cd": "0104", "stn_nm": "용산"}]}},
            TRANSFER_STATIONS: _ok(chtnList=[]),
        }
    )
    assert S.transfer_station_candidates(_client(recorder), "서울", "용산") == []


def test_station_search_puts_prefix_matches_first():
    """사람은 이름 앞을 칩니다 — "동" 은 동대구가 광주송정보다 먼저입니다."""
    names = ["서울", "동대구", "광주송정", "동해", "대전"]
    assert S.filter_station_names(names, "동") == ["동대구", "동해"]
    assert S.filter_station_names(names, "대") == ["대전", "동대구"]
    assert S.filter_station_names(names, "") == names
    assert S.filter_station_names(names, "없는역") == []
    assert S.filter_station_names(names, "대", limit=1) == ["대전"]


def test_station_codes_pass_through_and_unknown_names_are_refused():
    index = {"서울": "0001", "부산": "0020"}
    assert S.resolve_station_code("서울", index) == "0001"
    assert S.resolve_station_code("0020", index) == "0020"  # 이미 코드면 그대로
    assert S.resolve_station_code("없는역", index) is None
    recorder = _Recorder({STATION_DATA: {"stns": {"stn": []}}})
    with pytest.raises(ValueError, match="코드"):
        S.transfer_station_candidates(_client(recorder), "없는역", "부산")


# --- consent ------------------------------------------------------------------


def test_the_program_opens_one_category_at_a_time():
    for live in (False, True):
        reserve = reserve_consent(live=live)
        assert reserve.allow_reserve and not reserve.allow_cart
        assert not (reserve.allow_payment or reserve.allow_refund or reserve.allow_cancel)
        cart = cart_consent(live=live)
        assert cart.allow_cart and not cart.allow_reserve
        assert not (cart.allow_payment or cart.allow_refund or cart.allow_cancel)
    assert reserve_consent(live=False).dry_run is True
    assert reserve_consent(live=True).dry_run is False


def test_no_module_in_the_app_names_a_money_moving_call():
    for path in sorted(APP_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "allow_payment=True",
            "allow_refund=True",
            "allow_cancel=True",
            "real_card_acknowledged",
            "CardPayment",
            "pay_with_card",
            "pay_with_fake_card",
            ".refund(",
            "cancel_unpaid_hold(",
        ):
            assert forbidden not in source, f"{path.name}: {forbidden}"


# --- 예약 폼을 만들 수 없는 행 ---------------------------------------------------


def _row_without_class_code(**overrides: Any) -> dict[str, Any]:
    """``h_trn_clsf_cd`` 가 없는 행.

    처음 이 경로를 만든 계기는 수서→창원중앙 KTX-산천 387 이었는데, 그 행이
    보낸 값은 ``"0A"`` 였고 **그것을 거절한 쪽은 서버가 아니라 라이브러리의
    지나친 검사였습니다**(이제 받습니다). 그래도 이 경로는 남습니다 — 값이
    아예 없으면 폼을 만들 방법이 없고, 그때 담기 전에 알아야 합니다. 여기서는
    그 모양을 재현합니다.
    """
    row = _row("00387", **overrides)
    del row["h_trn_clsf_cd"]
    return row


def test_a_row_without_the_reservation_fields_is_named_before_it_bites():
    """자리가 열려도 폼이 안 만들어지는 행이 있습니다. 담기 전에 알아야 합니다."""
    broken = _journey(TrainSummary.from_raw(_row_without_class_code(general="11")))
    reason = J.unbookable_reason(broken)
    assert reason is not None and "train_class_code" in reason
    assert J.unbookable_reason(_journey(_summary(general="11"))) is None


def test_the_unbookable_detail_reports_the_value_instead_of_guessing_why():
    """원인을 지어내지 않고, 서버가 그 자리에 보낸 값을 그대로 보여 줍니다.

    예전 문구는 "수서 출발이라 SRT 라서" 라고 단정했는데, 그것은 확인한 적
    없는 추측이었습니다. 화면에는 열차와 필드 이름과 받은 값만 남습니다.
    """
    broken = _journey(TrainSummary.from_raw(_row_without_class_code(general="11")))
    detail = J.unbookable_detail(broken)
    assert detail is not None
    assert "train_class_code" in detail
    assert "None" in detail  # 서버가 그 자리에 준 값
    assert "387" in detail  # 어느 구간인지
    assert "SRT" not in detail
    assert J.unbookable_detail(_journey(_summary(general="11"))) is None


def test_such_a_target_is_dropped_instead_of_killing_the_whole_run():
    """예전에는 그 한 건이 자동예매 전체를 실패로 끝냈습니다."""
    recorder = _Recorder(
        {SEARCH: _search_reply([
            _row_without_class_code(general="11"),
            _row("00101", general="13"),
        ])}
    )
    broken = _journey(TrainSummary.from_raw(_row_without_class_code(general="11")))
    healthy = _journey(_summary(general="13"))
    booker = _booker(recorder, [broken, healthy], live=True, watch_minutes=0)
    booker._sleep = lambda stop, deadline: None  # type: ignore[method-assign]
    stop = threading.Event()
    polls: list[int] = []
    original = booker._poll

    def limited():
        polls.append(1)
        if len(polls) >= 3:
            stop.set()
        return original()

    booker._poll = limited  # type: ignore[method-assign]
    result = booker.run(stop)
    assert result.outcome is Outcome.STOPPED       # 죽지 않고 계속 지켜봤고
    assert broken.key() in booker._unusable        # 못 쓰는 것만 빠졌습니다
    # 폼을 만들다 걸리므로 요청은 아예 나가지 않습니다.
    assert recorder.count(RESERVE) == 0


def test_when_every_target_is_unusable_the_run_says_why():
    recorder = _Recorder(
        {SEARCH: _search_reply([_row_without_class_code(general="11")])}
    )
    broken = _journey(TrainSummary.from_raw(_row_without_class_code(general="11")))
    result = _booker(recorder, [broken], live=True).run(threading.Event())
    assert result.outcome is Outcome.FAILED
    assert "예약에 필요한 값" in result.message


# --- 런처 ---------------------------------------------------------------------


def _load_launcher():
    """``app/main.py`` 를 import 합니다. import 만으로는 창이 열리지 않습니다."""
    spec = importlib.util.spec_from_file_location(
        "korail_booker_launcher", APP_DIR / "main.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_launcher_names_the_dependencies_the_package_actually_declares():
    """설치 안내가 틀린 이름을 알려 주면 안내가 없느니만 못합니다."""
    pyproject = tomllib.loads(
        (APP_DIR.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    declared = {
        re.split(r"[<>=!~;\[ ]", requirement)[0]
        for requirement in pyproject["project"]["dependencies"]
    }
    assert set(_load_launcher().DEPENDENCIES) == declared


# --- 자동예매 -----------------------------------------------------------------


def _target(journey, request=None, label="") -> Target:
    return Target(journey=journey, request=request or _request(), label=label)


def _booker(recorder: _Recorder, targets, **option_overrides: Any) -> AutoBooker:
    options = BookingOptions(
        poll_interval_s=option_overrides.pop("poll_interval_s", 10.0),
        watch_minutes=option_overrides.pop("watch_minutes", 60),
        **option_overrides,
    )
    return AutoBooker(
        _client(recorder),
        [target if isinstance(target, Target) else _target(target) for target in targets],
        options,
        log=lambda message: None,
    )


def test_the_poll_interval_has_a_floor():
    with pytest.raises(ValueError, match="주기"):
        BookingOptions(poll_interval_s=1.0)


def test_preview_mode_sends_no_reservation():
    recorder = _Recorder({SEARCH: _search_reply([_row("00101", general="11")])})
    target = _journey(_summary(general="13"))
    result = _booker(recorder, [target], live=False).run(threading.Event())
    assert result.outcome is Outcome.PREVIEW
    assert recorder.count(RESERVE) == 0


def test_a_sold_out_target_is_taken_the_moment_it_opens_and_only_once():
    recorder = _Recorder(
        replies={RESERVE: _reserve_reply()},
        sequences={
            SEARCH: [
                _search_reply([_row("00101", general="13")]),
                _search_reply([_row("00101", general="13")]),
                _search_reply([_row("00101", general="11")]),
            ]
        },
    )
    target = _journey(_summary(general="13"))
    booker = _booker(recorder, [target], live=True, poll_interval_s=10.0)
    booker.options = dataclasses.replace(booker.options, poll_interval_s=10.0)
    # 주기를 기다리지 않도록 잠을 없앱니다. 무엇을 보내는지가 시험 대상입니다.
    booker._sleep = lambda stop, deadline: None  # type: ignore[method-assign]
    result = booker.run(threading.Event())
    assert result.outcome is Outcome.HELD
    assert result.pnr_no == SYNTHETIC_PNR
    assert recorder.count(SEARCH) == 3
    assert recorder.count(RESERVE) == 1
    assert recorder.seen[-1] == RESERVE


def test_a_seat_lost_in_the_same_second_keeps_watching():
    recorder = _Recorder(
        {
            SEARCH: _search_reply([_row("00101", general="11")]),
            RESERVE: _fail("ERR211161", "매진"),
        }
    )
    booker = _booker(recorder, [_journey(_summary(general="13"))], live=True,
                     watch_minutes=0)
    booker._sleep = lambda stop, deadline: None  # type: ignore[method-assign]
    stop = threading.Event()

    calls: list[int] = []
    original = booker._poll

    def counting_poll():
        calls.append(1)
        if len(calls) >= 3:
            stop.set()
        return original()

    booker._poll = counting_poll  # type: ignore[method-assign]
    result = booker.run(stop)
    assert result.outcome is Outcome.STOPPED
    assert recorder.count(RESERVE) >= 2  # 놓칠 때마다 다시 시도합니다


def test_a_transfer_target_books_both_legs_in_one_request():
    first = _row("00009", arrival="대전", arrival_code="0010", arrival_time="091500",
                 general="11")
    second = _row("00503", departure="대전", departure_code="0010",
                  departure_time="093700", arrival_time="110500", general="11")
    recorder = _Recorder(
        {
            SEARCH: _search_reply([first, second]),
            RESERVE: _reserve_reply(h_jrny_cnt="2"),
        }
    )
    target = _journey(
        TrainSummary.from_raw(first),
        TrainSummary.from_raw(second),
        source=J.JourneySource.SERVER_TRANSFER,
    )
    booker = AutoBooker(
        _client(recorder),
        [_target(target, _request(include_direct=False, include_transfer=True))],
        BookingOptions(poll_interval_s=10.0, live=True),
        log=lambda message: None,
    )
    result = booker.run(threading.Event())
    assert result.outcome is Outcome.HELD
    assert recorder.count(RESERVE) == 1


def test_standby_is_direct_only():
    direct = _journey(_summary(h_wait_rsv_flg=" 9"))
    transfer = _journey(
        _summary(arrival="대전", arrival_code="0010", h_wait_rsv_flg=" 9"),
        _summary(train_no="00503", departure="대전", departure_code="0010",
                 h_wait_rsv_flg=" 9"),
        source=J.JourneySource.SERVER_TRANSFER,
    )
    assert is_standby_available(direct)
    assert not is_standby_available(transfer)


def test_standby_holds_then_confirms_when_the_code_says_so():
    recorder = _Recorder(
        {
            SEARCH: _search_reply([_row("00101", h_wait_rsv_flg=" 9")]),
            RESERVE: _reserve_reply(h_msg_cd="IRR000014"),
            STANDBY_ROUTE: _ok(),
        }
    )
    target = _journey(_summary(h_wait_rsv_flg=" 9"))
    result = _booker(recorder, [target], live=True, allow_standby=True).run(
        threading.Event()
    )
    assert result.outcome is Outcome.HELD
    assert recorder.count(STANDBY_ROUTE) == 1


def test_standby_without_the_confirmation_code_leaves_the_hold_alone():
    recorder = _Recorder(
        {
            SEARCH: _search_reply([_row("00101", h_wait_rsv_flg=" 9")]),
            RESERVE: _reserve_reply(),
        }
    )
    target = _journey(_summary(h_wait_rsv_flg=" 9"))
    result = _booker(recorder, [target], live=True, allow_standby=True).run(
        threading.Event()
    )
    assert result.outcome is Outcome.HELD
    assert recorder.count(STANDBY_ROUTE) == 0


def test_the_cart_is_only_touched_when_asked():
    recorder = _Recorder(
        {SEARCH: _search_reply([_row("00101", general="11")]), RESERVE: _reserve_reply()}
    )
    target = _journey(_summary(general="11"))
    _booker(recorder, [target], live=True).run(threading.Event())
    assert recorder.count(CART) == 0

    recorder = _Recorder(
        {
            SEARCH: _search_reply([_row("00101", general="11")]),
            RESERVE: _reserve_reply(),
            CART: _ok(),
        }
    )
    _booker(recorder, [target], live=True, add_to_cart=True).run(threading.Event())
    assert recorder.count(CART) == 1


def test_an_expired_session_logs_in_again():
    logins: list[int] = []
    recorder = _Recorder(
        replies={RESERVE: _reserve_reply()},
        sequences={
            SEARCH: [
                _fail("P058", "세션이 만료되었습니다"),
                _search_reply([_row("00101", general="11")]),
            ]
        },
    )
    client = _client(recorder)

    def relogin() -> None:
        logins.append(1)
        client.session.current = KorailSession(jsessionid="fresh")

    booker = AutoBooker(
        client,
        [_target(_journey(_summary(general="13")))],
        BookingOptions(poll_interval_s=10.0, live=True),
        log=lambda message: None,
        relogin=relogin,
    )
    booker._sleep = lambda stop, deadline: None  # type: ignore[method-assign]
    result = booker.run(threading.Event())
    assert result.outcome is Outcome.HELD
    assert len(logins) == 1


def test_a_watch_that_runs_out_of_time_says_so():
    recorder = _Recorder({SEARCH: _search_reply([_row("00101", general="13")])})
    booker = _booker(recorder, [_journey(_summary(general="13"))], live=True,
                     watch_minutes=0)
    booker.options = dataclasses.replace(booker.options, watch_minutes=1)
    booker._sleep = lambda stop, deadline: None  # type: ignore[method-assign]
    polls: list[int] = []
    original = booker._poll

    def limited():
        polls.append(1)
        if len(polls) > 2:
            booker.options = dataclasses.replace(booker.options, watch_minutes=0)
        return original()

    booker._poll = limited  # type: ignore[method-assign]
    stop = threading.Event()
    threading.Timer(0.4, stop.set).start()
    result = booker.run(stop)
    assert result.outcome in (Outcome.STOPPED, Outcome.TIMEOUT)
    assert recorder.count(RESERVE) == 0


def test_the_payment_deadline_is_only_what_the_server_said():
    class _Hold:
        payment_deadline_date = "20990101"
        payment_deadline_time = "121000"
        payment_deadline_notice = ""

    assert payment_deadline_text(_Hold()) == "2099-01-01 12:10:00"

    class _NoDeadline:
        payment_deadline_date = ""
        payment_deadline_time = ""
        payment_deadline_notice = "12:10 까지 미결제시 자동 취소됩니다"

    assert "자동 취소" in payment_deadline_text(_NoDeadline())
    assert payment_deadline_text(None) == "알 수 없음"


def test_a_round_trip_holds_one_per_direction_and_then_stops():
    """왕복은 방향마다 한 건입니다. 한쪽을 잡아도 다른 쪽은 계속 지켜봅니다."""
    outbound = _request(departure="서울", arrival="부산", date="20990101")
    inbound = _request(departure="부산", arrival="서울", date="20990105")

    def handler(request: httpx.Request) -> httpx.Response:
        body = httpx.QueryParams(request.content.decode())
        if request.url.path == RESERVE:
            return httpx.Response(200, json=_reserve_reply())
        # 가는 편만 자리가 열려 있습니다.
        going = body.get("txtGoStart") == "서울"
        row = _row("00101", general="11" if going else "13")
        return httpx.Response(200, json=_search_reply([row]))

    recorder_client = KorailClient(transport=httpx.MockTransport(handler))
    recorder_client.session.current = KorailSession(jsessionid="s")
    booker = AutoBooker(
        recorder_client,
        [
            _target(_journey(_summary(general="13")), outbound, "가는 편"),
            _target(
                _journey(
                    TrainSummary.from_raw(
                        _row("00101", departure="부산", arrival="서울",
                             departure_code="0020", arrival_code="0001")
                    )
                ),
                inbound,
                "오는 편",
            ),
        ],
        BookingOptions(poll_interval_s=10.0, live=True, watch_minutes=1),
        log=lambda message: None,
    )
    booker._sleep = lambda stop, deadline: None  # type: ignore[method-assign]
    stop = threading.Event()
    polls: list[int] = []
    original = booker._poll

    def limited():
        polls.append(1)
        if len(polls) > 3:
            stop.set()
        return original()

    booker._poll = limited  # type: ignore[method-assign]
    result = booker.run(stop)
    # 가는 편은 잡혔고, 오는 편은 만석이라 못 잡은 채 중지됐습니다.
    assert result.outcome is Outcome.STOPPED
    assert len(booker._settled) == 1


def test_both_directions_finish_together():
    recorder = _Recorder(
        {SEARCH: _search_reply([_row("00101", general="11")]),
         RESERVE: _reserve_reply()}
    )
    client = _client(recorder)
    booker = AutoBooker(
        client,
        [
            _target(_journey(_summary(general="11")),
                    _request(departure="서울", arrival="부산"), "가는 편"),
            _target(_journey(_summary(general="11")),
                    _request(departure="부산", arrival="서울", date="20990105"),
                    "오는 편"),
        ],
        BookingOptions(poll_interval_s=10.0, live=True),
        log=lambda message: None,
    )
    result = booker.run(threading.Event())
    assert result.outcome is Outcome.HELD
    assert len(result.holds) == 2          # 방향마다 하나씩
    assert recorder.count(RESERVE) == 2    # 그리고 딱 둘뿐


def test_one_search_per_direction_not_per_target():
    """같은 방향에 대상이 여럿이어도 조회는 한 번입니다."""
    recorder = _Recorder({SEARCH: _search_reply([_row("00101"), _row("00103")])})
    request = _request()
    booker = AutoBooker(
        _client(recorder),
        [
            _target(_journey(_summary(train_no="00101")), request),
            _target(_journey(_summary(train_no="00103")), request),
        ],
        BookingOptions(poll_interval_s=10.0, live=True, watch_minutes=1),
        log=lambda message: None,
    )
    fresh = booker._poll()
    assert recorder.count(SEARCH) == 1
    assert len(fresh) == 2


def test_the_ui_event_pump_schedules_nothing_but_itself():
    """``_drain`` 은 120ms 마다 돕니다. 여기에 다른 일을 걸면 그 일도 그렇게 돕니다.

    역 목록 조회가 실제로 여기 걸려 초당 여덟 번씩 새어 나갔습니다. Tkinter 를
    띄우지 않고 원문에서 확인합니다.
    """
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    drains = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_drain"
    ]
    assert len(drains) == 1
    scheduled = [
        ast.unparse(call.args[1])
        for call in ast.walk(drains[0])
        if isinstance(call, ast.Call)
        and ast.unparse(call.func).endswith("after")
        and len(call.args) >= 2
    ]
    assert scheduled == ["self._drain"], scheduled


def test_startup_applies_the_round_trip_state_to_the_return_fields():
    """켜자마자의 '오는 편' 칸은 왕복 체크박스를 따라야 합니다.

    ``_restore`` 가 ``_round_trip_toggled`` 을 부르지 않아, 왕복이 꺼져 있는데도
    오는 날짜 칸과 [달력] 이 눌리는 상태로 떴습니다. Tkinter 를 띄우지 않고
    원문에서 확인합니다 — 화면 없이 도는 시험이라야 매번 돕니다.
    """
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    restores = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_restore"
    ]
    assert len(restores) == 1
    called = {
        ast.unparse(call.func)
        for call in ast.walk(restores[0])
        if isinstance(call, ast.Call)
    }
    assert "self._round_trip_toggled" in called, sorted(called)


# --- 기록 모양 ----------------------------------------------------------------


def _drawn(message: str, level: str = "info") -> str:
    entry = LF.format_entry(message, stamp="09:00:00", level=level)
    assert entry is not None
    return "".join(text for text, _tag in entry.pieces)


def test_a_detail_line_drops_the_repeated_clock_and_indents():
    """곁가지 줄마다 같은 시각을 찍으면 시각이 오히려 안 보입니다."""
    drawn = _drawn("    장바구니에도 담았습니다")

    assert drawn == " " * LF.STAMP_WIDTH + "· 장바구니에도 담았습니다\n"
    entry = LF.format_entry("    담았습니다", stamp="09:00:00")
    assert entry is not None
    assert entry.pieces[-1][1] == "detail"


def test_the_second_line_of_a_message_lines_up_under_the_first():
    """예전에는 둘째 줄이 왼쪽 끝에 붙어 새 기록처럼 보였습니다."""
    drawn = _drawn("예약했습니다\nPNR 123\n결제 기한 17:45")

    assert drawn.splitlines()[0] == "[09:00:00] 예약했습니다"
    for line in drawn.splitlines()[1:]:
        assert line.startswith(" " * LF.STAMP_WIDTH)


def test_a_new_poll_round_gets_a_blank_line_before_it():
    """회차마다 덩어리로 끊겨야 눈이 따라갑니다."""
    assert LF.format_entry("[3] 387(GE:매진)", stamp="09:00:00").blank_before
    assert not LF.format_entry("로그인했습니다.", stamp="09:00:00").blank_before


def test_a_level_beats_the_shape_so_a_failure_never_reads_as_a_side_note():
    entry = LF.format_entry("    놓쳤습니다", stamp="09:00:00", level="bad")
    assert entry is not None
    assert entry.pieces[-1][1] == "bad"


def test_an_empty_message_draws_nothing():
    assert LF.format_entry("", stamp="09:00:00") is None
    assert LF.format_entry("   \n  ", stamp="09:00:00") is None


def test_the_two_log_panes_are_separate_widgets():
    """자동예매 회차 기록이 조회 기록을 밀어 올리면 안 됩니다."""
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")
    assert "self.booking_text" in source
    assert "log=self.log_booking," in source


def test_a_preview_run_says_out_loud_that_nothing_was_sent():
    """미리보기로 끝난 실행은 창을 띄워 알려야 합니다.

    기록 한 줄만 남기면 잡힌 줄 알고 코레일 장바구니를 열어 보게 됩니다 —
    실제로 그랬습니다. Tkinter 를 띄우지 않고 원문에서 확인합니다.
    """
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    done = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_booking_done"
    ]
    assert len(done) == 1
    body = ast.unparse(done[0])
    assert "Outcome.PREVIEW" in body
    assert "showinfo" in body


def test_real_reservations_are_on_by_default_but_still_gated():
    """기본이 켬입니다 — 이 프로그램을 켜는 이유가 진짜 예약이기 때문입니다.

    대신 켜져 있어도 그냥 나가지는 않습니다. 시작하면 확인 창이 뜨고,
    로그인하지 않았으면 시작 자체가 막힙니다. 셋 중 하나라도 사라지면
    실수 한 번이 진짜 예약이 되므로 함께 고정합니다.
    """
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")

    assert "self.live_mode = tk.BooleanVar(value=True)" in source
    assert "if options.live and not self._confirm_live(targets):" in source
    assert "if options.live and not self.logged_in:" in source


def test_the_live_switch_is_never_written_to_the_settings_file():
    """실제 예약은 켤 때마다 사람이 켜야 합니다. 저장해 두면 다음에 몰래 켜집니다."""
    stored = dataclasses.asdict(ST.Settings())
    assert not [name for name in stored if "live" in name]


# --- 배포용 실행기 --------------------------------------------------------------


WINDOWS_LAUNCHER = REPO_ROOT / "실행 (Windows).bat"
UNIX_LAUNCHER = REPO_ROOT / "실행 (macOS_Linux).command"


def test_both_launchers_exist_and_start_the_same_program():
    """더블클릭 한 번으로 도는 길. 두 실행기가 같은 곳을 가리켜야 합니다."""
    for launcher in (WINDOWS_LAUNCHER, UNIX_LAUNCHER):
        text = launcher.read_text(encoding="utf-8")
        assert "main.py" in text, launcher.name
        # 시스템 파이썬을 건드리지 않고 전용 환경에 넣습니다.
        assert ".venv" in text, launcher.name


def test_the_launchers_install_exactly_what_the_program_asks_for():
    """``main.py`` 가 세는 의존성과 실행기가 넣는 것이 어긋나면 안 됩니다.

    어긋나면 실행기는 성공했다고 하고 프로그램은 "없습니다" 로 죽습니다.
    """
    spec = importlib.util.spec_from_file_location("_launcher_main", APP_DIR / "main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for launcher in (WINDOWS_LAUNCHER, UNIX_LAUNCHER):
        text = launcher.read_text(encoding="utf-8")
        expected = f"pip install {' '.join(module.DEPENDENCIES)}"
        assert expected in text, launcher.name


def test_a_failed_setup_never_leaves_a_half_built_environment():
    """반쯤 만들어진 .venv 가 남으면 다음 실행이 '이미 있다' 로 착각합니다."""
    assert 'rmdir /s /q "%VENV%"' in WINDOWS_LAUNCHER.read_text(encoding="utf-8")
    assert 'rm -rf "$VENV"' in UNIX_LAUNCHER.read_text(encoding="utf-8")


def test_the_launchers_keep_the_line_endings_they_need():
    """.bat 은 CRLF, .command 는 LF 여야 각자의 셸이 읽습니다."""
    rules = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat text eol=crlf" in rules
    assert "*.command text eol=lf" in rules


def test_the_unix_launcher_is_executable():
    """실행 비트가 없으면 더블클릭도 ./ 실행도 되지 않습니다."""
    assert UNIX_LAUNCHER.stat().st_mode & stat.S_IXUSR


def test_the_frozen_entry_point_does_not_lean_on_runtime_paths():
    """PyInstaller 는 정적으로 훑습니다 — 실행 중에 붙인 sys.path 를 못 봅니다."""
    entry = (REPO_ROOT / "packaging" / "desktop_entry.py").read_text(encoding="utf-8")
    tree = ast.parse(entry)
    imported = {
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    assert "from korail_booker.ui import run" in imported
    # 설명 글에는 sys.path 가 나옵니다. 여기서 보는 것은 **코드**입니다.
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "path"
    ]

    workflow = (
        REPO_ROOT / ".github" / "workflows" / "desktop-build.yml"
    ).read_text(encoding="utf-8")
    assert "--paths src --paths app" in workflow


# --- 텔레그램 -----------------------------------------------------------------

FAKE_TOKEN = "123456789:SYNTHETIC-TOKEN-NOT-REAL"


def _telegram(handler) -> N.TelegramNotifier:
    return N.TelegramNotifier(
        N.TelegramConfig(token=FAKE_TOKEN, chat_id="42"),
        transport=httpx.MockTransport(handler),
    )


def test_a_notification_is_sent_to_the_configured_chat():
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"ok": True})

    with _telegram(handler) as bot:
        assert bot.send("잡았습니다") is True
    assert seen[0]["chat_id"] == "42"
    assert seen[0]["text"] == "잡았습니다"


def test_a_failed_notification_is_false_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network", request=request)

    with _telegram(handler) as bot:
        assert bot.send("잡았습니다") is False

    def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    with _telegram(refusing) as bot:
        assert bot.send("잡았습니다") is False


def test_notifications_are_off_until_both_values_are_set():
    assert not N.TelegramConfig(token=FAKE_TOKEN).enabled
    assert not N.TelegramConfig(chat_id="42").enabled
    assert N.TelegramConfig(token=FAKE_TOKEN, chat_id="42").enabled


def test_the_chat_id_can_be_found_from_the_bot_updates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getUpdates")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"update_id": 1, "message": {"chat": {"id": 555, "type": "private"}}}
                ],
            },
        )

    with _telegram(handler) as bot:
        assert bot.resolve_chat_id() == "555"


def test_the_token_never_survives_in_a_message():
    """텔레그램은 토큰을 URL 에 싣습니다 — 예외 문구에 그대로 들어옵니다."""
    leaked = f"Client error for url https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    masked = N.mask_token(leaked, FAKE_TOKEN)
    assert FAKE_TOKEN not in masked
    assert "123456789" not in masked

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed for {request.url}", request=request)

    with _telegram(handler) as bot:
        described = bot.describe_failure(httpx.ConnectError(leaked))
    assert FAKE_TOKEN not in described


# --- 설정 ---------------------------------------------------------------------


def test_settings_have_nowhere_to_put_a_password():
    fields = {field.name for field in dataclasses.fields(ST.Settings)}
    for forbidden in ("password", "pw", "passwd", "secret", "credential"):
        assert not any(forbidden in name for name in fields), forbidden


def test_settings_round_trip_and_are_owner_only(tmp_path: Path):
    path = tmp_path / "settings.json"
    stored = ST.Settings(login_id="tester", telegram_token=FAKE_TOKEN, adult=2)
    assert ST.save(stored, path) == path
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == ST.SETTINGS_FILE_MODE, oct(mode)
    loaded = ST.load(path)
    assert loaded.login_id == "tester"
    assert loaded.adult == 2
    assert loaded.telegram_token == FAKE_TOKEN


def test_a_broken_settings_file_falls_back_to_defaults(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert ST.load(path) == ST.Settings()
    path.write_text(json.dumps({"adult": "여덟", "unknown": 1}), encoding="utf-8")
    assert ST.load(path).adult == 1


def test_the_stored_transfer_window_matches_the_screen_default():
    """저장값이 화면 기본값을 덮어써 "0분 이하"로 되돌아간 적이 있습니다.

    화면(``ui``)을 import 하면 Tkinter 가 필요하므로, 여기서는 그 파일의 상수를
    원문에서 읽어 대조합니다.
    """
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")
    for name, value in (
        ("DEFAULT_MIN_TRANSFER_MINUTES", ST.Settings().min_transfer_minutes),
        ("DEFAULT_MAX_TRANSFER_MINUTES", ST.Settings().max_transfer_minutes),
    ):
        assert f"{name} = {value}\n" in source, name


def test_a_masked_settings_dump_hides_the_token():
    dumped = ST.Settings(telegram_token=FAKE_TOKEN).masked()
    assert dumped["telegram_token"] == "***"


def test_the_settings_file_lives_outside_the_repository(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/synthetic-config")
    if sys.platform != "win32":
        assert ST.settings_path() == Path("/tmp/synthetic-config/korail-booker/settings.json")
    assert Path(__file__).parents[1] not in ST.settings_path().parents
