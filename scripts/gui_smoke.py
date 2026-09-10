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
    os.environ["HOME"] = tempfile.mkdtemp(prefix="korail-gui-smoke-")

    import tkinter as tk

    from korail_booker import ui as ui_module
    from korail_booker.autobook import Target
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

    root = tk.Tk()
    # 로그인 팝업은 이 확인의 대상이 아닙니다. 본 창을 막으면 아무것도 못 누릅니다.
    ui_module.BookerApp.open_login = lambda self: None  # type: ignore[method-assign]
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
    row = app.target_list.item(app.target_list.get_children()[0], "values")
    check("상태 칸이 구간별 예약임을 말한다", "구간별" in row[0], row[0])
    check("환승 대기 칸이 촉박한 환승을 경고한다", "촉박" in row[2], row[2])

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
    entries[0].insert(0, "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ")  # type: ignore[attr-defined]
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

    if args.shot:
        root.update()
        root.after(300, root.quit)
        root.mainloop()
        os.system(f"import -window root {args.shot}")
        print(f"화면을 {args.shot} 에 찍었습니다.")

    root.destroy()
    print(f"\n{sum(passed)}/{len(passed)} 통과")
    return 0 if all(passed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
