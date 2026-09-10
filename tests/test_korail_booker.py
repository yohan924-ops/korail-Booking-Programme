"""``app/`` 의 GUI 프로그램에서 화면이 아닌 부분 전부를 시험합니다.

Tkinter 는 여기서 import 하지 않습니다 — 그래서 화면 없는 CI 에서도 돕니다.
프로그램이 그렇게 나뉘어 있기 때문입니다: 계산은 ``journeys``, 조회는
``search``, 자동예매는 ``autobook``, 화면은 ``ui`` 하나.

못박는 것은 안전 계약입니다.

* 미리보기 모드에서는 예약 요청이 한 건도 나가지 않는다
* 잡으면 그 자리에서 끝난다 — 두 번째 예약 요청은 없다
* 결제·환불 범주의 consent 는 어디서도 만들지 않는다
* 취소 범주는 ``ui.py`` 의 ``cancel_consent()`` 한 곳에서만 열리고,
  자동예매(``autobook.py``)는 절대 취소를 부르지 않는다
* 텔레그램 토큰은 어떤 문구에도 남지 않는다
* 설정 파일에는 비밀번호를 담을 자리가 아예 없다

모든 요청은 ``httpx.MockTransport`` 를 지납니다.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import itertools
import json
import os
import re
import stat
import sys
import threading
import time
import tomllib
import urllib.parse
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from korail_booker import holds as H
from korail_booker import journeys as J
from korail_booker import logfmt as LF
from korail_booker import notify as N
from korail_booker import search as S
from korail_booker import session as ST_SESSION
from korail_booker import settings as ST
from korail_booker.autobook import (
    AutoBooker,
    BookingOptions,
    Outcome,
    PartialTransferError,
    Target,
    is_standby_available,
    payment_deadline_text,
    reserve_consent,
    reserve_once,
)

from korail_mobile_api import (
    KorailClient,
    KorailPassengerCounts,
    KorailSeatClass,
    KorailSession,
    KorailSessionExpiredError,
    KorailTransportError,
    TrainSummary,
)
from korail_mobile_api.mutation_parsers import parse_reservation_hold_response


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
    standing: str | None = None,
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
        # 주지 않으면 extra 로 들어온 값을 덮어쓰지 않습니다 — 예전 픽스처가
        # h_stnd_rsv_cd 를 그쪽으로 넣습니다.
        **({"h_stnd_rsv_cd": standing} if standing is not None else {}),
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
    # 구간마다 두 번 — 첫 페이지, 그리고 마지막 행의 시각부터 한 번 더.
    # 서버가 커서를 주지 않아도 뒤쪽 열차를 놓치지 않으려는 것입니다.
    assert recorder.count(SEARCH) == 4
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
    assert reserve_consent(live=False).dry_run is True
    assert reserve_consent(live=True).dry_run is False


def test_no_module_in_the_app_names_a_money_moving_call():
    """결제·환불은 어디에서도 열지 않습니다.

    취소는 예외입니다 — 사람이 [잡은 예약] 목록에서 직접 요청한 것입니다.
    그래도 **한 곳에서만** 열립니다: 다른 시험
    (:func:`test_only_ui_can_open_a_cancel_request`)이 그 한 곳이 ``ui.py``
    뿐이고 자동예매는 절대 손대지 않는다는 것을 확인합니다.
    """
    for path in sorted(APP_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "allow_payment=True",
            "allow_refund=True",
            "real_card_acknowledged",
            "CardPayment",
            "pay_with_card",
            "pay_with_fake_card",
            ".refund(",
        ):
            assert forbidden not in source, f"{path.name}: {forbidden}"
        if path.name == "ui.py":
            continue
        for forbidden in ("allow_cancel=True", "cancel_unpaid_hold("):
            assert forbidden not in source, f"{path.name}: {forbidden}"


def test_only_ui_can_open_a_cancel_request():
    """취소 consent 는 ``ui.py`` 딱 한 자리에서만 열립니다.

    이 프로그램은 지금까지 예약(reserve) 하나만 열었습니다. 사람이 [잡은
    예약] 목록에서 명시적으로 요청한 취소만 예외로 두되, **자동으로 도는
    감시가 절대 부를 수 없는 자리**에 있어야 합니다 — 그래서 ``ui.py`` 안,
    사람이 단추를 눌러야 닿는 함수 하나로 한정합니다.
    """
    ui_source = _ui_source()
    assert ui_source.count("allow_cancel=True") == 1
    assert ui_source.count("cancel_unpaid_hold(") == 1

    consent_fn = _ui_function("cancel_consent")
    assert "allow_cancel=True" in consent_fn
    assert "assert not consent.allow_reserve" in consent_fn
    assert "assert not consent.allow_payment" in consent_fn
    assert "assert not consent.allow_refund" in consent_fn
    assert "assert not consent.allow_cart" in consent_fn

    caller = _ui_function("on_cancel_hold")
    assert "cancel_consent()" in caller
    # 사람이 확인 창을 지나야만 나갑니다.
    assert "messagebox.askyesno(" in caller
    # 취소에 필요한 원본이 없으면(``hold_response is None``) 나가지 않습니다.
    assert "original = held.hold_response" in caller
    assert "if original is None:" in caller
    # 자동예매 파일의 자기 약속은 그대로입니다 — reserve_consent() 는
    # allow_cancel 이 꺼져 있는지 단언만 할 뿐 켜지 않습니다.
    booker_source = (APP_DIR / "korail_booker" / "autobook.py").read_text(
        encoding="utf-8"
    )
    assert "allow_cancel=True" not in booker_source
    assert "cancel_unpaid_hold(" not in booker_source


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
    assert "예약 폼이 요구하는 값" in result.message
    # 원인을 지어내지 않습니다 — 예전 문구는 SRT 라고 단정했습니다.
    assert "SRT" not in result.message and "수서" not in result.message


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


def test_the_cart_is_never_touched():
    """장바구니 경로를 걷어냈습니다.

    예약을 잡으면 결제 기한 안에 결제해야 하는 것은 담든 안 담든 같습니다.
    담아서 무엇이 좋아지는지는 이 저장소가 확인한 적이 없고, 확인하지 못한
    것을 위해 요청을 하나 더 내보낼 이유가 없습니다.
    """
    recorder = _Recorder(
        {SEARCH: _search_reply([_row("00101", general="11")]), RESERVE: _reserve_reply()}
    )
    _booker(recorder, [_journey(_summary(general="11"))], live=True).run(
        threading.Event()
    )

    assert recorder.count(CART) == 0
    for path in sorted(APP_DIR.rglob("*.py")):
        assert "allow_cart=True" not in path.read_text(encoding="utf-8"), path


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
    # 여러 묶음을 따로 돌리면 앞에 꼬리표가 붙습니다. 그래도 회차는 회차입니다.
    assert LF.format_entry("[A] [3] 387(GE:매진)", stamp="09:00:00").blank_before
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
    # 묶음마다 꼬리표를 단 기록기를 씁니다.
    assert "log=self._tagged_log(tag)," in source


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
    # 모달은 큐를 비운 뒤에 엽니다 — _drain 안에서 바로 열면 화면이 멈춥니다.
    assert "self._show_later('info'" in body


def test_the_program_always_reserves_for_real_and_still_gates_it():
    """미리보기 스위치를 없앴습니다 — 늘 진짜로 보냅니다.

    켜는 것을 잊고 미리보기를 진짜라고 믿는 일이 실제로 생겼고, 이 프로그램을
    켜는 이유가 진짜 예약입니다. 대신 시작할 때 확인 창이 뜨고, 로그인하지
    않았으면 시작 자체가 막힙니다. 둘 중 하나라도 사라지면 실수 한 번이 진짜
    예약이 되므로 함께 고정합니다.
    """
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")

    assert "live=True," in source
    assert "self.live_mode" not in source
    assert "if not self._confirm_live(targets, options):" in source
    assert "if not self.logged_in:" in source


def test_the_settings_file_stores_neither_the_live_switch_nor_the_cart():
    """둘 다 화면에서 사라졌습니다. 남은 필드는 되살아날 자리가 됩니다."""
    stored = dataclasses.asdict(ST.Settings())
    assert not [name for name in stored if "live" in name or "cart" in name]


# --- 페이지 끝까지 훑기 --------------------------------------------------------


def test_a_page_without_a_cursor_is_not_the_end_of_the_day():
    """서버가 "다음 있음" 을 주지 않아도 뒤쪽 열차를 놓치지 않습니다.

    동탄→대구(2026-09-12)를 00:00~23:30 으로 조회했더니 13:08 출발까지 열 편만
    나왔습니다. 앱에는 22:00 까지 나오는 구간입니다. 그 응답에 커서가 없었고,
    예전에는 거기서 끝냈습니다. 이제 마지막 행의 시각부터 다시 묻습니다.
    """
    recorder = _Recorder(
        sequences={
            SEARCH: [
                _search_reply([_row("00301", departure_time="054700")]),
                _search_reply([_row("00351", departure_time="184200")]),
                _search_reply([_row("04059", departure_time="204600")]),
            ]
        }
    )

    found = S.search_journeys(
        _client(recorder),
        _request(include_direct=True, include_transfer=False, max_pages=3),
    )

    numbers = sorted(journey.legs[0].train_no for journey in found)
    assert numbers == ["00301", "00351", "04059"], numbers


#: 동탄→대구 2026-09-12 환승, 앱 화면에 찍힌 그대로.
#: (1구간 열차·출발·도착, 2구간 열차·출발·도착)
DONGTAN_DAEGU = (
    ("00301", "054700", "062800", "01195", "071500", "090900"),
    ("00391", "064700", "072800", "01001", "075300", "093400"),
    ("00381", "071100", "075200", "01003", "081500", "100700"),
    ("00309", "074400", "082500", "01005", "085100", "102700"),
    ("00309", "074400", "082500", "01151", "085900", "105800"),
    ("00313", "082200", "091300", "01153", "092800", "111800"),
    ("00371", "092300", "100400", "01007", "104000", "121700"),
    ("04021", "104800", "112900", "01009", "121800", "135900"),
    ("00327", "130800", "134200", "01013", "135600", "153100"),
    ("00327", "130800", "134200", "01015", "141600", "155900"),
    ("00331", "134700", "143300", "01157", "150600", "171100"),
    ("00339", "155100", "163100", "01161", "165000", "183400"),
    ("00341", "160900", "164900", "01019", "173000", "191100"),
    ("00395", "165200", "173200", "01197", "180200", "201500"),
    ("00345", "171000", "175600", "01021", "183500", "201000"),
    ("00351", "184200", "192800", "01163", "193900", "213900"),
    ("00377", "191700", "195800", "04304", "202200", "221400"),
    ("00387", "194200", "202700", "01111", "211100", "224800"),
    ("04059", "204600", "213700", "01025", "220000", "234100"),
)


def _cursorless_server(pairs, page: int = 10) -> tuple[Callable, list[str]]:
    """``txtGoHour`` 이후 여정을 열 개까지 주고 **커서는 주지 않는** 서버.

    사용자가 받은 응답이 이런 모양이었다고 봅니다. 옛 코드가 두 페이지까지
    보게 돼 있었는데도 열 편에서 멈췄고, 둘째 페이지가 비었다면 남겼을
    "결과가 없습니다" 기록도 없었기 때문입니다 — 즉 둘째 페이지를 아예 묻지
    않았고, 그러려면 커서가 ``None`` 이어야 합니다.
    """
    asked: list[str] = []

    def server(request: httpx.Request) -> httpx.Response:
        form = urllib.parse.parse_qs(request.content.decode())
        since = form.get("txtGoHour", ["000000"])[0]
        asked.append(since)
        rows: list[dict[str, Any]] = []
        for first, out, back, second, out2, back2 in [
            pair for pair in pairs if pair[1] >= since
        ][:page]:
            rows.append(
                _row(first, departure="동탄", arrival="대전", departure_code="0507",
                     arrival_code="0010", departure_time=out, arrival_time=back)
            )
            rows.append(
                _row(second, departure="대전", arrival="대구", departure_code="0010",
                     arrival_code="0015", departure_time=out2, arrival_time=back2)
            )
        return httpx.Response(200, json=_search_reply(rows))

    return server, asked


def test_the_reported_dongtan_daegu_day_comes_back_whole():
    """실제로 겪은 누락. 열 편에서 끊기던 하루가 끝까지 나와야 합니다.

    옛 코드는 여기서 열 편(13:08 출발까지)만 찾았습니다 — 화면에 보인 것과
    같습니다.
    """
    server, asked = _cursorless_server(DONGTAN_DAEGU)
    client = KorailClient(transport=httpx.MockTransport(server))
    client.session.current = KorailSession(jsessionid="synthetic-session")

    found = S.search_journeys(
        client,
        _request(
            departure="동탄", arrival="대구", date="20260912",
            include_direct=False, include_transfer=True,
            max_transfer_minutes=0, max_pages=S.DEFAULT_MAX_PAGES,
        ),
    )

    pairs = {(j.legs[0].train_no, j.legs[1].train_no) for j in found}
    assert pairs == {(a, b) for a, _o, _b, b, _o2, _b2 in DONGTAN_DAEGU}
    # 하루를 다 훑고도 요청은 몇 번뿐입니다.
    assert len(asked) == 4, asked


def test_the_walk_stops_when_the_clock_stops_moving():
    """같은 시각을 다시 묻지 않습니다 — 안 그러면 한 페이지를 영원히 돕니다."""
    recorder = _Recorder({SEARCH: _search_reply([_row("00301", departure_time="054700")])})

    found = S.search_journeys(
        _client(recorder),
        _request(include_direct=True, include_transfer=False, max_pages=3),
    )

    # 첫 물음, 그리고 05:47 부터 한 번 더. 그 다음은 시각이 그대로라 멈춥니다.
    assert recorder.count(SEARCH) == 2
    assert len(found) == 1  # 같은 열차가 두 번 와도 하나로 셉니다


def test_the_next_ask_starts_at_the_same_minute_not_a_minute_later():
    """1분을 더하면 같은 분에 떠나는 다른 여정을 건너뜁니다.

    실제로 그런 줄이 옵니다 — 같은 열차 309(07:44)가 뒤 구간만 달리해 두 번.
    """
    recorder = _Recorder(
        sequences={
            SEARCH: [
                _search_reply([_row("00309", departure_time="074400")]),
                _search_reply([_row("00313", departure_time="074400")]),
            ]
        }
    )

    found = S.search_journeys(
        _client(recorder),
        _request(include_direct=True, include_transfer=False, max_pages=3),
    )

    assert sorted(journey.legs[0].train_no for journey in found) == ["00309", "00313"]


# --- 환승 한 구간만 매진일 때 ---------------------------------------------------


def _half_sold_out() -> J.Journey:
    """앞 구간은 매진, 뒤 구간은 예약가능. 환승에서 흔한 모양입니다."""
    return _journey(
        _summary(train_no="00009", general="13", general_name="매진"),
        _summary(train_no="00503", general="11", general_name="45,300원"),
        source=J.JourneySource.SERVER_TRANSFER,
    )


def test_one_sold_out_leg_makes_the_whole_transfer_unbookable():
    """두 구간을 한 PNR 로 잡습니다 — 한쪽만 타는 예약은 없습니다."""
    state = _half_sold_out().seat_state(KorailSeatClass.GENERAL)

    assert state.available is False
    assert state.sold_out is True
    assert state.status == "매진"
    assert _half_sold_out().bookable_seat_class(J.SeatPreference.ANY) is None


def test_the_parent_row_says_sold_out_but_each_leg_still_shows_its_own_state():
    """부모 줄만으로는 어느 구간이 막혔는지 모릅니다. 구간 줄이 그것을 말합니다."""
    journey = _half_sold_out()
    per_leg = [
        J.Journey(legs=(leg,), source=journey.source).seat_text(KorailSeatClass.GENERAL)
        for leg in journey.legs
    ]

    assert journey.seat_text(KorailSeatClass.GENERAL).startswith("매진")
    assert per_leg[0].startswith("매진")
    assert per_leg[1].startswith("예약가능")


def test_the_leg_rows_are_not_blank_where_the_seat_columns_are():
    """구간 줄의 좌석 칸이 비어 있으면 어느 구간이 매진인지 볼 방법이 없습니다."""
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    inserts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_insert_row"
    ]
    assert len(inserts) == 1
    body = ast.unparse(inserts[0])
    assert "Journey(legs=(leg,)" in body
    assert "alone.seat_text(KorailSeatClass.GENERAL)" in body


def test_the_booker_waits_for_both_legs_instead_of_grabbing_one():
    """한 구간이라도 닫혀 있으면 아무것도 보내지 않고 계속 지켜봅니다."""
    request = _request()
    recorder = _Recorder(
        {
            SEARCH: _search_reply(
                [
                    _row("00009", general="13", general_name="매진"),
                    _row("00503", general="11", general_name="45,300원"),
                ]
            )
        }
    )
    booker = AutoBooker(
        _client(recorder),
        [_target(_half_sold_out(), request)],
        BookingOptions(poll_interval_s=10.0, live=True, watch_minutes=0),
        log=lambda message: None,
    )

    booker._act_on(1, [(booker.targets[0], _half_sold_out())])

    assert recorder.count(RESERVE) == 0


# --- 환승역 목록과 조회 중지 ------------------------------------------------------


def test_a_search_never_overwrites_hand_picked_transfer_stations():
    """목록은 두 모드 모두에서 사람이 손댄 것입니다.

    직접 지정에서는 고른 역이 곧 조회 대상이고, 서버 추천에서는 고른 역이
    결과를 거르는 필터입니다. 어느 쪽이든 [조회] 를 눌렀다고 목록이 서버 후보
    전체로 되돌아가면 빼 둔 역이 조용히 되살아납니다 — 실제로 그랬습니다.
    """
    body = _ui_function("_server_candidates_loaded")

    # 모드를 가리지 않습니다. 목록이 있으면 그것으로 끝입니다.
    assert "TRANSFER_CUSTOM" not in body
    assert "if self.transfer_names():" in body
    # 그래도 받아 온 것은 (검증) 표시에 씁니다.
    assert "self._server_stations = set(names)" in body
    assert "self._redraw_transfer_marks()" in body


def test_the_manual_refresh_button_adds_and_never_deletes():
    """[구간 후보 갱신] 이 손으로 넣은 역을 지우면 안 됩니다.

    서버 후보에 없는 역을 넣을 수 있게 해 놓고, 후보를 한 번 더 불러오면 그
    역이 사라졌습니다. 이제는 **합칩니다** — 있던 것은 그대로 두고 없던 것만
    붙입니다. 지우는 일은 [빼기] 와 [목록 비우기] 가 합니다.
    """
    body = _ui_function("_transfer_stations_loaded")
    assert "existing = list(self.transfer_names())" in body
    assert "added = [name for name in names if name not in existing]" in body
    assert "merged = existing + added" in body

    source = _ui_source()
    assert 'text="비우기", width=6' in source
    clear = _ui_function("clear_transfer_stations")
    assert "self._fill_transfer_stations([], select_all=False, keep=set())" in clear


def test_a_station_the_server_also_offers_is_marked():
    """직접 지정한 역이 서버 추천과 겹치는지가, 서버가 받아 줄 가능성의 단서입니다."""
    source = _ui_source()
    assert 'VERIFIED_MARK = " (검증)"' in source
    assert "mark = VERIFIED_MARK if name in self._server_stations else \"\"" in source


def test_plain_station_strips_the_mark():
    source = _ui_source()
    tree = ast.parse(source)
    body = next(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_plain_station"
    )
    assert "VERIFIED_MARK" in body
    # 고른 역을 읽는 곳이 이 함수를 지납니다.
    assert "_plain_station(self.transfer_list.get(index))" in _ui_function(
        "selected_transfer_stations"
    )


def test_a_running_search_can_be_stopped():
    """하루치를 훑느라 요청이 여러 번 나갑니다. 끝까지 기다릴 이유가 없습니다."""
    source = _ui_source()
    assert 'text="조회 중지"' in source

    stop = _ui_function("on_stop_search")
    assert "self._search_cancelled.add(self._search_token)" in stop

    search = _ui_function("on_search")
    # 방향 사이에서 확인하고, 버린 조회의 결과는 화면에 올리지 않습니다.
    assert "if cancelled():" in search
    assert "if not cancelled():" in search


# --- 좌석 등급과 입석 ------------------------------------------------------------


def test_there_is_no_standing_only_seat_class_to_offer():
    """입석만 잡는 길이 라이브러리에 없습니다. 없는 것을 화면에 두지 않습니다.

    ``KorailSeatClass`` 는 일반실("1")과 특실("2") 뿐입니다. ``txtStndFlg`` 는
    고르는 값이 아니라 **계산되는 값**이고(일반실이면서 좌석 매진 + 입석 재고
    열림), 즉시예약(1101)은 좌석 코드가 ``"11"`` 일 때만 통과하므로 그 조합이
    성립하지 않습니다. 남은 길은 입석+좌석 병합(1202)뿐인데, 이 저장소는 그것을
    실서버로 보낸 적이 없습니다.
    """
    assert [member.name for member in KorailSeatClass] == ["GENERAL", "SPECIAL"]
    assert ui_seat_labels() == {"무관", "일반실", "특실"}


def test_the_screen_says_why_standing_is_not_a_choice():
    source = _ui_source()
    assert "입석은 따로 고를 수 없습니다" in source


def test_standing_shows_up_in_the_results_when_the_server_offers_it():
    """고를 수는 없어도 **있는지는** 보여야 합니다."""
    open_standing = _journey(_summary(general="13", standing="11"))
    assert "입석" in open_standing.extras()

    closed = _journey(_summary(general="11", standing="13"))
    assert "입석" not in closed.extras()

    # 환승은 모든 구간에 열려 있을 때만 셉니다.
    half = _journey(
        _summary(train_no="00009", general="13", standing="11"),
        _summary(train_no="00503", general="13", standing="13"),
        source=J.JourneySource.SERVER_TRANSFER,
    )
    assert "입석" not in half.extras()


def test_the_target_table_carries_the_seat_columns_too():
    """담고 나서 좌석이 어땠는지 다시 위 표를 뒤지게 하면 안 됩니다."""
    source = _ui_source()
    for name in ("일반실", "특실", "입석·자유석·대기"):
        assert f'("{name}"' in source, name

    body = _ui_function("sync_target_list")
    assert "target.journey.seat_text(KorailSeatClass.GENERAL)" in body
    assert "target.journey.seat_text(KorailSeatClass.SPECIAL)" in body
    assert "target.journey.extras()" in body


# --- 바로 예약 ------------------------------------------------------------------


def test_reserve_now_only_looks_at_the_target_list():
    """단추만 옮기고 동작을 두면, 4번에 있는 단추가 3번 표를 잡습니다."""
    body = _ui_function("on_reserve_now")

    assert "self.selected_indices()" in body
    assert "self.targets[index]" in body
    # 조회 결과를 보던 옛 길이 남아 있으면 안 됩니다.
    assert "selected_results" not in body


def test_reserve_now_needs_an_explicit_selection():
    """아무것도 안 고른 것을 '전부'로 읽으면 실수 한 번이 여러 건의 예약입니다."""
    body = _ui_function("on_reserve_now")
    assert "if not indices:" in body
    assert "예매 대상에서 잡을 열차를 고르세요" in body


def test_reserve_now_refuses_two_of_the_same_direction():
    """같은 여정을 두 번 잡는 것은 중복 예약입니다."""
    body = _ui_function("on_reserve_now")
    assert "directions.count(d) > 1" in body
    assert "중복 예약" in body


def test_reserve_now_keeps_going_when_one_of_several_fails():
    """여럿을 골랐다면 그중 되는 것은 잡히는 편이 낫습니다."""
    body = _ui_function("on_reserve_now")
    assert "continue" in body
    assert "self._reserve_now_finished" in body


def test_the_sold_out_message_points_at_the_right_button_now():
    """[바로 예약] 이 이미 예매 대상에 있으므로 "담기" 로 보내면 안 됩니다."""
    body = _ui_function("on_reserve_now")

    assert "[고른 것만 시작]" in body
    assert "[담기] 로 예매 대상에 넣고" not in body


# --- 로그인 팝업 ----------------------------------------------------------------


def test_the_program_asks_to_log_in_before_anything_else():
    """켜자마자 물어야, 조회부터 눌렀다가 "예약이 왜 안 되지" 로 가지 않습니다."""
    source = _ui_source()
    assert "self.root.after(300, self.open_login)" in source


def test_the_login_popup_blocks_the_main_window():
    """뒤에서 조회를 눌러 놓고 로그인 창을 찾는 일이 없어야 합니다."""
    body = _ui_function("open_login")
    assert "window.grab_set()" in body
    assert "window.transient(self.root)" in body
    # 닫아도 프로그램은 굴러갑니다 — 조회만 되는 상태로.
    assert "window.protocol('WM_DELETE_WINDOW', skip)" in body
    assert "비로그인" in body


def test_logging_in_is_blocked_while_a_watch_is_running():
    """자동예매 도중 다른 아이디로 로그인하면 그 감시가 쓰는 목록이 그 밑에서
    비워집니다 — 로그아웃과 같은 이유로 같은 방비를 둡니다."""
    body = _ui_function("open_login")
    assert body.index("self.any_running()") < body.index("tk.Toplevel(self.root)")
    assert "자동예매가 돌고 있습니다" in body


def test_a_login_popup_hint_says_no_hyphens_for_phone_numbers():
    """휴대폰번호 로그인은 하이픈을 빼야 합니다 — 안 그러면 서버가 거절합니다."""
    body = _ui_function("open_login")
    assert "하이픈" in body


def test_skipping_login_clears_the_session_lists():
    """비로그인으로 시작하면 이전 조회·예매 대상·잡은 예약이 다 비어야
    합니다 — 안 그러면 남의(또는 지난) 목록이 새 세션에 남습니다."""
    body = _ui_function("open_login")
    skip_start = body.index("def skip()")
    skip_body = body[skip_start : body.index("def attempt()")]
    assert "self._reset_session_lists()" in skip_body
    assert "self.any_running()" in skip_body


def test_a_failed_login_keeps_the_popup_open():
    """실패했는데 창이 닫히면 다시 칠 곳이 없습니다."""
    body = _ui_function("open_login")
    assert "note.set(message)" in body
    assert "login_button.configure(state='normal')" in body


def test_the_login_row_has_no_password_box():
    """비밀번호 칸은 팝업에만 있습니다. 본 화면에 남겨 두면 두 곳이 됩니다."""
    body = _ui_function("_build_login")
    assert "show='*'" not in body
    assert "self.login_pw" not in body


def test_the_buttons_match_the_login_state():
    """로그아웃할 것이 없는데 단추가 있으면 눌러 보게 됩니다."""
    body = _ui_function("sync_login_buttons")

    assert "'다른 아이디로 로그인'" in body
    assert "'로그인'" in body
    assert "self.logout_button.grid_remove()" in body
    assert "self.logout_button.grid()" in body

    # 상태가 바뀌는 세 곳이 모두 단추를 다시 맞춥니다.
    for name in ("_login_succeeded", "_login_failed", "on_logout"):
        assert "self.sync_login_buttons()" in _ui_function(name), name


# --- 화면 글과 손놀림 -----------------------------------------------------------


def test_no_screen_text_carries_markdown_asterisks():
    """Tk 은 마크다운을 모릅니다 — ``**`` 가 그대로 찍힙니다.

    실제로 확인 창에 "**방향마다 한 건씩**" 이 별표째로 나왔습니다.
    """
    source = _ui_source()
    tree = ast.parse(source)
    shown: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = ast.unparse(node.func)
        # 화면에 나가는 것: 위젯의 text= 와 messagebox 의 인자.
        if called.startswith("messagebox."):
            shown += [
                arg.value for arg in node.args if isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
            ]
        for keyword in node.keywords:
            if keyword.arg == "text" and isinstance(keyword.value, ast.Constant):
                value = keyword.value.value
                if isinstance(value, str):
                    shown.append(value)

    offenders = [text for text in shown if "**" in text]
    assert not offenders, offenders


def test_the_confirmation_says_the_conditions_it_is_starting_with():
    """무엇에 동의하는지 창이 말해야 합니다 — 몇 초마다, 얼마 동안."""
    body = _ui_function("_confirm_live")

    assert "options.poll_interval_s" in body
    assert "options.watch_minutes" in body
    assert "options.allow_standby" in body
    # 담긴 것이 많으면 다 적지 않고 몇 편 더 있는지 말합니다.
    assert "외 {len(targets) - len(shown)}편" in body


def test_double_click_adds_from_the_results_and_removes_from_the_targets():
    source = _ui_source()

    assert 'tree.bind("<Double-Button-1>", self._result_double_clicked)' in source
    assert (
        'self.target_list.bind("<Double-Button-1>", self._target_double_clicked)'
        in source
    )
    assert "self.add_targets()" in _ui_function("_result_double_clicked")
    assert "self.remove_targets()" in _ui_function("_target_double_clicked")


def test_enter_is_bound_per_field_not_to_the_whole_window():
    """창 전체에 걸면 아이디를 치다 Enter 를 눌러도 조회가 돌았습니다."""
    source = _ui_source()

    assert 'self.root.bind("<Return>"' not in source
    assert "for widget in self.query_fields:" in source
    # 로그인 칸의 Enter 는 팝업 안에서 답니다.
    assert 'field.bind("<Return>", lambda _event: attempt())' in source


def test_the_add_button_sits_with_the_results_and_reserve_with_the_targets():
    """담는 것은 조회 결과에서, 예약은 담아 둔 것 중에서."""
    results = _ui_function("_build_results")
    targets = _ui_function("_build_targets")

    assert "예매 대상에 담기" in results and "command=self.add_targets" in results
    assert "바로 예약" in targets and "command=self.on_reserve_now" in targets
    assert "바로 예약" not in results


# --- 작은 단추들 ----------------------------------------------------------------


def test_selecting_all_train_kinds_turns_every_one_on():
    """하나만 빼고 보려면 전부 켜고 하나만 끄는 편이 빠릅니다."""
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    body = next(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "select_all_train_kinds"
    )

    assert "var.set(True)" in body
    assert "self.sync_train_kinds()" in body
    assert 'text="모두 선택"' in source


def test_clearing_the_results_leaves_the_targets_alone():
    """담아 둔 것까지 사라지면 곤란합니다. 표만 비웁니다."""
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    body = next(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "clear_results"
    )

    assert "tree.delete(*tree.get_children())" in body
    assert "self.results = []" in body
    assert "self.item_journeys.clear()" in body
    # 예매 대상과 감시는 건드리지 않습니다.
    assert "self.targets" not in body
    assert "self.watches" not in body


# --- 감시 여럿 따로 돌리기 ------------------------------------------------------


def ui_seat_labels() -> set[str]:
    """화면이 내놓는 좌석 선택지의 이름. 원문에서 읽습니다(tkinter 없이)."""
    tree = ast.parse((APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "SEAT_CHOICES" not in names:
            continue
        return {
            pair.elts[0].value
            for pair in node.value.elts  # type: ignore[attr-defined]
            if isinstance(pair, ast.Tuple) and isinstance(pair.elts[0], ast.Constant)
        }
    raise AssertionError("SEAT_CHOICES 를 찾지 못했습니다")


def _ui_source() -> str:
    return (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")


def _ui_function(name: str) -> str:
    tree = ast.parse(_ui_source())
    return next(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_watches_are_remembered_by_journey_not_by_row_number():
    """자리 번호로 기억하면 사이에서 하나를 빼는 순간 전부 어긋납니다."""
    source = _ui_source()
    assert "keys: frozenset" in source
    assert "keys=frozenset(target.journey.key() for target in targets)" in source


def test_starting_again_never_watches_the_same_journey_twice():
    """같은 열차를 두 묶음이 노리면 예약이 두 번 나갑니다."""
    body = _ui_function("_start_targets")
    assert "watching = self.watching_keys()" in body
    assert "if t.journey.key() not in watching" in body
    # 방향으로도 막습니다 — 묶음끼리는 서로를 모릅니다.
    assert "busy = self._busy_directions()" in body
    assert "if t.direction not in busy" in body


def test_selected_only_start_and_stop_both_exist():
    """담긴 것 전부와 고른 것만 — 넷으로 나뉩니다."""
    source = _ui_source()
    for name in ("on_start_selected", "on_stop_selected"):
        assert f"def {name}" in source
    assert 'text="고른 것만 시작"' in source
    assert 'text="고른 것만 중지"' in source


def test_a_watched_target_cannot_be_removed_from_the_list():
    """빼도 그 묶음은 계속 노립니다 — 목록에 없는데 예약이 잡히면 알 수 없습니다."""
    body = _ui_function("remove_targets")
    assert "watching = self.watching_keys()" in body
    assert "감시 중인 열차는 뺄 수 없습니다" in body

    cleared = _ui_function("clear_targets")
    assert "self.any_running()" in cleared


def test_every_booking_log_line_says_which_watch_it_came_from():
    """여럿이 돌면 꼬리표 없이는 어느 줄이 어느 묶음 것인지 모릅니다."""
    body = _ui_function("_tagged_log")
    # 곁가지 줄(공백으로 시작)은 그 성질을 지켜야 들여쓰기가 깨지지 않습니다.
    # ast.unparse 는 따옴표를 홑따옴표로 바꿉니다.
    assert "message.startswith(' ')" in body
    assert "f'    [{tag}] {message.strip()}'" in body
    assert "f'[{tag}] {message}'" in body


def test_the_target_list_shows_what_is_running():
    body = _ui_function("sync_target_list")
    state = _ui_function("_target_state")
    assert "▶ 감시 중" in state and "대기" in state
    # 도는 것과 안 도는 것을 색으로도 가릅니다.
    assert "'watching' if watch else 'idle'" in body
    # 다시 그린 뒤에도 고른 줄은 그대로 있어야 합니다.
    assert "self.target_list.selection_add(item)" in body


def test_each_target_row_shows_its_own_interval_and_countdown():
    """여럿을 돌리면 묶음마다 주기와 남은 시간이 다릅니다."""
    body = _ui_function("sync_target_list")
    assert "watch.options.poll_interval_s" in body
    assert "watch.remaining(now)" in body

    source = _ui_source()
    assert '("조회 주기", 80, "center")' in source
    assert '("남은 감시", 110, "center")' in source
    # 1초 시계가 그 칸을 다시 씁니다.
    tick = _ui_function("_tick_holds")
    assert "self._watch_of(self.targets[index])" in tick


def test_conditions_are_read_at_start_and_a_restart_is_offered():
    """도는 중에 조건을 고쳐도 그 묶음은 옛 조건으로 돕니다.

    화면과 실제가 어긋나는데 화면이 말하지 않으면 사람이 속습니다.
    """
    source = _ui_source()
    assert "options: BookingOptions" in source  # 시작할 때 읽은 것을 들고 있다
    assert 'text="조건 바꿔 재시작"' in source

    body = _ui_function("restart_selected")
    assert "watch.session.stop()" in body
    # 멈춤은 즉시 걸리지 않습니다. 멈춘 것을 확인하고 다시 겁니다.
    # ast.unparse 는 제너레이터에 괄호를 하나 더 씌웁니다.
    assert "any((watch.running for watch in stopping))" in body
    # 멈춘 뒤에 선택을 다시 읽지 않습니다 — 그 사이에 표가 다시 그려져
    # 선택이 달라져 있을 수 있습니다.
    assert "picked = self.selected_targets()" in body
    assert "self._start_targets(picked)" in body


def test_a_transfer_station_can_be_taken_out_again():
    """넣기만 되고 빼기가 없으면 목록을 통째로 다시 불러와야 합니다."""
    source = _ui_source()
    assert "def remove_transfer_station" in source
    assert 'text="빼기", width=5, command=self.remove_transfer_station' in source

    body = _ui_function("remove_transfer_station")
    assert "self.transfer_list.delete(index)" in body
    assert "self.mark_stale()" in body


# --- 텔레그램 알림 --------------------------------------------------------------


def _announcing(recorder: _Recorder, targets, **options):
    """알림 문구를 모으며 한 번 돌립니다."""
    sent: list[str] = []
    booker = AutoBooker(
        _client(recorder),
        [_target(journey, _request()) for journey in targets],
        BookingOptions(poll_interval_s=10.0, watch_minutes=0, **options),
        log=lambda message: None,
        notify=sent.append,
    )
    return booker, sent


def test_the_start_and_the_end_are_always_announced():
    """잡았을 때만 알리면 조용한 것이 '아직' 인지 '안 돌고 있음' 인지 모릅니다."""
    recorder = _Recorder(
        {SEARCH: _search_reply([_row("00101", general="11")]), RESERVE: _reserve_reply()}
    )
    booker, sent = _announcing(recorder, [_journey(_summary(general="11"))], live=True)

    result = booker.run(threading.Event())

    assert result.outcome is Outcome.HELD
    assert sent[0].startswith("▶️ 자동예매 시작")
    assert sent[-1].startswith("✅ 자동예매 종료")


def test_the_announcement_says_which_journeys_are_being_watched():
    """무엇을 지켜보는지 적지 않으면 여러 개를 돌릴 때 알림이 쓸모없습니다."""
    recorder = _Recorder({SEARCH: _search_reply([])})
    booker, sent = _announcing(
        recorder,
        [_journey(_summary(train_no="00101")), _journey(_summary(train_no="00103"))],
        live=True,
    )

    # 감시 시간 무제한이라 결과가 없으면 영영 돕니다. 시작 알림은 고리에
    # 들어가기 전에 나가므로, 멈춤을 미리 걸어 두고 확인합니다.
    stop = threading.Event()
    stop.set()
    booker.run(stop)

    # 열차 번호는 사람이 보는 대로 앞의 0 을 뗀 모양입니다(코레일 화면과 같이).
    assert "101" in sent[0] and "103" in sent[0]
    assert "2편 감시" in sent[0]


def test_a_stopped_run_is_announced_too():
    """중지도 끝입니다. 조용히 멈추면 멈춘 줄을 모릅니다."""
    recorder = _Recorder({SEARCH: _search_reply([])})
    booker, sent = _announcing(recorder, [_journey(_summary())], live=True)
    stop = threading.Event()
    stop.set()

    booker.run(stop)

    assert sent[-1].startswith("⏹️ 자동예매 종료")


def test_the_chat_id_is_a_number_not_a_bot_name():
    """``@봇이름`` 은 이 칸의 모양이 아닙니다.

    실제로 ``@my_korail_alarm_bot`` 을 넣었다가 [내 대화 ID 찾기] 를 누르니
    숫자로 바뀌어 헷갈렸다는 보고가 있었습니다. 그 단추가 칸을 보지 않고
    덮어쓰기 때문인데, 화면이 그것을 말하지 않았습니다.
    """
    assert N.looks_like_chat_id("7073365948")
    assert N.looks_like_chat_id("-1001234567890")  # 그룹은 음수
    assert not N.looks_like_chat_id("@my_korail_alarm_bot")
    assert not N.looks_like_chat_id("")
    assert not N.looks_like_chat_id("123abc")


def test_the_dialog_says_the_button_overwrites_whatever_is_typed():
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")

    # Tk 은 마크다운을 모르므로 별표 없이 적습니다.
    assert "뭐가 적혀 있든 보지 않고 덮어씁니다" in source
    assert "봇 이름(@my_korail_alarm_bot 같은 것)을 넣는 칸이 아닙니다" in source
    # 숫자가 아닌 값으로는 보내지도 저장하지도 않습니다.
    assert "def bad_chat_id()" in source
    assert "if bad_chat_id():" in source


def test_the_resolved_chat_says_whose_it_is():
    """숫자 하나만 돌려주면 그 숫자가 무엇인지 알 수 없습니다."""
    payload = {
        "ok": True,
        "result": [
            {"message": {"chat": {"id": 7073365948, "first_name": "윤한", "username": "yh"}}}
        ],
    }

    with _telegram(lambda _r: httpx.Response(200, json=payload)) as bot:
        found = bot.resolve_chat()

    assert found is not None
    assert found.chat_id == "7073365948"
    # title → username → first_name 순서로 사람이 알아보는 이름을 고릅니다.
    assert found.title == "yh"


def test_a_chat_without_a_name_still_gives_its_number():
    payload = {"ok": True, "result": [{"message": {"chat": {"id": -100}}}]}

    with _telegram(lambda _r: httpx.Response(200, json=payload)) as bot:
        found = bot.resolve_chat()

    assert found is not None and found.chat_id == "-100" and found.title == ""


def test_the_token_dialog_walks_through_the_whole_setup():
    """BotFather 답장만 보고는 어느 값을 어디에 넣는지 헷갈립니다."""
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")

    for step in ("1단계 — 봇 만들기", "2단계 — 토큰 붙여넣기",
                 "3단계 — 봇에게 먼저 말 걸기", "4단계 — 대화 ID 채우기"):
        assert step in source, step
    assert "@BotFather" in source and "/newbot" in source
    assert "Use this token to access the HTTP API" in source
    # 봇에게 먼저 말을 걸지 않으면 대화 ID 를 못 얻습니다. 여기서 막힙니다.
    assert "먼저 말을 건 적이 없는 봇에게 대화 ID 를 주지" in source
    # 잘 안 될 때 어디를 고쳐야 하는지 갈라 줍니다.
    assert "잘 안 될 때:" in source


def test_the_example_token_is_not_a_real_one():
    """예시로 진짜 토큰을 적어 두면 그대로 붙여 넣는 사람이 생깁니다.

    처음 판은 사용자 스크린샷의 봇 번호를 그대로 예시에 적었습니다. 남의 봇
    번호를 저장소에 적어 둘 일이 아닙니다.
    """
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")

    assert "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ" in source
    # 실제로 받은 토큰의 봇 번호가 들어가 있으면 안 됩니다.
    assert "8667115971" not in source


def test_checking_the_token_is_separate_from_finding_the_chat_id():
    """"안 와요" 의 원인이 둘인데 증상이 같습니다. 갈라 주어야 고칠 수 있습니다."""
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")
    assert "def check_token()" in source
    assert 'text="토큰 확인"' in source
    assert "bot.bot_username()" in source


def test_get_me_names_the_bot_and_never_changes_anything():
    """토큰만으로 확인할 수 있는 가장 싼 방법입니다."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(
            200, json={"ok": True, "result": {"username": "my_korail_alarm_bot"}}
        )

    with _telegram(handler) as bot:
        assert bot.bot_username() == "my_korail_alarm_bot"

    assert calls == [f"GET /bot{FAKE_TOKEN}/getMe"]


