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

    from korail_booker import ui as ui_module
    from korail_booker.autobook import Target
    from korail_booker.holds import Held
    from korail_booker.journeys import Journey, JourneySource
    from korail_booker.search import TRANSFER_CUSTOM, SearchRequest

    from korail_mobile_api import KorailPassengerCounts, TrainSummary

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

    class _Click:
        def __init__(self, x: int, y: int, widget: object) -> None:
            self.x, self.y, self.widget = x, y, widget

    # -- 묶음과 담기 ---------------------------------------------------------
    app._show_journeys(results)
    root.update()
    parent_row = app.tree.get_children()[0]
    check("부모 줄은 여정이 아니라 묶음 머리다",
          app.tree.item(parent_row, "tags") == ("group",))
    app.tree.selection_set(parent_row)
    check("부모를 고르면 그 아래 조합이 전부 담긴다",
          len(app.selected_results()) == len(results), len(app.selected_results()))
    app.tree.selection_set(app.tree.get_children(parent_row)[1])
    check("자식만 고르면 그 하나만", len(app.selected_results()) == 1)

    app.tree.selection_set(parent_row)
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
    check("환승 대기 칸이 촉박한 환승을 경고한다", "촉박" in row[2], row[2])

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
        # 되돌립니다 — 뒤의 확인들이 이 목록을 또 씁니다.
        app.tree.selection_set(parent_row)
        app.add_targets()
        root.update()

    # -- 두 번 눌러도 접히지 않는가 ------------------------------------------
    app._show_journeys(results)
    root.update()
    head = app.tree.get_children()[0]
    # bbox 는 (x, y, 폭, 높이). Tk 는 줄이 안 보이면 빈 문자열을 돌려줍니다.
    box = app.tree.bbox(head)
    assert box, "묶음 머리 줄이 화면에 보이지 않습니다"
    top, height = int(box[1]), int(box[3])
    middle = top + height // 2

    was_open = bool(app.tree.item(head, "open"))
    verdict = app._result_double_clicked(_Click(120, middle, app.tree))
    root.update()
    check("글자 자리를 두 번 눌러도 접히거나 펴지지 않는다",
          verdict == "break" and bool(app.tree.item(head, "open")) == was_open,
          f"return={verdict!r} open={app.tree.item(head, 'open')}")
    check("그러면서 담기기는 한다", len(app.targets) == len(results), len(app.targets))
    before = len(app.targets)
    verdict = app._result_double_clicked(_Click(8, middle, app.tree))
    root.update()
    check("+/- 자리는 막지 않는다(접고 펴기가 살아 있다)",
          verdict is None and len(app.targets) == before, f"return={verdict!r}")

    # -- 감시 중에 텔레그램을 채워도 알림이 가는가 ----------------------------
    # 묶음 머리로 담으면 무엇이 담긴 것인지 말해 주는가
    app.targets = []
    app.sync_target_list()
    shown.clear()
    app.tree.selection_set(head)
    app.add_targets()
    root.update()
    check("묶음 머리로 담으면 1구간만 정해졌다고 알려 준다",
          any("1구간" in title for title, _m in shown), [t for t, _ in shown])
    app.targets = []
    app.sync_target_list()
    shown.clear()
    app.tree.selection_set(app.tree.get_children(head)[0])
    app.add_targets()
    root.update()
    check("자식 줄만 고르면 그 창이 뜨지 않는다",
          not any("1구간" in title for title, _m in shown), [t for t, _ in shown])

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

    # -- 잡은 예약: 구간별 홀드를 한 묶음으로 접는가 --------------------------
    app.holds = [
        Held(
            label="", summary="[1구간] 서울 → 대전", pnr="P1001", fare="10000",
            deadline=None, deadline_text="모름", kind="좌석 예약(1구간)",
            group="batch-1", full_summary="서울 → 동대구",
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
              app.hold_tree.item(item, "values")[3] == "P2001" for item in hold_top
          ))
    check("부모 줄은 선택해도 취소 대상이 되지 않는다(PNR 칸이 비어 있다)",
          app.hold_tree.item(group_parent, "values")[3] == "")
    app.hold_tree.selection_set(group_parent)
    check("부모를 고르고 취소를 찾으면 못 찾는다(안전하게 막힌다)",
          app._selected_hold_index() is None)
    app.holds = []
    app.sync_holds()

    # -- 로그인 팝업: 감시 중 잠금, 하이픈 안내, [비로그인] 목록 초기화 ------

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
    app.tree.selection_set(app.tree.get_children()[0])
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
    app.tree.selection_set(app.tree.get_children()[0])
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
