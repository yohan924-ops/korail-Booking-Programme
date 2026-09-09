"""``scripts/watch_and_reserve.py`` 의 오프라인 안전 시험.

이 스크립트는 사람 대신 조회를 되풀이하다가 진짜 예약을 만들 수 있습니다. 그래서
여기서 못박는 것은 편의가 아니라 **무엇을 보내지 않는가** 입니다.

* import 만으로는 I/O 도 환경변수 읽기도 일어나지 않는다
* 스위치가 다 서지 않으면 예약 요청이 한 건도 나가지 않는다
* 잡히면 그 자리에서 끝난다 — 두 번째 예약 요청은 없다
* 이 파일이 만드는 consent 는 예약 하나뿐이다(결제·취소·환불 아님)
* 조회 주기는 하한 아래로 내려가지 않는다

모든 요청은 ``httpx.MockTransport`` 를 지납니다. 네트워크에 닿는 것은 없습니다.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from korail_mobile_api import (
    KorailClient,
    KorailPassengerCounts,
    KorailSeatClass,
    KorailSession,
    TrainSearchQuery,
    TrainSummary,
)


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "watch_and_reserve.py"
SCRIPT_SOURCE = SCRIPT_PATH.read_text(encoding="utf-8")


def _load_script(name: str = "watch_and_reserve"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


wr = _load_script()

SEARCH = "/classes/com.korail.mobile.seatMovie.ScheduleView"
RESERVE = "/classes/com.korail.mobile.certification.TicketReservation"
STANDBY = "/classes/com.korail.mobile.reservationWait.ReservationWait"
SYNTHETIC_PNR = "399999999999999"


def _ok(**extra: Any) -> dict[str, Any]:
    return {"h_msg_cd": "SYNTHETIC.OK", "h_msg_txt": "ok", "strResult": "SUCC", **extra}


def _fail(code: str, message: str = "synthetic failure") -> dict[str, Any]:
    return {"h_msg_cd": code, "h_msg_txt": message, "strResult": "FAIL"}


def _train_row(
    train_no: str,
    *,
    general: str = "13",
    departure_time: Any = "060000",
    **extra: Any,
) -> dict[str, Any]:
    """검색 행 하나. 기본은 **매진**(``h_gen_rsv_cd`` = ``"13"``)입니다."""
    return {
        **extra,
        "h_trn_no": train_no,
        "h_trn_gp_cd": "100",
        "h_dpt_rs_stn_cd": "0001",
        "h_arv_rs_stn_cd": "0020",
        "h_dpt_rs_stn_nm": "서울",
        "h_arv_rs_stn_nm": "부산",
        "h_dpt_dt": "20990101",
        "h_dpt_tm": departure_time,
        "h_arv_tm": "083000",
        "h_run_dt": "20990101",
        "h_trn_clsf_cd": "00",
        "h_trn_clsf_nm": "KTX",
        "h_dpt_stn_run_ordr": "1",
        "h_arv_stn_run_ordr": "2",
        "h_dpt_stn_cons_ordr": "1",
        "h_arv_stn_cons_ordr": "2",
        "h_seat_att_cd": "015",
        "h_gen_rsv_cd": general,
    }


def _search_reply(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return _ok(trn_infos={"trn_info": rows})


def _reserve_reply(pnr: str = SYNTHETIC_PNR, **extra: Any) -> dict[str, Any]:
    return _ok(
        h_pnr_no=pnr,
        h_jrny_cnt="1",
        h_wct_no="SYNTHETIC_WCT",
        h_tmp_job_sqno1="SYNTHETIC_JOB_1",
        h_tmp_job_sqno2="SYNTHETIC_JOB_2",
        h_tot_prc="59800",
        h_tot_rcvd_amt="59800",
        h_ntisu_lmt_dt="20990101",
        h_ntisu_lmt_tm="121000",
        jrny_infos={"jrny_info": [{"h_jrny_sqno": "0001", "h_rsv_chg_no": "001"}]},
        **extra,
    )


class _Recorder:
    """경로별 응답을 돌려주고, 나간 요청을 순서대로 기록합니다.

    ``sequences`` 에 담긴 경로는 부를 때마다 다음 응답으로 넘어갑니다 — 매진이
    이어지다 자리가 열리는 상황을 그대로 만들 수 있습니다.
    """

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
        if body is None:  # pragma: no cover - 시험 배선 실수를 잡는 가드
            raise AssertionError(f"unexpected {request.method} {path}")
        return httpx.Response(200, json=body)

    def count(self, path: str) -> int:
        return self.seen.count(path)


def _client(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch) -> KorailClient:
    client = KorailClient(transport=httpx.MockTransport(recorder))
    client.session.current = KorailSession(jsessionid="synthetic-session")

    def _fake_login(*_args: Any, **_kwargs: Any) -> KorailSession:
        session = KorailSession(jsessionid="synthetic-session")
        client.session.current = session
        return session

    monkeypatch.setattr(client, "login", _fake_login)
    monkeypatch.setattr(wr, "read_credentials_from_env", lambda: ("member", "pw"))
    return client


def _plan(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "query": TrainSearchQuery("서울", "부산", "20990101"),
        "passengers": KorailPassengerCounts(adult=1),
        "seat_class": KorailSeatClass.GENERAL,
        "poll_interval_s": 0.0,
        "max_pages": 1,
        "deadline": None,
    }
    defaults.update(overrides)
    return wr.WatchPlan(**defaults)


def _watcher(client: KorailClient, plan: Any, *, live: bool) -> Any:
    return wr.Watcher(client, wr._Console(), plan, live=live)


# --- import 안전성 -------------------------------------------------------------


def test_module_level_code_is_only_definitions_and_constants():
    tree = ast.parse(SCRIPT_SOURCE)
    for node in tree.body:
        if isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Assign,
                ast.AnnAssign,
                ast.Expr,  # 모듈 docstring
            ),
        ):
            if isinstance(node, ast.Expr):
                assert isinstance(node.value, ast.Constant), ast.dump(node)
            continue
        assert isinstance(node, ast.If), ast.dump(node)
        assert ast.unparse(node.test) == "__name__ == '__main__'"


def test_importing_reads_no_environment_variable_and_opens_no_file(
    monkeypatch: pytest.MonkeyPatch,
):
    class _Poisoned(dict):
        def __getitem__(self, key):  # pragma: no cover - 돌면 안 된다
            raise AssertionError(f"import read os.environ[{key!r}]")

        def get(self, key, default=None):  # pragma: no cover
            raise AssertionError(f"import read os.environ.get({key!r})")

    def _no_open(*args, **kwargs):  # pragma: no cover
        raise AssertionError("import opened a file")

    monkeypatch.setattr(os, "environ", _Poisoned())
    monkeypatch.setattr("builtins.open", _no_open)
    module = _load_script("watch_and_reserve_import_probe")
    assert module.DEFAULT_POLL_INTERVAL_S == 30.0


# --- consent 는 예약 하나뿐 ------------------------------------------------------


def test_consent_opens_reserve_only():
    for live in (False, True):
        consent = wr.reserve_consent(live=live)
        assert consent.allow_reserve
        assert not consent.allow_payment
        assert not consent.allow_cancel
        assert not consent.allow_refund
        assert not consent.allow_discount_card
        assert not consent.allow_cart
        assert not consent.allow_price_recalculation
        assert consent.fake_card_only
        assert not consent.real_card_acknowledged
    assert wr.reserve_consent(live=False).dry_run is True
    assert wr.reserve_consent(live=True).dry_run is False


def test_the_script_never_names_a_money_moving_symbol():
    """카드도 결제도 이 파일에 들어오지 않는다는 것을 원문으로 못박습니다.

    ``allow_payment`` 같은 철자는 :func:`reserve_consent` 의 단언에 나오므로
    막는 것은 **켜는 형태** 입니다.
    """
    for forbidden in (
        "allow_payment=True",
        "allow_refund=True",
        "allow_cancel=True",
        "allow_cart=True",
        "real_card_acknowledged=True",
        "fake_card_only=False",
        "CardPayment",
        "pay_with_card",
        "pay_with_fake_card",
        ".refund(",
        "cancel_unpaid_hold(",
    ):
        assert forbidden not in SCRIPT_SOURCE, forbidden


# --- 스위치 --------------------------------------------------------------------


def test_nothing_starts_without_the_package_live_switch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KORAIL_MOBILE_API_LIVE", raising=False)
    with pytest.raises(wr.WatchAborted, match="KORAIL_MOBILE_API_LIVE"):
        wr.require_opt_ins(live=False)


def test_reserving_needs_the_mutation_switch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KORAIL_MOBILE_API_LIVE", "1")
    monkeypatch.delenv(wr.LIVE_MUTATION_ENV, raising=False)
    with pytest.raises(wr.WatchAborted, match=wr.LIVE_MUTATION_ENV):
        wr.require_opt_ins(live=True)
    # 조회만 하는 실행은 그 스위치 없이도 됩니다.
    wr.require_opt_ins(live=False)


def test_watch_only_run_sends_no_reservation(monkeypatch: pytest.MonkeyPatch):
    recorder = _Recorder({SEARCH: _search_reply([_train_row("00101", general="11")])})
    client = _client(recorder, monkeypatch)
    assert _watcher(client, _plan(), live=False).run() == wr.EXIT_OK
    assert recorder.count(RESERVE) == 0
    assert recorder.count(SEARCH) == 1


# --- 잡는 순간 -----------------------------------------------------------------


def test_sold_out_is_watched_and_the_open_seat_is_taken_once(
    monkeypatch: pytest.MonkeyPatch,
):
    recorder = _Recorder(
        replies={RESERVE: _reserve_reply()},
        sequences={
            SEARCH: [
                _search_reply([_train_row("00101"), _train_row("00103")]),
                _search_reply([_train_row("00101"), _train_row("00103")]),
                _search_reply(
                    [_train_row("00101"), _train_row("00103", general="11")]
                ),
            ]
        },
    )
    client = _client(recorder, monkeypatch)
    assert _watcher(client, _plan(), live=True).run() == wr.EXIT_OK
    assert recorder.count(SEARCH) == 3
    # 딱 한 번. 재시도한 예약은 중복 예약이다.
    assert recorder.count(RESERVE) == 1
    assert recorder.seen[-1] == RESERVE


def test_a_seat_lost_between_search_and_reserve_keeps_watching(
    monkeypatch: pytest.MonkeyPatch,
):
    """예약 순간에 매진이 되면 다음 열차로 넘어가고, 없으면 계속 지켜봅니다."""
    recorder = _Recorder(
        replies={
            SEARCH: _search_reply([_train_row("00101", general="11")]),
            RESERVE: _fail("ERR211161", "매진"),
        }
    )
    client = _client(recorder, monkeypatch)
    plan = _plan(poll_interval_s=0.01, deadline=time.monotonic() + 0.2)
    assert _watcher(client, plan, live=True).run() == wr.EXIT_NOT_FOUND
    assert recorder.count(RESERVE) >= 1
    assert recorder.count(SEARCH) >= 1


def test_a_run_that_finds_nothing_ends_with_the_not_found_code(
    monkeypatch: pytest.MonkeyPatch,
):
    recorder = _Recorder({SEARCH: _search_reply([_train_row("00101")])})
    client = _client(recorder, monkeypatch)
    plan = _plan(poll_interval_s=0.01, deadline=time.monotonic() + 0.15)
    assert _watcher(client, plan, live=True).run() == wr.EXIT_NOT_FOUND
    assert recorder.count(RESERVE) == 0


def test_an_empty_result_is_not_a_failure(monkeypatch: pytest.MonkeyPatch):
    """``WRD000061``(직통 없음)은 조건이 아직 안 맞는 것이지 오류가 아닙니다."""
    recorder = _Recorder({SEARCH: _fail("WRD000061", "직통열차가 없습니다")})
    client = _client(recorder, monkeypatch)
    plan = _plan(poll_interval_s=0.01, deadline=time.monotonic() + 0.1)
    assert _watcher(client, plan, live=True).run() == wr.EXIT_NOT_FOUND


def test_an_expired_session_logs_in_again_and_carries_on(
    monkeypatch: pytest.MonkeyPatch,
):
    logins: list[int] = []
    recorder = _Recorder(
        replies={RESERVE: _reserve_reply()},
        sequences={
            SEARCH: [
                _fail("P058", "세션이 만료되었습니다"),
                _search_reply([_train_row("00101", general="11")]),
            ]
        },
    )
    client = _client(recorder, monkeypatch)
    original_login = client.login

    def _counting_login(*args: Any, **kwargs: Any) -> KorailSession:
        logins.append(1)
        return original_login(*args, **kwargs)

    monkeypatch.setattr(client, "login", _counting_login)
    assert _watcher(client, _plan(), live=True).run() == wr.EXIT_OK
    assert len(logins) == 2  # 처음 한 번, 만료 뒤 한 번
    assert recorder.count(RESERVE) == 1


def test_repeated_session_loss_stops_instead_of_looping(
    monkeypatch: pytest.MonkeyPatch,
):
    recorder = _Recorder({SEARCH: _fail("P058", "세션이 만료되었습니다")})
    client = _client(recorder, monkeypatch)
    with pytest.raises(wr.WatchAborted, match="세션"):
        _watcher(client, _plan(), live=True).run()
    assert recorder.count(SEARCH) == wr.MAX_RELOGIN + 1


# --- 예약대기 ------------------------------------------------------------------


def test_standby_is_not_attempted_unless_asked(monkeypatch: pytest.MonkeyPatch):
    recorder = _Recorder(
        {SEARCH: _search_reply([_train_row("00101", h_wait_rsv_flg=" 9")])}
    )
    client = _client(recorder, monkeypatch)
    plan = _plan(poll_interval_s=0.01, deadline=time.monotonic() + 0.1)
    assert _watcher(client, plan, live=True).run() == wr.EXIT_NOT_FOUND
    assert recorder.count(RESERVE) == 0


def test_standby_holds_and_then_confirms(monkeypatch: pytest.MonkeyPatch):
    recorder = _Recorder(
        {
            SEARCH: _search_reply([_train_row("00101", h_wait_rsv_flg=" 9")]),
            RESERVE: _reserve_reply(h_msg_cd="IRR000014"),
            STANDBY: _ok(),
        }
    )
    client = _client(recorder, monkeypatch)
    assert _watcher(client, _plan(allow_standby=True), live=True).run() == wr.EXIT_OK
    assert recorder.count(RESERVE) == 1
    assert recorder.count(STANDBY) == 1


def test_standby_without_the_confirmation_code_leaves_the_hold_alone(
    monkeypatch: pytest.MonkeyPatch,
):
    recorder = _Recorder(
        {
            SEARCH: _search_reply([_train_row("00101", h_wait_rsv_flg=" 9")]),
            RESERVE: _reserve_reply(),  # IRR000014 가 아님
        }
    )
    client = _client(recorder, monkeypatch)
    assert _watcher(client, _plan(allow_standby=True), live=True).run() == wr.EXIT_OK
    assert recorder.count(STANDBY) == 0


def test_standby_is_refused_for_the_special_cabin(monkeypatch: pytest.MonkeyPatch):
    """예약대기는 일반실 탭에만 있습니다. 특실 감시는 시도조차 하지 않습니다."""
    recorder = _Recorder(
        {SEARCH: _search_reply([_train_row("00101", h_wait_rsv_flg=" 9")])}
    )
    client = _client(recorder, monkeypatch)
    plan = _plan(
        allow_standby=True,
        seat_class=KorailSeatClass.SPECIAL,
        poll_interval_s=0.01,
        deadline=time.monotonic() + 0.1,
    )
    assert _watcher(client, plan, live=True).run() == wr.EXIT_NOT_FOUND
    assert recorder.count(RESERVE) == 0


# --- 고르는 규칙 ---------------------------------------------------------------


def _summary(**raw: Any) -> TrainSummary:
    return TrainSummary.from_raw(_train_row("00123", **raw))


def test_availability_reads_the_cabin_that_is_being_booked():
    train = TrainSummary.from_raw(
        _train_row("00123", general="13", h_spe_rsv_cd="11")
    )
    assert not wr.is_reservable(train, KorailSeatClass.GENERAL)
    assert wr.is_reservable(train, KorailSeatClass.SPECIAL)


def test_only_eleven_counts_as_available():
    for code in ("13", "10", "", "1", "111"):
        train = TrainSummary.from_raw(_train_row("00123", general=code))
        assert not wr.is_reservable(train, KorailSeatClass.GENERAL), code


def test_a_departure_time_that_lost_its_leading_zero_still_matches():
    """서버는 ``h_dpt_tm`` 을 JSON 숫자로 보내기도 합니다 — ``63000``.

    문자열로 그냥 비교하면 ``"63000" > "120000"`` 이라 06:30 열차가 오전 시간창
    밖으로 밀려납니다.
    """
    train = TrainSummary.from_raw(_train_row("00123", departure_time=63000))
    assert wr.departure_time_of(train) == "063000"
    plan = _plan(depart_after="060000", depart_before="120000")
    assert wr.matches(train, plan)


def test_time_window_excludes_what_is_outside_it():
    plan = _plan(depart_after="080000", depart_before="120000")
    assert not wr.matches(_summary(departure_time="070000"), plan)
    assert wr.matches(_summary(departure_time="080000"), plan)
    assert wr.matches(_summary(departure_time="120000"), plan)
    assert not wr.matches(_summary(departure_time="120001"), plan)


def test_train_numbers_match_with_or_without_leading_zeros():
    plan = _plan(train_numbers=frozenset({wr.normalize_train_no("123")}))
    assert wr.matches(_summary(), plan)  # 행은 "00123"
    assert not wr.matches(TrainSummary.from_raw(_train_row("00124")), plan)


def test_train_name_is_a_substring_filter():
    assert wr.matches(_summary(), _plan(train_name="ktx"))
    assert not wr.matches(_summary(), _plan(train_name="ITX"))


def test_pagination_stops_at_the_end_of_the_window(monkeypatch: pytest.MonkeyPatch):
    """시간창을 지난 행이 나오면 다음 페이지를 더 넘기지 않습니다."""
    recorder = _Recorder(
        {
            SEARCH: _ok(
                trn_infos={
                    "trn_info": [
                        _train_row("00101", departure_time="080000"),
                        _train_row("00103", departure_time="140000"),
                    ]
                },
                h_next_pg_flg="Y",
                h_next_qry_st_no="1",
                h_next_qry_st_trn_no="00103",
                h_page_cnt="10",
            )
        }
    )
    client = _client(recorder, monkeypatch)
    plan = _plan(depart_before="120000", max_pages=5)
    found = wr.collect_candidates(client, plan)
    assert recorder.count(SEARCH) == 1
    assert [train.train_no for train in found] == ["00101"]


# --- 입력 검증 -----------------------------------------------------------------


def _args(**overrides: Any) -> Any:
    argv = [
        "--from",
        "서울",
        "--to",
        "부산",
        "--date",
        "20990101",
    ]
    for key, value in overrides.items():
        flag = f"--{key.replace('_', '-')}"
        if value is True:
            argv.append(flag)
        else:
            argv.extend([flag, str(value)])
    return wr.build_parser().parse_args(argv)


def test_the_poll_interval_has_a_floor():
    with pytest.raises(wr.WatchAborted, match="interval"):
        wr.build_plan(_args(interval=1))
    plan = wr.build_plan(_args(interval=wr.MIN_POLL_INTERVAL_S))
    assert plan.poll_interval_s == wr.MIN_POLL_INTERVAL_S


def test_the_request_spacing_has_a_floor():
    with pytest.raises(wr.WatchAborted, match="min-interval"):
        wr.build_plan(_args(min_interval=0.2))


def test_a_past_date_is_refused():
    with pytest.raises(wr.WatchAborted, match="지난 날짜"):
        wr.build_plan(_args(date="20200101"))
    with pytest.raises(wr.WatchAborted, match="YYYYMMDD"):
        wr.build_plan(_args(date="2099-01-01"))


def test_clock_values_are_accepted_in_the_three_shapes_people_type():
    assert wr.parse_clock("08:30", flag="--after") == "083000"
    assert wr.parse_clock("0830", flag="--after") == "083000"
    assert wr.parse_clock("083015", flag="--after") == "083015"
    assert wr.parse_clock("", flag="--after") == ""
    for bad in ("8", "25:00", "08:70", "hello"):
        with pytest.raises(wr.WatchAborted):
            wr.parse_clock(bad, flag="--after")


def test_a_window_that_ends_before_it_starts_is_refused():
    with pytest.raises(wr.WatchAborted, match="--after"):
        wr.build_plan(_args(after="12:00", before="08:00"))


def test_the_search_starts_at_the_window_and_carries_every_passenger():
    plan = wr.build_plan(_args(after="08:00", adult=2, child=1))
    assert plan.query.departure_time == "080000"
    assert plan.passengers.total == 3
    assert plan.query.passengers == 3


def test_too_many_passengers_is_refused_before_anything_goes_out():
    with pytest.raises(wr.WatchAborted, match="승객"):
        wr.build_plan(_args(adult=9, child=2))


def test_device_identity_is_all_three_values_or_none(monkeypatch: pytest.MonkeyPatch):
    for name in (wr.DEVICE_ID_ENV, wr.OS_VERSION_ENV, wr.DEVICE_MODEL_ENV):
        monkeypatch.delenv(name, raising=False)
    config, identity = wr.build_config()
    assert config.dynapath.enabled
    assert "합성" in identity

    monkeypatch.setenv(wr.DEVICE_ID_ENV, "0123456789abcdef")
    with pytest.raises(wr.WatchAborted, match=wr.OS_VERSION_ENV):
        wr.build_config()

    monkeypatch.setenv(wr.OS_VERSION_ENV, "15")
    monkeypatch.setenv(wr.DEVICE_MODEL_ENV, "SM-S928N")
    config, identity = wr.build_config()
    assert config.dynapath.token_settings.device_id == "0123456789abcdef"
    assert "환경변수" in identity