@pytest.mark.parametrize(
    "payload",
    [{"ok": False}, {"ok": True, "result": {}}, {"ok": True, "result": {"username": ""}}],
)
def test_a_bad_token_names_no_bot(payload):
    with _telegram(lambda _r: httpx.Response(200, json=payload)) as bot:
        assert bot.bot_username() is None


# --- 로그인 표시와 조회 진행 막대 --------------------------------------------


def test_the_login_state_speaks_in_colour():
    """가장 자주 확인하는 것입니다. 색으로 말하면 읽지 않아도 압니다."""
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")

    assert "LOGIN_OK_COLOUR" in _ui_function("_login_succeeded")
    assert '_set_login_state("로그인 실패", LOGIN_BAD_COLOUR)' in source
    # 초록/빨강이 실제로 초록/빨강이어야 합니다.
    assert 'LOGIN_OK_COLOUR = "#1a7f37"' in source
    assert 'LOGIN_BAD_COLOUR = "#b3261e"' in source


def test_logging_out_drops_the_session_and_the_password():
    """세션과 비밀번호를 다 버립니다. 자동예매 중에는 막습니다."""
    source = _ui_source()
    body = _ui_function("on_logout")

    assert "self.client = None" in body
    assert "self.logged_in = False" in body
    assert "self._credentials = None" in body
    # 비밀번호는 팝업 안에만 있었고 창이 닫히며 사라집니다.
    assert "self.login_pw" not in source
    # 돌고 있는데 세션을 버리면 자동예매가 도중에 죽습니다.
    assert "self.any_running()" in body
    # 계정이 사라지는 순간이므로 목록도 비웁니다.
    assert "self._reset_session_lists()" in body


