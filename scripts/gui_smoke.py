#!/usr/bin/env python3
"""창을 **실제로 띄워** 화면과 동작을 확인합니다. 네트워크는 쓰지 않습니다.

왜 따로 있는가 — `tests/test_korail_booker.py` 의 화면 시험은 전부 소스를
읽어 확인합니다. 시험 환경에 tkinter 가 없기 때문입니다(그래서
`korail_booker.ui` 는 import 조차 되지 않습니다). 소스 확인은 "그렇게 쓰여
있다" 까지만 말해 주고, **그려 놓으니 잘리더라** 는 말해 주지 못합니다.
실제로 그렇게 [조회] 단추가 잘리고, 예매 대상의 촉박 경고가 표 밖으로 밀려나
있었습니다.

그래서 화면이 있는 곳에서 손으로 돌립니다::

    xvfb-run -a --server-args="-screen 0 1600x1200x24" python3 scripts/gui_smoke.py
    xvfb-run -a python3 scripts/gui_smoke.py --shot /tmp/main.png

확인하는 것:

* 창이 뜨고, 묶음마다 제 최소 높이를 지키는지(단추가 잘리지 않는지).
* 손으로 넣은 환승역을 [후보 갱신]·[조회] 가 지우지 않는지.
* 묶음의 부모 줄을 고르면 그 아래 조합이 전부 담기는지.
* 예매 대상의 상태·환승 대기 칸이 구간별 예약과 촉박한 환승을 말하는지.
* 텔레그램 [이번만 쓰기] 가 설정 파일을 건드리지 않는지.

끝에 몇 개가 통과했는지 적고, 하나라도 어긋나면 종료 코드 1 입니다.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "app")]


def _raw_train(
    train_no: str,
    *,
    departure: str,
    arrival: str,
    departure_code: str,
    arrival_code: str,
    departure_time: str,
    arrival_time: str,
) -> dict[str, str]:
    """조회 응답 한 줄. 시험 픽스처와 같은 필드를 채웁니다.

    시험 쪽에서 가져다 쓰지 않는 것은 이 스크립트가 ``tests/`` 없이도 돌아야
    하기 때문입니다 — 설치본에는 시험이 들어가지 않습니다.
    """
    return {
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
        "h_trn_clsf_nm": "KTX",
        "h_dpt_stn_run_ordr": "1",
        "h_arv_stn_run_ordr": "2",
        "h_dpt_stn_cons_ordr": "1",
        "h_arv_stn_cons_ordr": "2",
        "h_seat_att_cd": "015",
        "h_gen_rsv_cd": "11",
        "h_spe_rsv_cd": "11",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shot", help="이 경로에 화면을 찍습니다(ImageMagick import).")
    args = parser.parse_args()

    # 진짜 설정 파일을 건드리지 않습니다. 저장 갈래를 눌러 보기 때문입니다.
    # HOME 만 바꾸면 모자랍니다 — settings_dir() 는 XDG_CONFIG_HOME 을 먼저
    # 보고, 윈도우에서는 APPDATA 를 봅니다. 그것을 안 막으면 진짜 설정을
    # 읽어 **토큰을 화면에 찍고**, 저장 갈래가 그 파일을 덮어씁니다.
    sandbox = tempfile.mkdtemp(prefix="korail-gui-smoke-")
    for name in ("HOME", "XDG_CONFIG_HOME", "APPDATA"):
        os.environ[name] = sandbox

    import tkinter as tk
    import types
    from tkinter import ttk

    from korail_booker import ui as ui_module
    from korail_booker.autobook import Target
    from korail_booker.holds import Held
    from korail_booker.journeys import Journey, JourneySource
    from korail_booker.search import TRANSFER_CUSTOM, SearchRequest

    from korail_mobile_api import KorailPassengerCounts, KorailSeatClass, TrainSummary

    def train(**kwargs: str) -> TrainSummary:
        return TrainSummary.from_raw(_raw_train(**kwargs))

    request = SearchRequest(
        departure="동탄", arrival="동대구", date="20990101",
        passengers=KorailPassengerCounts(adult=1),
    )
    first = train(train_no="00301", departure="동탄", arrival="대전",
                  departure_code="0001", arrival_code="0010",
                  departure_time="054700", arrival_time="062800")

    def combination(number: str, departs: str, arrives: str) -> Journey:
        second = train(train_no=number, departure="대전", arrival="동대구",
                       departure_code="0010", arrival_code="0015",
                       departure_time=departs, arrival_time=arrives)
        return Journey(legs=(first, second), source=JourneySource.CUSTOM_TRANSFER)

    results = [
        Target(journey=combination("00301", "063000", "071200"), request=request),
        Target(journey=combination("00003", "063400", "072200"), request=request),
        Target(journey=combination("00203", "064900", "073100"), request=request),
    ]

    # 대화상자를 가로챕니다. 그대로 두면 모달이 제 이벤트 고리를 돌려 이
    # 스크립트가 사람이 [확인] 을 누를 때까지 영영 멈춥니다 — 실제로 그랬습니다.
    # 가로채 두면 "그 창이 떴는가" 까지 확인할 수 있습니다.
    shown: list[tuple[str, str]] = []

    def _record(title: str = "", message: str = "", **_kw: object) -> str:
        shown.append((title, message))
        return "ok"

    for _name in ("showinfo", "showwarning", "showerror"):
        setattr(ui_module.messagebox, _name, _record)
    ui_module.messagebox.askyesno = lambda *_a, **_k: True  # type: ignore[assignment]

    root = tk.Tk()
    # 로그인 팝업은 켜자마자 뜨는 것은 이 확인의 대상이 아닙니다 — 본 창을
    # 막으면 아무것도 못 누릅니다. 진짜 구현은 따로 챙겨 뒀다가 아래에서
    # 손으로 부릅니다(감시 중 잠금·하이픈 안내·[비로그인] 초기화 확인).
    real_open_login = ui_module.BookerApp.open_login
    ui_module.BookerApp.open_login = lambda self: None  # type: ignore[method-assign]
    # 역 목록도 막습니다. 켜자마자 진짜 KORAIL 요청이 나가고, 실패하면 모달
    # 오류창이 떠서 이 스크립트가 영영 안 끝납니다.
    ui_module.BookerApp.on_load_stations = lambda self: None  # type: ignore[method-assign]
    app = ui_module.BookerApp(root)
    # 창이 처음 그려진 뒤에야 확정되는 것이 있습니다 — 화면 크기에 맞춘 첫
    # 크기와, 그 크기로 다시 잰 칸별 최소 높이. 한 박자 돌리고 봅니다.
    root.update()
    root.after(200, root.quit)
    root.mainloop()

    passed: list[bool] = []

    def check(name: str, condition: bool, detail: object = "") -> None:
        passed.append(bool(condition))
        mark = "PASS" if condition else "FAIL"
        tail = f"  — {detail}" if detail != "" else ""
        print(f"{mark}  {name}{tail}")

    # -- 단추가 있는 묶음이 제 높이를 받는가 --------------------------------
    #
    # 숫자를 손으로 준 칸(표·기록)은 **일부러** 요구 높이보다 작습니다 —
    # 줄이면 보이는 줄 수만 줄어듭니다. 스스로 재는 칸만 봅니다: 그쪽은
    # 줄어들면 단추가 잘립니다.
    for parent, frame, given_minsize in app._panes:
        if given_minsize is not None:
            continue
        need = frame.winfo_reqheight()
        given = int(parent.panecget(frame, "minsize"))
        check(f"단추가 있는 칸이 요구 높이({need})만큼 받는다", given >= need,
              f"minsize={given}")

    # -- 손으로 넣은 환승역이 살아남는가 ------------------------------------
    app.include_transfer.set(True)
    app.transfer_mode.set(TRANSFER_CUSTOM)
    app.sync_transfer_state()
    app.station_names = ("대전", "서대전", "김천구미", "동대구", "오송")
    for name in ("오송", "서대전"):
        app.transfer_query.set(name)
        app.add_transfer_station()
    by_hand = app.transfer_names()
    app._transfer_stations_loaded(["대전", "김천구미"])
    check("[후보 갱신] 이 손으로 넣은 역을 지우지 않는다",
          set(by_hand) <= set(app.transfer_names()), app.transfer_names())
    check("서버도 준 역에는 (검증) 이 붙는다",
          "대전 (검증)" in [app.transfer_list.get(i)
                          for i in range(app.transfer_list.size())])
    kept = app.transfer_names()
    app._server_candidates_loaded(("동탄", "동대구"), ["부산"])
    check("[조회] 가 목록을 덮지 않는다", app.transfer_names() == kept,
          app.transfer_names())
    app.clear_transfer_stations()
    check("[비우기] 가 목록을 비운다", app.transfer_list.size() == 0)
    app._server_candidates_loaded(("동탄", "동대구"), ["대전", "김천구미"])
    check("비어 있으면 조회가 채운다",
          app.transfer_names() == ("대전", "김천구미"), app.transfer_names())

    # -- 조회 중 mid-search 환승역 후보 갱신이 잠금을 풀지 않는가 -----------
    #
    # 실제로 있었던 문제: [조회] 를 누르면 환승 조건 칸이 잠기는데, 조회
    # 스레드가 그 구간의 환승역 후보를 받아 와 화면 상태를 다시 맞추는
    # 순간(sync_transfer_state) 그 칸이 도로 골라지게 바뀌었습니다. ttk
    # 위젯은 .cget('state') 가 .state(['disabled']) 를 반영하지 않으므로
    # (별개의 메커니즘입니다) .instate(['disabled']) 로 확인해야 합니다.
    app._searching(True)
    root.update()
    check("조회를 시작하면 서버 추천 라디오가 실제로 잠긴다(instate)",
          app.server_radio.instate(["disabled"]))
    check("환승역 목록도 잠긴다", app.transfer_list.cget("state") == "disabled")
    app._server_candidates_loaded(("동탄", "동대구"), ["대전", "김천구미"])
    root.update()
    check("조회 중 mid-search 갱신 뒤에도 라디오가 계속 잠겨 있다",
          app.server_radio.instate(["disabled"]))
    check("조회 중 mid-search 갱신 뒤에도 환승역 목록이 계속 잠겨 있다",
          app.transfer_list.cget("state") == "disabled",
          app.transfer_list.cget("state"))
    check("조회 중 mid-search 갱신 뒤에도 [후보 갱신] 단추가 계속 잠겨 있다",
          app.transfer_load_button.instate(["disabled"]))
    app._transfer_stations_loaded(["대전", "오송"])
    root.update()
    check("[구간 후보 갱신] 콜백 뒤에도 계속 잠겨 있다",
          app.transfer_load_button.instate(["disabled"])
          and app.transfer_list.cget("state") == "disabled")
    app._searching(False)
    root.update()
    check("조회가 끝나면 풀린다", not app.server_radio.instate(["disabled"])
          and app.transfer_list.cget("state") == "normal")

    class _Click:
        def __init__(self, x: int, y: int, widget: object) -> None:
            self.x, self.y, self.widget = x, y, widget

    # -- 왕복: 오는 날짜가 가는 날짜보다 앞서지 않는가 -----------------------
    app.round_trip.set(True)
    app._round_trip_toggled()
    app.date.set("2026-09-10")
    app.return_date.set("2026-09-10")
    root.update()
    app.date.set("2026-09-15")
    root.update()
    check("가는 날짜를 뒤로 미루면 오는 날짜도 따라간다",
          app.return_date.get() == "2026-09-15", app.return_date.get())
    app.open_calendar(for_return=True)
    root.update()
    nine = next(
        w for w in app.calendar._grid.winfo_children() if w.cget("text") == "14"
    )
    check("오는 날 달력에서 가는 날보다 이전인 날은 잠긴다",
          nine.instate(["disabled"]))
    app.calendar.hide()
    app.round_trip.set(False)
    app._round_trip_toggled()
    root.update()

    # -- 묶음과 담기 ---------------------------------------------------------
    def find_widgets(
        widget: tk.Misc, predicate: object, found: list | None = None
    ) -> list:
        if found is None:
            found = []
        for child in widget.winfo_children():
            try:
                if predicate(child):  # type: ignore[operator]
                    found.append(child)
            except tk.TclError:
                pass
            find_widgets(child, predicate, found)
        return found

    app._show_journeys(results)
    root.update()
    parent_row = app.tree.get_children()[0]
    check("부모 줄은 여정이 아니라 묶음 머리다",
          app.tree.item(parent_row, "tags") == ("group",))
    app.tree.selection_set(parent_row)
    check("부모를 고르면 그 아래 조합이 전부 (조회용으로는) 잡힌다",
          len(app.selected_results()) == len(results), len(app.selected_results()))
    app.tree.selection_set(app.tree.get_children(parent_row)[1])
    check("자식만 고르면 그 하나만", len(app.selected_results()) == 1)

    # 부모(묶음 머리)를 고르고 [담기] 를 눌러도 **곧장 담기지 않습니다** —
    # 이어지는 2구간이 전부(고른 적 없는 것까지) 담기던 것을 그만뒀습니다.
    app.tree.selection_set(parent_row)
    before_windows = set(root.winfo_children())
    app.add_targets()
    root.update()
    check("묶음 머리를 고르고 담아도 곧장 담기지 않는다",
          len(app.targets) == 0, len(app.targets))
    new_windows = [
        w for w in root.winfo_children()
        if w not in before_windows and isinstance(w, tk.Toplevel)
    ]
    check("대신 고르는 팝업이 뜬다", len(new_windows) == 1, new_windows)
    picker = new_windows[0]
    check("팝업 제목이 '이어지는 구간 고르기' 다",
          picker.title() == "이어지는 구간 고르기", picker.title())
    picker_trees = find_widgets(picker, lambda w: isinstance(w, ttk.Treeview))
    picker_tree = picker_trees[0] if picker_trees else None
    picker_rows = picker_tree.get_children() if picker_tree is not None else ()
    check("팝업 목록에 후보가 다 있다",
          picker_tree is not None and len(picker_rows) == len(results),
          len(picker_rows) if picker_tree is not None else None)
    if picker_tree is not None and picker_rows:
        picker_tree.selection_set(picker_rows[1])
        pick_buttons = find_widgets(
            picker, lambda w: isinstance(w, ttk.Button) and w.cget("text") == "고른 것 담기"
        )
        assert pick_buttons, "[고른 것 담기] 단추를 못 찾았습니다"
        pick_buttons[0].invoke()
        root.update()
    check("팝업에서 하나를 고르면 그제서야 1구간과 함께 담긴다",
          len(app.targets) == 1
          and len(app.targets[0].journey.legs) == len(results[0].journey.legs),
          len(app.targets))
    check("고르고 나면 팝업이 닫힌다", not picker.winfo_exists())

    # 자식(이어서) 줄만 고르면 팝업 없이 곧장 담깁니다.
    app.targets = []
    app.sync_target_list()
    children = app.tree.get_children(parent_row)
    app.tree.selection_set(children[0])
    before_windows = set(root.winfo_children())
    app.add_targets()
    root.update()
    check("자식 줄만 고르면 팝업 없이 곧장 담긴다",
          len(app.targets) == 1
          and not [
              w for w in root.winfo_children()
              if w not in before_windows and isinstance(w, tk.Toplevel)
          ],
          len(app.targets))

    # 예매 대상 묶음 표시를 보려면 자식 여럿을 한꺼번에 고릅니다.
    app.targets = []
    app.sync_target_list()
    app.tree.selection_set(*children)
    app.add_targets()
    root.update()
    # 예매 대상도 조회 결과처럼 1구간이 같으면 묶입니다.
    target_top = app.target_list.get_children()
    check("1구간이 같은 예매 대상은 한 부모 줄로 묶인다",
          len(target_top) == 1
          and app.target_list.item(target_top[0], "tags") == ("group",),
          target_top)
    target_children = app.target_list.get_children(target_top[0])
    check("그 아래 후보가 다 들어 있다", len(target_children) == len(results),
          len(target_children))
    row = app.target_list.item(target_children[0], "values")
    check("상태 칸이 구간별 예약임을 말한다", "구간별" in row[0], row[0])
    # 칸 순서: 상태, 구분, 열차, 출발역, 출발, 도착역, 도착, 총 소요, 환승 대기, ...
    check("환승 대기 칸이 촉박한 환승을 경고한다", "촉박" in row[8], row[8])

    # 부모 줄을 고르면 그 아래 전부를 고른 것으로 칩니다.
    app.target_list.selection_set(target_top[0])
    check("예매 대상 부모를 고르면 그 아래 전부가 골라진다",
          len(app.selected_indices()) == len(results), app.selected_indices())

    # +/- 자리를 두 번 눌러도 빼지 않습니다 — 접고 펴는 기본 동작만 삽니다.
    root.update()
    box = app.target_list.bbox(target_top[0])
    if box:
        top_y, height = int(box[1]), int(box[3])
        middle = top_y + height // 2
        before = len(app.targets)
        verdict = app._target_double_clicked(_Click(8, middle, app.target_list))
        root.update()
        check("예매 대상도 +/- 자리는 빼지 않는다(접고 펴기가 살아 있다)",
              verdict is None and len(app.targets) == before, f"return={verdict!r}")

        # 글자 자리를 두 번 누르면 묶음 부모 전체가 함께 빠집니다.
        verdict = app._target_double_clicked(_Click(120, middle, app.target_list))
        root.update()
        check("글자 자리를 두 번 누르면 묶음 전체가 빠진다",
              verdict == "break" and len(app.targets) == 0,
              f"return={verdict!r} targets={len(app.targets)}")

    # -- 두 번 눌러도 접히지 않는가, 더블클릭한 묶음 머리도 팝업으로 가는가 ----
    app._show_journeys(results)
    root.update()
    head = app.tree.get_children()[0]
    # bbox 는 (x, y, 폭, 높이). Tk 는 줄이 안 보이면 빈 문자열을 돌려줍니다.
    box = app.tree.bbox(head)
    assert box, "묶음 머리 줄이 화면에 보이지 않습니다"
    top, height = int(box[1]), int(box[3])
    middle = top + height // 2

    was_open = bool(app.tree.item(head, "open"))
    before_windows = set(root.winfo_children())
    verdict = app._result_double_clicked(_Click(120, middle, app.tree))
    root.update()
    check("글자 자리를 두 번 눌러도 접히거나 펴지지 않는다",
          verdict == "break" and bool(app.tree.item(head, "open")) == was_open,
          f"return={verdict!r} open={app.tree.item(head, 'open')}")
    check("더블클릭도 묶음 머리면 곧장 담기지 않고 팝업이 뜬다",
          len(app.targets) == 0, len(app.targets))
    doubled_windows = [
        w for w in root.winfo_children()
        if w not in before_windows and isinstance(w, tk.Toplevel)
    ]
    check("그 팝업도 같은 창이다", len(doubled_windows) == 1, doubled_windows)
    for w in doubled_windows:
        w.destroy()
    root.update()

    before = len(app.targets)
    verdict = app._result_double_clicked(_Click(8, middle, app.tree))
    root.update()
    check("+/- 자리는 막지 않는다(접고 펴기가 살아 있다)",
          verdict is None and len(app.targets) == before, f"return={verdict!r}")

    # -- 구간 정보 줄만 골라 담으면 그 구간 하나만 --------------------------
    # 1구간이 겹치지 않는 새 후보라, 묶이지 않고 조회 때처럼 '1구간'/'2구간'
    # 정보 줄이 그대로 펼쳐집니다.
    solo_first = train(train_no="00777", departure="동탄", arrival="대전",
                        departure_code="0001", arrival_code="0010",
                        departure_time="150000", arrival_time="160000")
    solo_second = train(train_no="00778", departure="대전", arrival="동대구",
                         departure_code="0010", arrival_code="0015",
                         departure_time="161000", arrival_time="171500")
    solo_journey = Journey(legs=(solo_first, solo_second),
                           source=JourneySource.CUSTOM_TRANSFER)
    solo_target = Target(journey=solo_journey, request=request)
    app._show_journeys([solo_target])
    root.update()
    solo_row = app.tree.get_children()[0]
    check("혼자인 후보는 묶이지 않는다",
          app.tree.item(solo_row, "tags") != ("group",))
    leg_rows = app.tree.get_children(solo_row)
    check("구간 정보 줄이 둘 펼쳐진다", len(leg_rows) == 2, len(leg_rows))

    app.targets = []
    app.tree.selection_set(leg_rows[0])
    app.add_targets()
    root.update()
    check("구간 정보 줄만 고르면 그 구간 하나만 담긴다",
          len(app.targets) == 1 and len(app.targets[0].journey.legs) == 1,
          [len(t.journey.legs) for t in app.targets])
    check("그 대상의 방향은 그 구간 자신의 역이다(전체 여정이 아니다)",
          app.targets and app.targets[0].direction == ("동탄", "대전", "20990101"),
          app.targets[0].direction if app.targets else None)

    app.targets = []
    app.tree.selection_set(leg_rows[1])
    app.add_targets()
    root.update()
    check("2구간 줄만 고르면 2구간 하나만 담긴다",
          app.targets and app.targets[0].direction == ("대전", "동대구", "20990101"),
          app.targets[0].direction if app.targets else None)

    # -- 좌석 체크박스: 기본값(무관)은 None, 하나라도 떼면 굳는다 ------------
    app.targets = []
    app.tree.selection_set(solo_row)
    app.add_targets()
    root.update()
    check("체크박스를 안 건드리면 좌석 선택이 옛 방식(None)이다",
          app.targets and app.targets[0].seat_choices is None,
          app.targets[0].seat_choices if app.targets else None)

    app.targets = []
    app.pick_leg1_special.set(False)  # 1구간은 일반실만
    app.pick_leg2_general.set(False)  # 2구간은 특실만
    app.tree.selection_set(solo_row)
    app.add_targets()
    root.update()
    check("1구간/2구간을 따로 고르면 그 여정에 굳는다",
          app.targets
          and app.targets[0].seat_choices
          == (frozenset({KorailSeatClass.GENERAL}), frozenset({KorailSeatClass.SPECIAL})),
          app.targets[0].seat_choices if app.targets else None)
    app.pick_leg1_special.set(True)
    app.pick_leg2_general.set(True)

    app.targets = []
    app.pick_leg1_general.set(False)
    app.pick_leg1_special.set(False)
    shown.clear()
    app.tree.selection_set(solo_row)
    app.add_targets()
    root.update()
    check("구간에 등급을 하나도 안 고르면 담지 않고 알린다",
          not app.targets and any("하나도 안 골랐습니다" in message for _t, message in shown),
          (app.targets, shown))
    app.pick_leg1_general.set(True)
    app.pick_leg1_special.set(True)

    # -- 좌석 등급 팝업: 평소엔 안 보이고, 옆 글자가 지금 값을 따라간다 -----
    check("체크박스를 안 건드리면 옆 글자가 '무관' 이다",
          app.seat_pick_summary.get() == "무관", app.seat_pick_summary.get())
    app.pick_special.set(False)
    root.update()
    check("체크박스를 바꾸면(코드로도) 옆 글자가 따라온다",
          app.seat_pick_summary.get() != "무관", app.seat_pick_summary.get())
    app.pick_special.set(True)
    root.update()

    before_windows = set(root.winfo_children())
    app.open_seat_pick_dialog()
    root.update()
    dialog_windows = [
        w for w in root.winfo_children()
        if w not in before_windows and isinstance(w, tk.Toplevel)
    ]
    check("[좌석 등급…] 을 누르면 팝업이 뜬다", len(dialog_windows) == 1, dialog_windows)
    if dialog_windows:
        checks_in_dialog = find_widgets(
            dialog_windows[0], lambda w: isinstance(w, ttk.Checkbutton)
        )
        check("팝업 안에 체크박스 여섯 개가 있다", len(checks_in_dialog) == 6,
              len(checks_in_dialog))
        dialog_windows[0].destroy()
    root.update()

    app.targets = []
    app.sync_target_list()

    # -- [바로 예약] 이 성공하면 예매 대상에서 빠지는가 ---------------------
    from korail_mobile_api import ReservationHoldResponse

    def fake_hold(pnr: str) -> ReservationHoldResponse:
        return ReservationHoldResponse(
            raw={}, pnr_no=pnr,
            payment_deadline_date="20990101", payment_deadline_time="120000",
            total_price="10000",
        )

    app.targets = [results[0]]
    app.sync_target_list()
    app._reserve_now_done(results[0], [fake_hold("SMOKE1")])
    root.update()
    check("[바로 예약] 으로 잡히면 예매 대상에서 빠진다",
          results[0] not in app.targets, app.targets)
    check("그러면서 잡은 예약에는 들어간다",
          any(h.pnr == "SMOKE1" for h in app.holds),
          [h.pnr for h in app.holds])

    # -- 1구간이 같고 2구간만 다른 조합은 서로를 막지 않는가 -----------------
    app.targets = []
    app.holds = []
    app.sync_target_list()
    app.sync_holds()
    app._reserve_now_done(results[0], [fake_hold("SMOKE2")])
    root.update()
    busy = app._busy_journeys()
    check("정확히 같은 조합은 막힌다", results[0].journey.key() in busy)
    check("1구간만 같고 2구간이 다른 조합은 막히지 않는다",
          results[1].journey.key() not in busy
          and results[2].journey.key() not in busy,
          [t.journey.key() in busy for t in results])
    app.targets = []
    app.holds = []
    app.sync_target_list()
    app.sync_holds()

    # -- 감시 중인 예매 대상은 빼거나 비울 수 없는가(감시 중이 아닌 것은 된다) --
    app.targets = [results[0], results[1]]
    app.sync_target_list()
    app.watches = [
        types.SimpleNamespace(
            running=True,
            keys=frozenset([results[0].journey.key()]),
            tag="A",
            options=types.SimpleNamespace(poll_interval_s=30.0),
            remaining=lambda now: "무제한",
        )
    ]  # type: ignore[list-item]
    app.target_list.selection_set(*app.target_list.get_children())
    app.remove_targets()
    root.update()
    check("감시 중인 예매 대상은 [빼기] 로 안 빠진다",
          results[0] in app.targets, app.targets)
    check("감시 중이 아닌 예매 대상은 [빼기] 로 빠진다",
          results[1] not in app.targets, app.targets)

    app.targets = [results[0], results[1]]
    app.sync_target_list()
    app.clear_targets()
    root.update()
    check("감시 중인 예매 대상은 [비우기] 로도 안 빠진다",
          app.targets == [results[0]], app.targets)
    app.watches = []
    app.targets = []
    app.sync_target_list()

    # 바인딩된 메서드는 볼 때마다 새 객체라 `is` 로는 비교되지 않습니다.
    check("알림 함수는 설정을 굽지 않는다",
          app._make_notifier().__func__ is type(app)._notify_now)

    # -- 텔레그램: 이번만 쓰기 -----------------------------------------------
    app.on_telegram_settings()
    root.update()
    window = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]

    def by_text(widget: tk.Misc, label: str) -> tk.Misc | None:
        for child in widget.winfo_children():
            try:
                if child.cget("text") == label:  # type: ignore[call-overload]
                    return child
            except tk.TclError:
                pass
            found = by_text(child, label)
            if found is not None:
                return found
        return None

    entries: list[tk.Misc] = []

    def collect(widget: tk.Misc) -> None:
        for child in widget.winfo_children():
            if child.winfo_class() == "TEntry":
                entries.append(child)
            collect(child)

    collect(window)
    # 비우고 넣습니다. 그냥 insert 하면 이미 있던 값 뒤에 붙어, 확인하려던
    # 값이 아니라 이어 붙은 쓰레기를 확인하게 됩니다.
    entries[0].delete(0, "end")  # type: ignore[attr-defined]
    entries[0].insert(0, "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ")  # type: ignore[attr-defined]
    entries[1].delete(0, "end")  # type: ignore[attr-defined]
    entries[1].insert(0, "987654321")  # type: ignore[attr-defined]
    once = by_text(window, "이번만 쓰기")
    check("[이번만 쓰기] 단추가 있다", once is not None)
    if once is not None:
        once.invoke()  # type: ignore[attr-defined]
        root.update()
    check("이번 실행 값이 잡힌다",
          app._telegram_once is not None
          and app._telegram_once.chat_id == "987654321", app._telegram_once)
    check("설정 파일에는 쓰지 않는다",
          app.settings.telegram_token == "" and app.settings.telegram_chat_id == "")
    check("알림이 그 값을 쓴다", app._make_notifier() is not None)

    # -- 잡은 예약: 조회 결과와 같은 칸으로 채워지는가, 묶이는가 ----------------
    HOLD_COLUMNS = ui_module.HOLD_COLUMNS
    PNR_COL = HOLD_COLUMNS.index("PNR")
    first_target = results[0]
    app.holds = [
        Held(
            label="", summary="[1구간] 서울 → 대전", pnr="P1001", fare="10000",
            deadline=None, deadline_text="모름", kind="좌석 예약(1구간)",
            group="batch-1", full_summary="서울 → 동대구",
            held_journey=first_target.journey,
        ),
        Held(
            label="", summary="[2구간] 대전 → 동대구", pnr="P1002", fare="12000",
            deadline=None, deadline_text="모름", kind="좌석 예약(2구간)",
            group="batch-1", full_summary="서울 → 동대구",
        ),
        # 묶지 않는(단독) 홀드 — 다른 batch 의 홀드와 섞이면 안 됩니다.
        Held(
            label="", summary="부산 → 광주", pnr="P2001", fare="20000",
            deadline=None, deadline_text="모름", group="batch-2",
        ),
    ]
    app.sync_holds()
    root.update()
    hold_top = app.hold_tree.get_children()
    check("같은 batch 의 구간별 홀드는 한 부모 줄로 묶인다",
          len(hold_top) == 2, hold_top)
    group_parent = next(
        item for item in hold_top
        if app.hold_tree.item(item, "tags") == ("group",)
    )
    group_children = app.hold_tree.get_children(group_parent)
    check("그 부모 아래 두 구간이 다 들어 있다", len(group_children) == 2,
          len(group_children))
    check("묶지 않는 홀드는 최상위에 혼자 남는다",
          len(hold_top) - 1 == 1
          and any(
              app.hold_tree.item(item, "values")[PNR_COL] == "P2001"
              for item in hold_top
          ))
    check("부모 줄은 선택해도 취소 대상이 되지 않는다(PNR 칸이 비어 있다)",
          app.hold_tree.item(group_parent, "values")[PNR_COL] == "")
    app.hold_tree.selection_set(group_parent)
    check("부모를 고르고 취소를 찾으면 못 찾는다(안전하게 막힌다)",
          app._selected_hold_index() is None)

    # held_journey 를 준 홀드는 조회 결과와 정확히 같은 아홉 칸을 보여야
    # 합니다 — 같은 함수(_journey_row_values)로 채우기 때문입니다. **이름으로
    # 각 칸을 짚어** 확인합니다 — 앞의 아홉 칸을 통째로 슬라이스만 하면,
    # '종류' 를 끝에 잘못 이어 붙여 뒤의 모든 칸이 한 칸씩 밀리는 버그를
    # 놓칩니다(실제로 그랬습니다 — 종류/열차 칸이 하나씩 밀려 보였습니다).
    p1001 = next(
        item for item in group_children
        if app.hold_tree.item(item, "values")[PNR_COL] == "P1001"
    )
    p1001_values = app.hold_tree.item(p1001, "values")
    expected_kind, *expected_rest = app._journey_row_values("", first_target.journey)
    rest_names = [
        "열차", "출발역", "출발", "도착역", "도착", "총 소요", "환승 대기",
        "일반실", "특실", "입석·자유석·대기",
    ]
    checks = {"구분": expected_kind, "종류": "좌석 예약(1구간)"}
    checks.update(dict(zip(rest_names, expected_rest, strict=True)))
    for name, expected_value in checks.items():
        col = HOLD_COLUMNS.index(name)
        check(f"잡은 예약의 '{name}' 칸이 조회 결과와 같다",
              p1001_values[col] == expected_value,
              (p1001_values[col], expected_value))

    # held_journey 가 없는 홀드는 지어내지 않고 그 칸들을 비웁니다.
    p1002 = next(
        item for item in group_children
        if app.hold_tree.item(item, "values")[PNR_COL] == "P1002"
    )
    p1002_values = app.hold_tree.item(p1002, "values")
    check("여정을 못 받은 홀드도 종류 칸은 채운다",
          p1002_values[HOLD_COLUMNS.index("종류")] == "좌석 예약(2구간)",
          p1002_values)
    check("여정을 못 받은 홀드는 좌석 칸을 '-' 로 비운다(지어내지 않는다)",
          p1002_values[HOLD_COLUMNS.index("열차")] == "-",
          p1002_values)

    app.holds = []
    app.sync_holds()

    # -- 서버에서 잡은 예약 불러오기 -----------------------------------------
    from korail_mobile_api import ReservationHistoryResponse, ReservationHistoryTrain

    def history_train(**kwargs: object) -> ReservationHistoryTrain:
        base = dict(
            departure_station="동탄", departure_time="054700",
            arrival_station="대전", arrival_time="062800",
            run_date="20990101", train_no="00301",
            train_class_code="00", train_class_name="KTX",
            reservation_type_code="1", payment_flag="N", settlement_flag="N",
            pnr_no="H0000001",
        )
        base.update(kwargs)
        return ReservationHistoryTrain(**base)

    # 이 세션이 이미 [바로 예약]으로 잡아 둔 것 하나 — 서버 조회로 덮이면
    # 안 됩니다(취소 버튼이 되는 hold_response 를 잃습니다).
    app.holds = [Held(
        label="", summary="세션 홀드", pnr="H0000001", fare="10000",
        deadline=None, deadline_text="모름",
        hold_response=fake_hold("H0000001"),
    )]
    response = ReservationHistoryResponse(items=(
        # 같은 PNR("H0000001")이지만 이미 세션이 아는 것이므로 무시돼야 함.
        history_train(pnr_no="H0000001"),
        # 새 단독(직통) 예약.
        history_train(pnr_no="H0000002", train_no="00101"),
        # 새 환승(한 PNR 에 구간 둘) 예약.
        history_train(pnr_no="H0000003", train_no="00201", arrival_station="대전"),
        history_train(pnr_no="H0000003", train_no="00202",
                      departure_station="대전", arrival_station="동대구",
                      departure_time="070000", arrival_time="080000"),
        # PNR 이 없는 행 — 지어낼 수 없으니 뺍니다.
        history_train(pnr_no=""),
    ))
    app._reservations_loaded(response, announce=False)
    root.update()
    check("이 세션이 이미 아는 PNR 은 서버 조회로 안 덮인다",
          sum(1 for h in app.holds if h.pnr == "H0000001") == 1
          and next(h for h in app.holds if h.pnr == "H0000001").hold_response is not None,
          [(h.pnr, h.hold_response is not None) for h in app.holds])
    check("PNR 없는 행은 빠지고, 새 PNR 둘만 불러와진다",
          len(app.holds) == 3, [h.pnr for h in app.holds])
    loaded_single = next(h for h in app.holds if h.pnr == "H0000002")
    check("단독 예약은 종류가 '불러온 예약' 이고 취소용 원본이 없다",
          loaded_single.kind == "불러온 예약" and loaded_single.hold_response is None,
          (loaded_single.kind, loaded_single.hold_response))
    check("결제 기한을 지어내지 않고 모른다고 적는다",
          loaded_single.deadline is None and "코레일 앱" in loaded_single.deadline_text,
          loaded_single.deadline_text)
    loaded_transfer = next(h for h in app.holds if h.pnr == "H0000003")
    check("한 PNR 에 구간 둘이면 환승(서버 조합)으로 표시된다",
          loaded_transfer.held_journey is not None
          and loaded_transfer.held_journey.is_transfer
          and loaded_transfer.held_journey.source is JourneySource.SERVER_TRANSFER,
          loaded_transfer.held_journey)

    item = app._hold_items[app.holds.index(loaded_transfer)]
    row = app.hold_tree.item(item, "values")
    check("불러온 환승 행도 조회 결과와 같은 칸(출발역 등)으로 보인다",
          row[HOLD_COLUMNS.index("출발역")] == "동탄"
          and row[HOLD_COLUMNS.index("도착역")] == "동대구",
          row)
    leg_rows = app.hold_tree.get_children(item)
    check("불러온 환승도 +/- 로 구간이 펼쳐진다", len(leg_rows) == 2, len(leg_rows))

    app.holds = []
    app.sync_holds()

    # -- 로그인 팝업: 감시 중 잠금, 하이픈 안내, [비로그인] 목록 초기화 ------
    # find_widgets 는 위 "묶음과 담기" 절에서 이미 정의했습니다.

    # 감시가 도는 중에는 팝업 자체가 열리지 않는다(로그아웃과 같은 방비).
    app.watches = [types.SimpleNamespace(running=True)]  # type: ignore[list-item]
    shown.clear()
    real_open_login(app)
    check(
        "감시 중에는 로그인 팝업이 안 열린다",
        app._login_window is None
        and any("자동예매가 돌고 있습니다" in message for _title, message in shown),
        shown,
    )
    app.watches = []

    # 감시가 없으면 뜬다 — 하이픈 안내와 [비로그인] 단추를 확인한다.
    real_open_login(app)
    root.update()
    window = app._login_window
    assert window is not None, "로그인 팝업이 뜨지 않았습니다"
    labels = find_widgets(window, lambda w: w.winfo_class() == "TLabel")
    check(
        "휴대폰번호는 하이픈 없이 넣으라고 안내한다",
        any("하이픈" in str(label.cget("text")) for label in labels),
    )
    buttons = find_widgets(window, lambda w: w.winfo_class() == "TButton")
    skip_button = next(
        (b for b in buttons if str(b.cget("text")) == "비로그인"), None
    )
    check("[비로그인] 단추가 있다", skip_button is not None)

    # 목록을 채워 두고 [비로그인] 을 누르면 조회·예매 대상·잡은 예약이 전부
    # 비어야 한다 — 이전 세션의 목록이 새 세션에 남으면 안 된다.
    app._show_journeys(results)
    root.update()
    # 묶음 머리는 이제 팝업을 열 뿐 곧장 담지 않으므로, 자식들을 골라 담습니다.
    app.tree.selection_set(*app.tree.get_children(app.tree.get_children()[0]))
    app.add_targets()
    root.update()
    app.holds = [
        Held(
            label="",
            summary="동탄 -> 동대구",
            pnr="P0000001",
            fare="10000",
            deadline=None,
            deadline_text="모름",
        )
    ]
    app.sync_holds()
    assert app.targets and app.holds and app.results, "목록을 못 채웠습니다"
    if skip_button is not None:
        skip_button.invoke()  # type: ignore[attr-defined]
    root.update()
    check(
        "[비로그인] 을 누르면 조회·예매 대상·잡은 예약이 모두 비워진다",
        not app.targets and not app.holds and not app.results and not app.journeys,
    )
    check("로그인 팝업이 닫힌다", app._login_window is None)

    # 로그인에 성공해도(다른 아이디로 로그인 포함) 같은 초기화가 걸린다.
    app._show_journeys(results)
    root.update()
    # 묶음 머리는 이제 팝업을 열 뿐 곧장 담지 않으므로, 자식들을 골라 담습니다.
    app.tree.selection_set(*app.tree.get_children(app.tree.get_children()[0]))
    app.add_targets()
    root.update()
    app.holds = [
        Held(
            label="",
            summary="동탄 -> 동대구",
            pnr="P0000002",
            fare="10000",
            deadline=None,
            deadline_text="모름",
        )
    ]
    app.sync_holds()
    app.login_id.set("tester")
    app._login_succeeded()
    check(
        "로그인에 성공해도 목록이 초기화된다",
        not app.targets and not app.holds and not app.results and not app.journeys,
    )

    if args.shot:
        root.update()
        root.after(300, root.quit)
        root.mainloop()
        # 셸을 거치지 않습니다(경로에 공백이나 따옴표가 있어도 안전).
        # 그리고 **정말 찍혔는지** 봅니다 — 예전에는 실패해도 찍었다고 적었습니다.
        taken = subprocess.run(
            ["import", "-window", "root", args.shot], check=False
        )
        if taken.returncode == 0:
            print(f"화면을 {args.shot} 에 찍었습니다.")
        else:
            print(f"화면을 찍지 못했습니다(import 종료 코드 {taken.returncode}).")

    root.destroy()
    print(f"\n{sum(passed)}/{len(passed)} 통과")
    return 0 if all(passed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