def test_a_new_login_also_resets_the_session_lists():
    """다른 아이디로 로그인해도 이전 계정의 목록이 남으면 안 됩니다."""
    body = _ui_function("_login_succeeded")
    assert "self._reset_session_lists()" in body
    assert "self.any_running()" in body


def test_resetting_session_lists_clears_results_targets_and_holds():
    """세 목록을 모두 비우고, 서버에는 아무것도 보내지 않습니다."""
    body = _ui_function("_reset_session_lists")
    assert "self.results = []" in body
    assert "self.targets.clear()" in body
    assert "self.holds.clear()" in body
    assert "self.sync_target_list()" in body
    assert "self.sync_holds()" in body
    # 이 함수 자체는 서버를 부르지 않습니다 — 화면 목록만 정리합니다.
    assert "client" not in body.lower()


def test_every_way_a_search_ends_stops_the_progress_bar():
    """하나라도 빠뜨리면 영원히 돌아가는 막대가 남습니다."""
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }

    assert "self._searching(True)" in functions["on_search"]
    for name in ("_show_journeys", "_search_failed", "_reset_buttons"):
        assert "self._searching(False)" in functions[name], name


# --- 잡은 예약과 결제 기한 ------------------------------------------------------


def test_the_deadline_is_read_the_way_the_app_reads_it():
    """``h_ntisu_lmt_dt`` + ``h_ntisu_lmt_tm`` 을 이어 붙여 읽습니다."""
    assert H.parse_deadline("20260909", "174500") == datetime(2026, 9, 9, 17, 45)
    # 초가 없는 네 자리도 옵니다.
    assert H.parse_deadline("20260909", "1745") == datetime(2026, 9, 9, 17, 45)


@pytest.mark.parametrize(
    "date_text,time_text",
    [(None, "174500"), ("20260909", None), ("2026-09-09", "174500"),
     ("20260909", "17:45"), ("20261399", "174500"), ("", "")],
)
def test_a_deadline_that_does_not_parse_is_unknown_not_guessed(date_text, time_text):
    """반쯤 읽어 엉뚱한 시각을 만드느니 모른다고 합니다 — 틀리면 표를 잃습니다."""
    assert H.parse_deadline(date_text, time_text) is None


def test_the_countdown_says_what_is_left():
    deadline = datetime(2026, 9, 9, 17, 45)
    assert H.remaining_text(deadline, datetime(2026, 9, 9, 17, 44, 0)) == "1분 0초 남음"
    assert H.remaining_text(deadline, datetime(2026, 9, 9, 17, 44, 30)) == "30초 남음"
    assert H.remaining_text(deadline, datetime(2026, 9, 9, 17, 30, 0)) == "15분 0초 남음"
    assert H.remaining_text(deadline, datetime(2026, 9, 9, 15, 30, 0)) == "2시간 15분 남음"
    assert H.remaining_text(deadline, datetime(2026, 9, 9, 17, 46, 0)) == "기한 지남"
    assert H.remaining_text(None, datetime(2026, 9, 9, 17, 44)) == "기한 모름"


def test_an_unknown_deadline_is_never_urgent_and_never_expired():
    """모르는 것을 급하다고도, 지났다고도 하지 않습니다."""
    now = datetime(2026, 9, 9, 17, 44)
    assert not H.is_urgent(None, now)
    assert not H.is_expired(None, now)


def test_a_row_is_coloured_by_how_much_time_is_left():
    held = H.Held(
        label="가는 편", summary="387 수서→창원중앙", pnr="123", fare="45,300원",
        deadline=datetime(2026, 9, 9, 17, 45), deadline_text="2026-09-09 17:45",
    )

    assert held.tag(datetime(2026, 9, 9, 17, 30)) == "held"
    assert held.tag(datetime(2026, 9, 9, 17, 43)) == "urgent"
    assert held.tag(datetime(2026, 9, 9, 17, 46)) == "expired"
    assert held.row(datetime(2026, 9, 9, 17, 44))[-1] == "1분 0초 남음"
    assert held.row(datetime(2026, 9, 9, 17, 44))[0] == "가는 편"


def test_a_one_way_hold_is_labelled_rather_than_left_blank():
    held = H.Held(
        label="", summary="101 서울→부산", pnr="1", fare="-",
        deadline=None, deadline_text="알 수 없음",
    )
    assert held.row(datetime(2026, 9, 9, 17, 44))[0] == "편도"


def test_reserve_now_sends_exactly_one_reservation():
    """[바로 예약] 은 한 번만 보냅니다. 되풀이하면 중복 예약입니다."""
    recorder = _Recorder({RESERVE: _reserve_reply()})

    reserve_once(
        _client(recorder),
        _journey(_summary(general="11")),
        passengers=KorailPassengerCounts(adult=1),
        seat_class=KorailSeatClass.GENERAL,
        live=True,
    )

    assert recorder.count(RESERVE) == 1


def test_reserve_now_sends_nothing_unless_it_is_live():
    recorder = _Recorder({RESERVE: _reserve_reply()})

    result = reserve_once(
        _client(recorder),
        _journey(_summary(general="11")),
        passengers=KorailPassengerCounts(adult=1),
        seat_class=KorailSeatClass.GENERAL,
        live=False,
    )

    assert recorder.count(RESERVE) == 0
    assert [item.category for item in result] == ["reserve"]


def test_the_screen_learns_about_every_hold_the_booker_makes():
    """결과에는 방향별 홀드만 남습니다 — 어느 여정의 것인지는 콜백이 나릅니다."""
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")
    assert "on_hold=self.on_hold_made" in source
    assert "self.root.after(1000, self._tick_holds)" in source


# --- 창 크기 ------------------------------------------------------------------


def test_the_window_scrolls_sideways_too():
    """세로만 굴러가면 창보다 넓은 묶음의 오른쪽이 **그냥 잘립니다.**

    환승 조건 칸의 [후보 갱신]·[비우기] 가 실제로 그렇게 사라져 있었고, 세로만
    굴러가는 창에서는 잘렸다는 사실조차 보이지 않았습니다.
    """
    source = _ui_source()
    assert 'orient="horizontal", command=canvas.xview' in source
    assert "xscrollcommand=hscroll.set" in source
    assert "self.canvas.xview_scroll(step, 'units')" in _ui_function("_on_wheel")
    # 안쪽 폭은 창이 아니라 **안에 든 것**이 정합니다.
    assert "span = max(body.winfo_reqwidth(), width)" in source


def test_the_first_window_size_comes_from_the_content_and_the_screen():
    """고정값으로 잡으면 넓은 화면에서도 잘리고, 좁은 화면에서는 삐져나갑니다."""
    body = _ui_function("_fit_to_screen")
    assert "body.winfo_reqwidth()" in body
    assert "self.root.winfo_screenwidth() * 0.92" in body
    assert "self.root.winfo_screenheight() * 0.92" in body
    source = _ui_source()
    assert 'self.root.geometry("1240x1000")' not in source
    # 크기가 바뀌면 감싸는 라벨이 줄 수를 다시 잡습니다 — 다시 재야 합니다.
    build = _ui_function("_build")
    assert build.index("_fit_to_screen") < build.rindex("_settle_panes")
    assert "self.root.after(80, lambda: self._resettle(fit))" in build


def test_the_train_table_never_starts_at_one_row():
    """이 창에서 가장 자주 보는 표입니다. 한 줄이면 쓸모가 없습니다."""
    source = _ui_source()
    assert "RESULTS_MIN_HEIGHT = 185" in source
    assert "minsize=RESULTS_MIN_HEIGHT" in _ui_function("_build_results")


def test_the_target_table_carries_the_warnings_where_they_cannot_be_cut():
    """여정 칸 끝에 달았더니 표가 조금만 좁아도 안 보였습니다."""
    source = _ui_source()
    assert '("상태", 140, "center")' in source
    assert '("환승 대기", 150, "center")' in source
    state = _ui_function("_target_state")
    assert "books_as_one_reservation(target.journey)" in state
    assert "구간별" in state
    transfer = _ui_function("_target_transfer")
    assert "_transfer_text(" in transfer


def test_one_train_is_written_the_same_way_everywhere():
    """한 화면에서 같은 열차가 "00017" 과 "KTX 17" 로 갈리면 안 됩니다."""
    body = _ui_function("_row_values")
    assert "one_line(journey.train_label())" in body
    assert "journey.train_numbers()" not in body


def test_a_script_exists_to_actually_open_the_window():
    """소스 확인은 "그렇게 쓰여 있다" 까지만 말해 줍니다.

    그려 놓으니 [조회] 단추가 잘리더라는 것은 띄워 봐야 압니다. 시험 환경에는
    tkinter 가 없으므로 그 확인은 스크립트로 두고 손으로 돌립니다.
    """
    script = REPO_ROOT / "scripts" / "gui_smoke.py"
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert "BookerApp" in text
    # 진짜 설정 파일을 건드리면 안 됩니다 — 저장 갈래를 눌러 보기 때문입니다.
    # HOME 만으로는 모자랍니다 — settings_dir() 는 XDG_CONFIG_HOME 을 먼저
    # 보고 윈도우에서는 APPDATA 를 봅니다. 새는 순간 진짜 토큰이 찍힙니다.
    assert 'for name in ("HOME", "XDG_CONFIG_HOME", "APPDATA")' in text
    assert "os.environ[name] = sandbox" in text
    # 진짜 KORAIL 요청이 나가면 안 됩니다.
    assert "on_load_stations = lambda self: None" in text


def test_the_whole_window_scrolls():
    """기능이 늘면서 어떤 화면에서도 다 보이지는 않게 됐습니다."""
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")

    assert "tk.Canvas(self.root" in source
    assert "canvas.configure(yscrollcommand=scroll.set, xscrollcommand=hscroll.set)" in source
    # 휠은 창 어디서 굴려도 듣되, 스스로 굴러가는 위젯에는 양보합니다.
    assert 'canvas.bind_all("<MouseWheel>", self._on_wheel)' in source
    assert 'canvas.bind_all("<Button-4>", self._on_wheel)' in source
    assert "SELF_SCROLLING" in source


def test_every_section_is_a_pane_the_user_can_resize():
    """1~5 묶음과 기록이 서로 크기를 나눌 수 있어야 합니다."""
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    builders = {
        node.name: ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_build_")
    }

    for name in ("_build_login", "_build_query", "_build_results",
                 "_build_targets", "_build_holds", "_build_log"):
        assert "_add_pane(parent" in builders[name], name
    # 자동예매 조건은 제 묶음이 아니라 예매 대상 묶음 안에 붙습니다 — 담는
    # 것과 그것을 노리는 조건은 한 가지 일이고, 묶음 머리 하나를 아끼면
    # 그만큼 표에 줄이 늘어납니다.
    assert "_build_booking" not in builders
    assert "self._build_booking_controls(frame)" in builders["_build_targets"]


def test_the_query_section_uses_its_right_hand_space():
    """오른쪽이 비어 있으면 그만큼 아래 목록이 눌립니다.

    환승 조건 묶음을 왼쪽 줄들 **옆에** 세워 조회 묶음이 472px 에서 300px
    남짓으로 줄었습니다. 그러려면 왼쪽 줄이 좁아야 해서 승객과 열차 종류를
    따로 줄로 뗐습니다 — 한 줄에 몰면 1000px 가까이 되어 옆자리가 없습니다.
    """
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")

    # 환승 조건은 1번 칸(오른쪽)에서 왼쪽 줄 전체와 나란히 섭니다.
    assert "row=0, column=1, rowspan=7" in source
    # 승객은 구간에서 떨어져 나온 제 줄입니다.
    assert 'self._section(frame, 1, "승객")' in source
    # 열차 종류 여덟은 두 줄로 접힙니다.
    assert "half = (len(TRAIN_KINDS) + 1) // 2" in source


def test_a_pane_with_buttons_measures_its_own_minimum():
    """손으로 적어 둔 최소 높이는 반드시 어긋납니다.

    예매 대상 묶음의 최소 높이를 96 으로 적어 뒀다가 [담기]·[빼기]·[비우기] 가
    창이 조금만 작아져도 잘렸습니다. 조회 묶음의 [조회] 도 같은 위험입니다.
    이제 그 묶음들은 ``minsize`` 를 주지 않고, 제 요구 높이를 재서 씁니다.
    """
    source = (APP_DIR / "korail_booker" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    builders = {
        node.name: ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_build_")
    }

    # 단추가 잘릴 수 있는 묶음은 재서 씁니다.
    for name in ("_build_login", "_build_query", "_build_targets"):
        assert "minsize=" not in builders[name], name
    # 줄여도 줄 수만 줄어드는 묶음만 숫자를 적습니다.
    for name in ("_build_results", "_build_log"):
        assert "minsize=" in builders[name], name

    # 본문 높이도 상수가 아니라 그 최소 높이들의 합입니다.
    assert "BODY_HEIGHT" not in source
    assert "sum(self._pane_minimums)" in source


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


def test_the_local_exe_builder_matches_the_ci_build():
    """같은 exe 를 두 길로 만듭니다. 명령이 어긋나면 한쪽만 되는 일이 생깁니다."""
    script = (REPO_ROOT / "exe 만들기 (Windows).bat").read_text(encoding="utf-8")
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "desktop-build.yml"
    ).read_text(encoding="utf-8")

    for fragment in ("--onefile", "--windowed", "--name KorailBooker",
                     "--paths src --paths app", "packaging/desktop_entry.py"):
        assert fragment in workflow, fragment
        # 배치 파일은 경로 구분자가 다릅니다. 그 부분만 바꿔 대조합니다.
        assert fragment.replace("/", "\\") in script or fragment in script, fragment


def test_the_icon_is_optional_on_both_build_paths():
    """``packaging/icon.png`` 을 넣으면 exe 아이콘이 되고, 없어도 빌드는 됩니다.

    아이콘이 없다고 멈추면 그림을 넣지 않은 사람은 exe 를 못 만듭니다.
    """
    script = (REPO_ROOT / "exe 만들기 (Windows).bat").read_text(encoding="utf-8")
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "desktop-build.yml"
    ).read_text(encoding="utf-8")

    for text in (script, workflow):
        # .png 를 쓰려면 pillow 가 있어야 PyInstaller 가 .ico 로 바꿔 줍니다.
        assert "pillow" in text
        assert "packaging/icon.png" in text or "packaging\\icon.png" in text

    # .ico 가 있으면 그것을 먼저 씁니다 — 변환 없이 그대로 들어갑니다.
    # 그리고 둘 다 없을 때를 갈라 두어야 빌드가 멈추지 않습니다.
    assert 'if exist "packaging\\icon.ico" set "ICON=' in script
    assert 'if not defined ICON if exist "packaging\\icon.png"' in script
    assert "if (Test-Path packaging/icon.ico)" in workflow
    assert "elseif (Test-Path packaging/icon.png)" in workflow


def test_the_local_exe_builder_stands_on_its_own():
    """더블클릭 하나로 끝나야 합니다 — 다른 것부터 누르라고 시키지 않습니다.

    .venv 가 있으면 그대로 쓰고, 없으면 여기서 만듭니다. 친구에게 줄 파일
    하나를 만들러 온 사람에게 준비 단계를 더 붙이지 않습니다.
    """
    script = (REPO_ROOT / "exe 만들기 (Windows).bat").read_text(encoding="utf-8")

    assert 'if exist "%VPY%" goto haveenv' in script
    assert '%PY% -m venv "%VENV%"' in script
    assert "pip install httpx cryptography" in script


def test_neither_batch_file_reads_a_variable_it_set_in_the_same_block():
    """cmd 는 괄호 블록을 통째로 펼칩니다 — 그 안에서 방금 set 한 값은 빈 값입니다.

    실제로 이 함정에 걸린 판을 썼습니다. 눈으로 다시 볼 일이 아니라 여기서
    막습니다.
    """
    block = re.compile(r"^\s*(?:if|for)[^\n]*\(\s*\n(.*?)^\s*\)\s*$", re.M | re.S)
    for name in ("실행 (Windows).bat", "exe 만들기 (Windows).bat"):
        script = (REPO_ROOT / name).read_text(encoding="utf-8")
        for body in block.findall(script):
            assigned = set(re.findall(r'set "(\w+)=', body))
            read = set(re.findall(r"%(\w+)%", body))
            assert not (assigned & read), (name, assigned & read)


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


# --- 직접 조합 결과를 1구간으로 묶기 -------------------------------------------


def _leg(train_no: str, dep: str, arr: str) -> TrainSummary:
    return _summary(train_no=train_no, departure_time=dep, arrival_time=arr)


def test_the_same_first_leg_becomes_one_bundle():
    """직접 조합은 1구간 하나에 2구간이 여럿 붙습니다 — 그것을 묶습니다."""
    first = _leg("00301", "054700", "062800")
    a = _journey(first, _leg("00301", "063000", "071200"), source=J.JourneySource.CUSTOM_TRANSFER)
    b = _journey(first, _leg("00003", "063400", "072200"), source=J.JourneySource.CUSTOM_TRANSFER)
    groups = J.group_by_first_leg([a, b])
    assert [indices for _head, indices in groups] == [[0, 1]]


def test_a_bundle_keeps_the_original_row_numbers():
    """화면은 번호로 원래 목록을 되짚습니다. 묶으면서 잃으면 엉뚱한 걸 담습니다."""
    first = _leg("00301", "054700", "062800")
    other = _leg("00305", "060000", "064000")
    journeys = [
        _journey(first, _leg("00301", "063000", "071200"), source=J.JourneySource.CUSTOM_TRANSFER),
        _journey(other, _leg("00007", "065000", "073000"), source=J.JourneySource.CUSTOM_TRANSFER),
        _journey(first, _leg("00003", "063400", "072200"), source=J.JourneySource.CUSTOM_TRANSFER),
    ]
    groups = J.group_by_first_leg(journeys)
    assert [indices for _head, indices in groups] == [[0, 2], [1]]


def test_direct_and_server_transfers_are_never_bundled():
    """직통은 1구간이 곧 여정이고, 서버 추천은 코레일이 이미 골라 준 것입니다."""
    train = _leg("00301", "054700", "062800")
    journeys = [
        _journey(train),
        _journey(train),
        _journey(train, _leg("00003", "063400", "072200"), source=J.JourneySource.SERVER_TRANSFER),
        _journey(train, _leg("00005", "064000", "073000"), source=J.JourneySource.SERVER_TRANSFER),
    ]
    groups = J.group_by_first_leg(journeys)
    assert [indices for _head, indices in groups] == [[0], [1], [2], [3]]


def test_the_screen_draws_bundles_and_expands_them_when_picked():
    """부모 줄을 고르면 그 아래 조합 전부가 담깁니다."""
    source = _ui_source()
    assert "group_by_first_leg" in source
    assert "def _insert_group" in source
    assert "self._group_children" in source
    body = _ui_function("_selected_results_with_groups")
    assert "_group_children" in body
    # 부모로 통째로 딸려 온 것은 화면이 말해 줍니다.
    assert "groups.append((self.results[members[0]], len(members)))" in body


# --- 텔레그램: 저장할지 이번만 쓸지 --------------------------------------------


def test_telegram_settings_can_be_used_without_touching_the_disk():
    """남의 컴퓨터에서 한 번만 쓰고 싶을 때가 있습니다. 토큰은 봇 전체 열쇠입니다."""
    source = _ui_source()
    assert 'text="⑥ 저장하고 쓰기"' in source
    assert 'text="이번만 쓰기"' in source
    assert 'text="저장된 값 지우기"' in source
    once = _ui_function("use_once")
    assert "self._telegram_once = TelegramConfig(" in once
    assert "settings_module.save" not in once


def test_a_one_time_telegram_setting_wins_over_the_stored_one():
    """방금 넣은 값을 쓰겠다는 뜻입니다. 저장된 값이 있어도 그렇습니다."""
    body = _ui_function("_telegram_config")
    assert "self._telegram_once or TelegramConfig(" in body
    # 설정을 굽지 않습니다 — 감시가 도는 중에 채워 넣어도 그때부터 갑니다.
    maker = _ui_function("_make_notifier")
    assert "return self._notify_now" in maker
    assert "self._telegram_config()" in _ui_function("_notify_now")


def test_saving_clears_the_one_time_value():
    """저장한 값이 곧바로 쓰이지 않으면 사람이 저장이 안 됐다고 생각합니다."""
    store = _ui_function("store")
    assert "self._telegram_once = None" in store
    forget = _ui_function("forget")
    assert "telegram_token=''" in forget
    assert "self._telegram_once = None" in forget


# --- 검증되지 않은 환승은 구간마다 따로 삽니다 ----------------------------------


def _custom_transfer(gap_minutes: int = 6) -> J.Journey:
    """직접 조합 환승 하나. 두 구간 다 자리가 있습니다."""
    second = 628 + gap_minutes // 60 * 100 + gap_minutes % 60
    return _journey(
        _summary(train_no="00301", departure_time="054700", arrival_time="062800",
                 general="11", special="11"),
        _summary(train_no="00003", departure_time=f"{second:04d}00",
                 arrival_time="072200", general="11", special="11"),
        source=J.JourneySource.CUSTOM_TRANSFER,
    )


def test_only_a_server_checked_transfer_is_bought_as_one_reservation():
    """서버가 짝지어 준 조합만 한 건(PNR 하나)입니다.

    직접 조합을 환승 예약으로 보내면 서버가 거절하는 일이 있습니다 —
    ``ERR911193 환승최소허용시간 미달`` 을 실제로 받았습니다.
    """
    direct = _journey(_summary())
    server = _journey(
        _summary(train_no="00301"),
        _summary(train_no="00003"),
        source=J.JourneySource.SERVER_TRANSFER,
    )
    assert J.books_as_one_reservation(direct)
    assert J.books_as_one_reservation(server)
    assert not J.books_as_one_reservation(_custom_transfer())


def test_a_custom_combination_is_reserved_one_leg_at_a_time():
    """한 요청이 아니라 두 요청입니다. 각 구간은 그냥 직통 열차 한 편입니다."""
    recorder = _Recorder({RESERVE: _reserve_reply()})

    results = reserve_once(
        _client(recorder),
        _custom_transfer(),
        passengers=KorailPassengerCounts(adult=1),
        seat_class=KorailSeatClass.GENERAL,
        live=True,
    )

    assert recorder.count(RESERVE) == 2
    assert len(results) == 2


def test_a_server_checked_transfer_still_goes_in_one_request():
    """묶어 사도 되는 것까지 쪼개면 PNR 이 둘로 늘어납니다."""
    recorder = _Recorder({RESERVE: _reserve_reply(h_jrny_cnt="2")})

    results = reserve_once(
        _client(recorder),
        _journey(
            _summary(train_no="00301", departure_time="054700",
                     arrival_time="062800", general="11", special="11"),
            _summary(train_no="00003", departure_time="063400",
                     arrival_time="072200", general="11", special="11"),
            source=J.JourneySource.SERVER_TRANSFER,
        ),
        passengers=KorailPassengerCounts(adult=1),
        seat_class=KorailSeatClass.GENERAL,
        live=True,
    )

    assert recorder.count(RESERVE) == 1
    assert len(results) == 1


def test_a_leg_that_fails_never_hides_the_leg_that_was_already_held():
    """앞 구간이 잡힌 사실이 사라지면, 사람은 기한을 모른 채 표를 잃습니다."""
    recorder = _Recorder(
        sequences={
            RESERVE: [
                _reserve_reply(),
                _fail("ERR911193", "환승최소허용시간 미달"),
            ]
        }
    )

    with pytest.raises(PartialTransferError) as caught:
        reserve_once(
            _client(recorder),
            _custom_transfer(),
            passengers=KorailPassengerCounts(adult=1),
            seat_class=KorailSeatClass.GENERAL,
            live=True,
        )

    assert caught.value.leg_number == 2
    assert len(caught.value.held) == 1
    assert "이미 잡혀" in str(caught.value)


def test_the_watcher_stops_after_a_partial_hold_instead_of_trying_again():
    """다시 돌면 이미 잡아 둔 앞 구간을 한 번 더 잡습니다 — 중복 예약입니다."""
    body = _ui_source()
    assert "PartialTransferError" in body
    booker = (APP_DIR / "korail_booker" / "autobook.py").read_text(encoding="utf-8")
    assert "def _settle_partial" in booker
    tree = ast.parse(booker)
    settle = next(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_settle_partial"
    )
    # 이 방향은 여기서 끝냅니다. 계속 지켜보면 앞 구간을 또 잡습니다.
    assert "self._settled[target.direction] = held" in settle
    assert "self._finish(" in settle


def test_the_screen_says_when_a_target_will_be_bought_leg_by_leg():
    """예약이 하나가 아니라 둘이 됩니다. 모르고 누르면 안 됩니다."""
    warning = _ui_function("_split_warning")
    assert "books_as_one_reservation" in warning
    assert "구간마다 따로" in warning
    source = _ui_source()
    # 확인 창 둘 다 이 경고를 싣습니다 — 바로 예약과 자동예매.
    assert source.count("self._split_warning(") == 2


# --- 촉박한 환승 경고 -----------------------------------------------------------


def test_a_short_connection_is_flagged_but_a_long_one_is_not():
    assert J.is_tight_transfer(_custom_transfer(gap_minutes=6))
    assert J.is_tight_transfer(_custom_transfer(gap_minutes=9))
    assert not J.is_tight_transfer(_custom_transfer(gap_minutes=10))
    assert not J.is_tight_transfer(_custom_transfer(gap_minutes=44))


def test_a_direct_train_is_never_a_tight_transfer():
    """환승이 없으면 환승 대기도 없습니다. 모르는 것을 경고로 바꾸지 않습니다."""
    assert not J.is_tight_transfer(_journey(_summary()))


def test_the_threshold_is_ours_and_says_so():
    """코레일의 최소 환승 허용 시간은 확인하지 못했습니다."""
    source = (APP_DIR / "korail_booker" / "journeys.py").read_text(encoding="utf-8")
    assert "TIGHT_TRANSFER_MINUTES = 10" in source
    assert "확인하지 못했습니다" in source
    assert "ERR911193" in source


def test_a_tight_connection_is_red_in_both_tables():
    """색은 줄 단위입니다 — 칸 하나만 물들일 수 없어서 표도 함께 답니다."""
    source = _ui_source()
    assert 'tree.tag_configure("tight", foreground="#d1242f")' in source
    assert 'self.target_list.tag_configure("tight", foreground="#d1242f")' in source
    tags = _ui_function("_row_tags")
    assert "if is_tight_transfer(journey):" in tags
    assert "return ('tight',)" in tags
    # 담고 나면 위 표를 다시 보지 않습니다. 경고가 함께 따라와야 합니다.
    transfer = _ui_function("_target_transfer")
    assert "_transfer_text(" in transfer
    assert "is_tight_transfer(journey)" in _ui_function("_transfer_text")


def test_the_watcher_buys_a_custom_combination_one_leg_at_a_time():
    """서버가 검증하지 않은 조합은 한 요청이 아니라 구간 수만큼입니다."""
    first = _row("00009", arrival="대전", arrival_code="0010", arrival_time="091500",
                 general="11")
    second = _row("00503", departure="대전", departure_code="0010",
                  departure_time="093700", arrival_time="110500", general="11")
    recorder = _Recorder({SEARCH: _search_reply([first, second]),
                          RESERVE: _reserve_reply()})
    journey = _journey(
        TrainSummary.from_raw(first),
        TrainSummary.from_raw(second),
        source=J.JourneySource.CUSTOM_TRANSFER,
    )
    made: list[str] = []
    booker = AutoBooker(
        _client(recorder),
        [_target(journey, _request(include_direct=False, include_transfer=True))],
        BookingOptions(poll_interval_s=10.0, live=True),
        log=lambda message: None,
        on_hold=lambda label, summary, kind, direction, hold: made.append(kind),
    )

    result = booker.run(threading.Event())

    assert result.outcome is Outcome.HELD
    assert recorder.count(RESERVE) == 2
    # 두 구간이 각각 예약이 됩니다 — 잡은 예약 목록에도 둘로 들어갑니다.
    # 종류는 구간마다 다릅니다 — 그래야 같은 '좌석 예약(구간별)' 이 두 번
    # 찍혀서 중복 예약처럼 보이지 않습니다.
    assert made == ["좌석 예약(1구간)", "좌석 예약(2구간)"]
    assert len(result.holds) == 2


def test_a_half_finished_custom_combination_stops_instead_of_retrying():
    """다음 회차에 또 돌면 이미 잡은 앞 구간을 한 번 더 잡습니다."""
    first = _row("00009", arrival="대전", arrival_code="0010", arrival_time="091500",
                 general="11")
    second = _row("00503", departure="대전", departure_code="0010",
                  departure_time="093700", arrival_time="110500", general="11")
    recorder = _Recorder(
        replies={SEARCH: _search_reply([first, second])},
        sequences={RESERVE: [_reserve_reply(),
                             _fail("ERR911193", "환승최소허용시간 미달")]},
    )
    journey = _journey(
        TrainSummary.from_raw(first),
        TrainSummary.from_raw(second),
        source=J.JourneySource.CUSTOM_TRANSFER,
    )
    told: list[str] = []
    booker = AutoBooker(
        _client(recorder),
        [_target(journey, _request(include_direct=False, include_transfer=True))],
        BookingOptions(poll_interval_s=10.0, live=True),
        log=told.append,
        notify=told.append,
    )

    result = booker.run(threading.Event())

    # 두 번만 나갑니다 — 1구간 성공, 2구간 실패. 되풀이하지 않습니다.
    assert recorder.count(RESERVE) == 2
    assert result.outcome is Outcome.HELD
    assert any("일부만 잡혔습니다" in line for line in told)
    assert any("ERR911193" in line for line in told)


def test_how_a_target_is_bought_follows_what_the_user_agreed_to():
    """다시 조회한 여정의 출처가 아니라, 담을 때 본 것을 따릅니다.

    확인 창이 "구간마다 따로 삽니다" 라고 말해 놓고 한 건으로 사면 약속이
    깨집니다. 구간의 값(좌석 코드 등)은 방금 받은 것을 그대로 씁니다.
    """
    booker = (APP_DIR / "korail_booker" / "autobook.py").read_text(encoding="utf-8")
    tree = ast.parse(booker)
    body = next(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_try_reserve"
    )
    assert "journey = replace(journey, source=target.journey.source)" in body
    assert "one_go = books_as_one_reservation(journey)" in body


# --- 감사에서 나온 결함들 -------------------------------------------------------


def test_a_deadline_clock_that_lost_its_leading_zero_is_repaired():
    """서버는 09:30 을 JSON 숫자 93000 으로도 보냅니다.

    자르기만 하면 "93:00:0" 이라는 없는 시각이 알림과 목록에 찍히고, 카운트다운은
    같은 값을 거절해 "기한 모름" 이라고 적습니다. 둘이 어긋나면 사람은 진짜 기한을
    알 방법이 없습니다 — 그리고 그 시간대가 밤새 잡은 예약의 기한입니다.
    """
    hold = parse_reservation_hold_response(
        _reserve_reply(h_ntisu_lmt_dt="20990101", h_ntisu_lmt_tm=93000)
    )
    assert payment_deadline_text(hold) == "2099-01-01 09:30:00"
    assert H.parse_deadline("20990101", "93000") == datetime(2099, 1, 1, 9, 30)


def test_the_countdown_uses_korean_time():
    """기한은 서버가 한국 시각으로 줍니다. 컴퓨터 시계가 다르면 그대로 빼면 틀립니다."""
    source = (APP_DIR / "korail_booker" / "holds.py").read_text(encoding="utf-8")
    assert "KST = timezone(timedelta(hours=9))" in source
    assert "def now_kst()" in source
    assert "now_kst()" in _ui_source()


def test_every_ending_carries_the_reservations_already_made():
    """왕복에서 한쪽을 잡아 둔 채 시간이 끝나면, 그 예약을 결과가 잃으면 안 됩니다."""
    booker = (APP_DIR / "korail_booker" / "autobook.py").read_text(encoding="utf-8")
    tree = ast.parse(booker)
    run = next(
        ast.unparse(n) for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_run"
    )
    assert "BookingResult(" not in run          # 모든 끝은 _result 를 거칩니다
    assert run.count("self._result(") >= 5
    result = next(
        ast.unparse(n) for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_result"
    )
    assert "for group in self._settled.values() for hold in group" in result


def test_the_end_is_announced_even_when_the_loop_explodes():
    """밤새 켜 둔 사람에게 시작 알림만 오고 아무 소식이 없으면 안 됩니다."""
    booker = (APP_DIR / "korail_booker" / "autobook.py").read_text(encoding="utf-8")
    tree = ast.parse(booker)
    run = next(
        ast.unparse(n) for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "run"
    )
    assert "except BaseException" in run
    assert run.count("self.announce(") >= 3


def test_a_broken_reserve_post_is_never_retried():
    """전송이 끊기면 서버에 예약이 생겼는지 알 수 없습니다. 다시 보내면 중복입니다."""
    recorder = _Recorder(
        replies={SEARCH: _search_reply([_row("00101", general="11")])},
        sequences={RESERVE: [_ok(), _ok()]},
    )
    client = _client(recorder)

    def explode(*_a: Any, **_k: Any) -> Any:
        raise KorailTransportError("synthetic connection reset")

    client.reserve = explode  # type: ignore[method-assign]
    told: list[str] = []
    booker = AutoBooker(
        client,
        [_target(_journey(_summary(general="11")))],
        BookingOptions(poll_interval_s=10.0, live=True),
        log=told.append,
        notify=told.append,
    )

    result = booker.run(threading.Event())

    assert any("전송 중에 끊겼습니다" in line for line in told)
    assert any("다시 보내지" in line for line in told)
    assert result.outcome is not Outcome.HELD


def test_a_direction_with_nothing_left_to_watch_does_not_spin():
    """폼을 못 만들어 전부 빠진 방향을 '아직 안 끝남' 으로 세면 감시가 헛돕니다."""
    booker = (APP_DIR / "korail_booker" / "autobook.py").read_text(encoding="utf-8")
    assert "def _open_directions" in booker
    assert "alive = {target.direction for target in self._pending()}" in booker


def test_the_request_decides_which_targets_share_a_search():
    """방향만 보고 묶으면 첫 대상의 조건으로만 물어, 나머지는 영영 안 잡힙니다."""
    booker = (APP_DIR / "korail_booker" / "autobook.py").read_text(encoding="utf-8")
    assert "for group in dict.fromkeys(target.request for target in pending)" in booker
    assert "strict=True" in booker


def test_the_journey_key_carries_the_date():
    """같은 열차가 날짜만 달리해 옵니다. 날짜가 없으면 둘이 같은 열쇠가 됩니다."""
    today = _journey(_summary(train_no="00101"))
    tomorrow = _journey(
        TrainSummary.from_raw({**_row("00101"), "h_dpt_dt": "20990102"})
    )
    assert today.key() != tomorrow.key()
    assert today.key()[0][1] == "20990101"


def test_the_pacer_holds_its_floor_under_many_threads():
    """클라이언트 하나를 스레드 여럿이 씁니다. 잠금이 없으면 한꺼번에 쏩니다."""
    pacer = ST_SESSION.Pacer(min_interval_s=0.05)
    stamps: list[float] = []
    lock = threading.Lock()

    def hit() -> None:
        pacer.wait()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=hit) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    stamps.sort()
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    # 잠금이 없으면 이 간격이 전부 0 에 가깝습니다.
    assert all(gap >= 0.04 for gap in gaps), gaps


def test_settings_are_written_beside_and_swapped_in():
    """제자리에 덮어쓰면 쓰다가 멈춘 순간 토큰까지 함께 잃습니다."""
    source = (APP_DIR / "korail_booker" / "settings.py").read_text(encoding="utf-8")
    assert "tempfile.mkstemp(" in source
    assert "os.replace(temporary, target)" in source
    # 만드는 순간부터 소유자 전용입니다 — umask 권한으로 만든 뒤 좁히면 늦습니다.
    assert source.index("os.chmod(temporary") < source.index("os.fdopen(descriptor")


def test_a_settings_file_full_of_nonsense_still_opens_the_window(tmp_path: Path):
    """손으로 고칠 수 있는 파일입니다. 무엇이 들어 있든 창은 떠야 합니다."""
    target = tmp_path / "settings.json"
    target.write_text('{"watch_minutes": ' + "1" + "0" * 400 + "}", encoding="utf-8")
    assert ST.load(target).watch_minutes == 60
    target.write_text(
        json.dumps({"poll_interval_s": float("nan"), "transfer_mode": "bogus"}),
        encoding="utf-8",
    )
    restored = ST.load(target)
    assert restored.poll_interval_s == 30.0
    assert restored.transfer_mode == "server"


def test_the_chat_id_lookup_asks_for_the_latest_update():
    """limit 만 주면 텔레그램은 밀린 것 중 가장 오래된 쪽부터 돌려줍니다."""
    source = (APP_DIR / "korail_booker" / "notify.py").read_text(encoding="utf-8")
    assert '"offset": -1' in source


def test_a_broken_token_never_escapes_the_notifier():
    """httpx.InvalidURL 은 HTTPError 가 아닙니다. 새면 그날 밤 알림이 다 사라집니다."""
    notifier = N.TelegramNotifier(N.TelegramConfig(token="a\nb", chat_id="1"))
    try:
        assert notifier.send("hi") is False
        assert notifier.bot_username() is None
        assert notifier.resolve_chat() is None
    finally:
        notifier.close()


def test_one_direction_is_never_watched_or_reserved_twice():
    """묶음끼리는 서로를 모릅니다. 방향으로 막지 않으면 예약이 두 번 나갑니다."""
    source = _ui_source()
    assert "def _busy_directions" in source
    start = _ui_function("_start_targets")
    assert "busy = self._busy_directions()" in start
    now = _ui_function("on_reserve_now")
    assert "busy = self._busy_directions()" in now
    assert "conflicting" in now


def test_the_target_selection_survives_a_redraw():
    """항목 id 는 다시 그릴 때마다 새로 매겨집니다. 그걸로 찾으면 늘 빗나갑니다."""
    body = _ui_function("sync_target_list")
    # 줄 번호가 아니라 **어느 열차였는지**로 되돌립니다. 번호로 되돌리면
    # [빼기] 로 앞줄이 사라진 뒤 그 번호에 온 다른 열차가 골라집니다.
    assert "(target.journey.key(), target.label) for target in self.selected_targets()" in body
    assert "if (target.journey.key(), target.label) not in chosen:" in body


def test_a_second_search_cannot_overwrite_the_first():
    """Enter 는 잠긴 단추도 그냥 지나갑니다."""
    body = _ui_function("on_search")
    assert "if self._search_token is not None:" in body


def test_the_wheel_never_changes_a_combobox_value():
    """창을 굴리려던 휠이 좌석 등급을 바꾸고 스크롤 밖으로 밀어냅니다."""
    source = _ui_source()
    assert "def swallow_wheel" in source
    assert "self.swallow_wheel(self)" in source


def test_a_date_that_is_not_on_the_calendar_is_refused():
    from korail_booker.ui import parse_date_field

    for bad in ("2026-11-31", "2026-13-01", "9999-99-99"):
        with pytest.raises(ValueError):
            parse_date_field(bad)


def test_a_modal_never_stops_the_event_queue():
    """_drain 안에서 모달을 열면 [확인] 을 누를 때까지 화면이 멈춥니다."""
    body = _ui_function("_worker_failed")
    assert "self.root.after(0, lambda: messagebox.showerror" in body


def test_closing_asks_before_losing_a_deadline():
    """잡은 예약 목록은 메모리에만 있습니다. 닫으면 PNR 과 기한이 사라집니다."""
    body = _ui_function("on_close")
    assert "self._reserving" in body
    assert "is_expired(held.deadline, now_kst())" in body


def test_the_client_is_built_once_even_under_two_threads():
    body = _ui_function("_ensure_client")
    assert "with self._client_lock:" in body


def test_stopping_a_search_lets_the_next_one_start():
    """중지가 표시를 안 내리면 재진입 가드가 [조회] 를 세션 내내 막습니다."""
    body = _ui_function("on_stop_search")
    assert "self._search_cancelled.add(self._search_token)" in body
    assert "self._search_token = None" in body
    # 버림 표시는 그대로 남아 늦게 끝난 조회가 화면을 덮지 못합니다.
    assert body.index("_search_cancelled.add") < body.index("self._search_token = None")


# --- 실행 기반 2차 감사에서 나온 결함들 ------------------------------------------


def test_a_broken_reserve_never_ends_as_a_preview():
    """진짜로 보냈는데 "아무것도 보내지 않았습니다" 로 끝나면 안 됩니다."""
    recorder = _Recorder({SEARCH: _search_reply([_row("00101", general="11")])})
    client = _client(recorder)

    def explode(*_a: Any, **_k: Any) -> Any:
        raise KorailTransportError("synthetic connection reset")

    client.reserve = explode  # type: ignore[method-assign]
    told: list[str] = []
    result = AutoBooker(
        client,
        [_target(_journey(_summary(general="11")))],
        BookingOptions(poll_interval_s=10.0, live=True),
        log=told.append,
        notify=told.append,
    ).run(threading.Event())

    assert result.outcome is Outcome.FAILED
    assert "결과를 알 수 없습니다" in result.message
    assert result.outcome is not Outcome.PREVIEW


def test_a_standby_post_that_is_cut_never_kills_the_watch():
    """예약대기는 곁가지입니다. 그 전송 실패가 좌석 감시를 죽이면 안 됩니다."""
    recorder = _Recorder({SEARCH: _search_reply([_row("00101", h_wait_rsv_flg=" 9")])})
    client = _client(recorder)
    calls: list[str] = []

    def explode(*_a: Any, **kwargs: Any) -> Any:
        calls.append(str(kwargs.get("job_type")))
        raise KorailTransportError("synthetic connection reset")

    client.reserve = explode  # type: ignore[method-assign]
    told: list[str] = []
    result = AutoBooker(
        client,
        [_target(_journey(_summary(h_wait_rsv_flg=" 9")))],
        BookingOptions(poll_interval_s=10.0, live=True, allow_standby=True),
        log=told.append,
        notify=told.append,
    ).run(threading.Event())

    # 예외가 새지 않고 결과가 돌아옵니다.
    assert result.outcome in (Outcome.FAILED, Outcome.STOPPED, Outcome.TIMEOUT)
    assert any("전송 중에 끊겼습니다" in line for line in told)


def test_a_failed_relogin_does_not_kill_the_watch():
    """비밀번호가 바뀌었을 수 있습니다. 첫 회차에 죽으면 남은 횟수가 무의미합니다."""
    def explode() -> None:
        raise RuntimeError("synthetic login failure")

    booker = AutoBooker(
        _client(_Recorder({SEARCH: _search_reply([_row("00101")])})),
        [_target(_journey(_summary()))],
        BookingOptions(poll_interval_s=10.0),
        log=lambda _m: None,
        relogin=explode,
    )
    assert booker._try_relogin() is False


def test_the_deadline_is_read_by_one_parser_only():
    """화면 글과 카운트다운이 따로 읽어 12시간 다른 기한을 말했습니다."""
    booker = (APP_DIR / "korail_booker" / "autobook.py").read_text(encoding="utf-8")
    assert "from .holds import parse_deadline" in booker
    text = next(
        ast.unparse(n) for n in ast.walk(ast.parse(booker))
        if isinstance(n, ast.FunctionDef) and n.name == "payment_deadline_text"
    )
    assert "parse_deadline(" in text
    assert "normalize_clock" not in text
    # 두 곳이 같은 값을 말합니다.
    hold = parse_reservation_hold_response(
        _reserve_reply(h_ntisu_lmt_dt="20990101", h_ntisu_lmt_tm="0016")
    )
    shown = payment_deadline_text(hold)
    counted = H.parse_deadline(hold.payment_deadline_date, hold.payment_deadline_time)
    assert counted is not None
    assert shown == counted.strftime("%Y-%m-%d %H:%M:%S")


def test_a_direction_stays_blocked_even_when_its_row_is_gone():
    """줄을 빼면 막을 근거가 사라져 같은 구간에 두 번째 예약이 나갔습니다."""
    source = _ui_source()
    assert "direction: tuple[str, str, str] = ('', '', '')" in ast.unparse(
        ast.parse((APP_DIR / "korail_booker" / "holds.py").read_text(encoding="utf-8"))
    )
    body = _ui_function("_busy_directions")
    # 화면 목록이 아니라 예약과 감시가 아는 방향을 봅니다.
    assert "self.targets" not in body
    assert "busy |= watch.directions" in body
    assert "held.direction for held in self.holds" in body
    assert "busy |= self._reserving_directions" in body
    # 멈춘 묶음은 풀어 줍니다 — 안 그러면 한 번 돌린 방향을 다시 못 노립니다.
    assert "if watch.running:" in body
    assert "directions: frozenset[tuple[str, str, str]]" in source


def test_an_expired_hold_no_longer_blocks_its_direction():
    """기한이 지나면 코레일이 스스로 취소합니다 — 화면이 계속 막으면 안 됩니다.

    실제로 결제 기한이 지난 예약 두 건이 있는데도 같은 구간의 다른 조합을
    새로 노릴 수 없는 신고가 있었습니다. 원인은 ``_busy_directions`` 가
    기한을 보지 않고 방향이 있다는 사실만 봤기 때문입니다.
    """
    body = _ui_function("_busy_directions")
    assert "not is_expired(held.deadline, now)" in body


def test_a_cut_reserve_from_the_button_is_not_reported_as_a_plain_failure():
    """"실패" 라고만 적으면 사람이 다시 눌러 중복 예약을 만듭니다."""
    source = _ui_source()
    assert "def _reserve_now_broken" in source
    body = _ui_function("_reserve_now_broken")
    assert "다시 누르기 전에" in body
    now = _ui_function("on_reserve_now")
    assert "except KorailTransportError as exc:" in now


def test_the_reserve_button_stays_disabled_while_a_request_is_out():
    body = _ui_function("_reset_buttons")
    assert "if not self._reserving:" in body


def test_every_combobox_refuses_the_wheel():
    """역 칸만 막으면 좌석 등급과 시각 칸이 그대로 굴러갑니다."""
    source = _ui_source()
    assert source.count("swallow_wheel(") >= 4


def test_today_is_korean_time_everywhere():
    """코레일의 '오늘' 은 한국 시각입니다. 노트북 시계로 보면 오늘을 거절합니다."""
    source = _ui_source()
    assert "now_kst().strftime('%Y%m%d')" in ast.unparse(ast.parse(source))
    assert "date.today()" not in source


def test_the_return_window_is_saved_after_it_is_read():
    """앞서 저장하면 오는 편 시간대가 늘 한 번 전 조회의 값으로 남습니다."""
    body = _ui_function("on_search")
    assert body.index("build_return_request") < body.index("self._remember(request)")


def test_a_half_bought_transfer_is_not_recorded_as_the_whole_journey():
    """구간별 홀드는 전체 여정이 아니라 그 구간으로 적혀야 합니다.

    여정 한 줄을 그대로 두 번 쓰면 PNR·운임이 다른데 '여정' 칸만 같아 보여
    중복 예약으로 오인하게 됩니다 — 실제로 그런 신고가 있었습니다.
    """
    booker = (APP_DIR / "korail_booker" / "autobook.py").read_text(encoding="utf-8")
    partial = next(
        ast.unparse(n) for n in ast.walk(ast.parse(booker))
        if isinstance(n, ast.FunctionDef) and n.name == "_settle_partial"
    )
    assert "journey.leg_hold_label(number - 1, partial=True)" in partial

    settle = next(
        ast.unparse(n) for n in ast.walk(ast.parse(booker))
        if isinstance(n, ast.FunctionDef) and n.name == "_settle"
    )
    # 전부 성공했을 때도 구간별이면 마찬가지입니다.
    assert "journey.leg_hold_label(number - 1) if split else journey.summary()" in settle


def test_the_leg_hold_label_names_which_leg_it_is():
    """전체 여정 요약이 아니라 그 구간이 무엇인지가 먼저 와야 합니다."""
    first = _summary(train_no="00301", departure="동탄", arrival="대전",
                     arrival_code="0010", departure_time="054700",
                     arrival_time="062800")
    second = _summary(train_no="00003", departure="대전", arrival="동대구",
                      departure_code="0010", arrival_code="0015",
                      departure_time="063400", arrival_time="072200")
    journey = _journey(first, second, source=J.JourneySource.CUSTOM_TRANSFER)
    assert journey.leg_hold_label(0).startswith("[1구간] ")
    assert journey.leg_hold_label(1).startswith("[2구간] ")
    assert "전체 여정:" in journey.leg_hold_label(0)
    # 부분 실패는 문구가 갈립니다 — "구간만" 은 나머지를 못 잡았다는 뜻입니다.
    assert journey.leg_hold_label(0, partial=True).startswith("[1구간만] ")
    assert "원래 여정:" in journey.leg_hold_label(0, partial=True)


def test_a_stale_row_number_never_indexes_past_the_list():
    """표를 다시 그리기 전의 번호는 줄어든 목록의 범위를 벗어납니다.

    [빼기] 로 목록이 짧아진 직후 ``sync_target_list`` 가 옛 번호로 ``self.targets``
    를 짚어 IndexError 로 죽었습니다 — 선택을 열차로 되돌리게 고치면서 생긴
    구멍입니다.
    """
    body = _ui_function("selected_indices")
    assert "index < len(self.targets)" in body


# --- 화면 개선 4건 --------------------------------------------------------------


def test_double_click_adds_without_folding_the_transfer_row():
    """Treeview 는 두 번 누르면 접었다 폈다 하는 것이 기본입니다.

    그래서 환승 여정을 담을 때마다 구간 줄이 제멋대로 접히고 펴졌습니다.
    접고 펴는 것은 왼쪽 +/- 를 눌러서만 되어야 합니다.
    """
    body = _ui_function("_result_double_clicked")
    assert "return 'break'" in body
    # +/- 자리에서는 막지 않습니다 — 막으면 접고 펴는 것이 죽습니다.
    assert "if self._on_expander(widget, event):" in body
    expander = _ui_function("_on_expander")
    # Tk 가 그 자리를 부르는 이름. 앞에 스타일 이름이 붙으므로 끝만 봅니다.
    assert "identify_element(event.x, event.y)).endswith('indicator')" in expander
    # 예매 대상 표도 같은 규칙입니다.
    assert "return 'break'" in _ui_function("_target_double_clicked")


def test_the_notifier_reads_the_settings_when_it_fires():
    """감시를 걸어 놓고 나서 텔레그램을 채우면 그때부터 알림이 가야 합니다."""
    source = _ui_source()
    # 설정을 구워 넘기지 않습니다.
    assert "notify=self._make_notifier()" in source
    assert "return self._notify_now" in _ui_function("_make_notifier")
    now = _ui_function("_notify_now")
    assert "config = self._telegram_config()" in now
    # 알림 실패가 예약을 죽이지 않습니다.
    assert "except Exception" in now


def test_a_late_telegram_setting_tells_the_running_watches():
    """뒤늦게 채운 사람은 설정이 먹혔는지, 지금 얼마나 남았는지를 알아야 합니다."""
    body = _ui_function("announce_watches")
    # 남은 시간으로 말합니다 — 시작할 때의 총 감시 시간이 아니라.
    assert "watch.remaining(now)" in body
    assert "watch.options.poll_interval_s" in body
    source = _ui_source()
    # 저장하기와 이번만 쓰기 둘 다에서 걸립니다.
    assert source.count("self.announce_watches()") == 2


def test_picking_a_bundle_head_says_what_it_just_added():
    """부모 줄은 1구간만 정합니다. 2구간은 가능한 것이 전부 담깁니다."""
    body = _ui_function("_explain_groups")
    assert "1구간" in body and "2구간" in body
    assert "if not groups:" in body            # 자식만 골랐으면 조용합니다
    # Tk 에 찍히는 글에 마크다운이 없는지는 저장소 전체 시험이 봅니다
    # (test_no_screen_text_carries_markdown_asterisks). 여기서 다시 보면
    # 주석의 강조까지 걸립니다.
    assert "self._explain_groups(groups)" in _ui_function("add_targets")


def test_switching_transfer_mode_never_overwrites_a_curated_list():
    """모드 전환이 [조회]·[후보 갱신] 규칙을 뒤로 돌아가면 안 됩니다."""
    body = _ui_function("_offer_transfer_candidates")
    # 사람이 만든 목록이 있으면 표시만 다시 그립니다.
    assert "if self.transfer_names():" in body
    assert "self._redraw_transfer_marks()" in body
    # 비어 있을 때만 불러옵니다.
    assert "transfer_station_candidates(client, departure, arrival)" in body
    # 구간이 없거나 실패해도 모드 전환을 막지 않습니다.
    assert "if not departure or not arrival:" in body
    assert "except (KorailApiError, ValueError)" in body
    assert "self._offer_transfer_candidates()" in _ui_function("_transfer_toggled")


# --- 잡은 예약: 취소·정리 ---------------------------------------------------------


def test_the_hold_table_rebuilds_instead_of_shifting_indices():
    """줄 하나를 지우면 번호가 밀립니다. append-only 짝으로는 어긋납니다."""
    source = _ui_source()
    assert "def sync_holds" in source
    remember = _ui_function("remember_hold")
    assert "self.sync_holds()" in remember
    sync = _ui_function("sync_holds")
    assert "self.hold_tree.delete(*self.hold_tree.get_children())" in sync
    assert "self._hold_items = {}" in sync


def test_removing_a_hold_never_touches_the_server():
    body = _ui_function("remove_holds")
    assert "del self.holds[index]" in body
    assert "self.sync_holds()" in body
    assert "client" not in body


def test_clearing_expired_holds_sends_nothing_to_the_server():
    """기한이 지나면 코레일이 스스로 취소합니다 — 물어볼 필요가 없습니다."""
    body = _ui_function("clear_expired_holds")
    assert "is_expired(held.deadline, now)" in body
    assert "self.remove_holds(expired)" in body
    assert "client" not in body


def test_clearing_all_holds_warns_about_unpaid_ones_first():
    """이 목록이 PNR 을 보는 유일한 자리입니다. 지우기 전에 알려야 합니다."""
    body = _ui_function("clear_holds")
    assert "unpaid = [held for held in self.holds if not is_expired" in body
    assert "messagebox.askyesno(" in body
    assert "held.pnr" in body


def test_cancel_hold_refuses_an_expired_or_unconfirmed_row():
    body = _ui_function("on_cancel_hold")
    # 기한이 지난 것은 취소를 보내지 않습니다 — 이미 죽은 홀드입니다.
    assert "is_expired(held.deadline, now_kst())" in body
    assert "만료된 것 지우기" in body
    # 로그인 없이는 안 나갑니다.
    assert "if not self.logged_in:" in body
    # 성공하면 목록에서만 뺍니다(서버 재확인 없이) — 응답이 곧 성사입니다.
    done = _ui_function("_cancel_hold_done")
    assert "self.remove_holds([index])" in done
    assert "isinstance(result, MutationPreview)" in done


def test_the_holds_panel_offers_all_three_actions():
    source = _ui_source()
    assert 'text="선택 취소"' in source
    assert 'text="만료된 것 지우기"' in source
    # '비우기' 는 예매 대상 칸에도 있는 이름이라 held 쪽 버튼을 콕 집어 봅니다.
    build = _ui_function("_build_holds")
    assert "text='비우기'" in build
    assert "command=self.on_cancel_hold" in build
    assert "command=self.clear_expired_holds" in build
    assert "command=self.clear_holds" in build


# --- 조회 중 조건 잠금 ------------------------------------------------------------


def test_searching_locks_the_query_and_transfer_fields():
    """도는 중에 조건을 바꾸면 결과가 어느 조건의 것인지 알 수 없어집니다."""
    body = _ui_function("_searching")
    assert "self._lock_query_fields(busy)" in body
    lock = _ui_function("_lock_query_fields")
    assert "self.search_button" in lock
    assert "self.search_stop_button" in lock
    assert "self.calendar" in lock
    assert "self._set_widget_locked(self.query_frame" in lock


def test_unlocking_restores_conditional_state_not_just_normal():
    """왕복·환승이 꺼져 있던 칸까지 통째로 풀면, 조건과 다시 어긋납니다."""
    lock = _ui_function("_lock_query_fields")
    assert "self.sync_round_trip_state()" in lock
    assert "self.sync_transfer_state()" in lock
    # 부작용(mark_stale 등) 없이 상태만 다시 맞추는 자리가 따로 있어야 합니다.
    assert "def sync_round_trip_state" in _ui_source()
    round_trip = _ui_function("_round_trip_toggled")
    assert "self.sync_round_trip_state()" in round_trip
    assert "self.mark_stale()" in round_trip


def test_the_calendar_manages_its_own_disabled_days():
    """지난 날짜 단추를 스스로 잠가 둡니다 — 한 번 더 풀면 되살아납니다."""
    lock = _ui_function("_lock_query_fields")
    assert "self.calendar" in lock
    helper = _ui_function("_set_widget_locked")
    assert "if child in exempt:" in helper
    assert "continue" in helper
