"""Tkinter 화면. 이 프로그램에서 Tkinter 를 import 하는 유일한 파일입니다.

규칙 둘로 굴러갑니다.

* **네트워크는 작업 스레드에서.** 조회도 로그인도 자동예매도 워커에서 돌고,
  결과는 큐에 담깁니다. Tkinter 위젯은 스레드에서 건드리면 안 되므로 큐를
  ``after`` 로 비우는 곳(:meth:`BookerApp._drain`)만 위젯을 만집니다.
* **실제 예약은 명시적으로.** "실제 예약" 체크가 꺼져 있으면 dry-run 이라
  예약 요청이 나가지 않습니다. 켜고 시작하면 확인 창이 한 번 더 뜹니다.
  결제는 어느 경우에도 하지 않습니다.
"""

from __future__ import annotations

import calendar
import math
import queue
import threading
import time
import tkinter as tk
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from tkinter import messagebox, ttk

from korail_mobile_api import (
    KorailApiError,
    KorailClient,
    KorailPassengerCounts,
    KorailSeatClass,
    KorailTransportError,
    MutationConsent,
    MutationPreview,
    ReservationHistoryResponse,
    ReservationHistoryTrain,
    ReservationHoldResponse,
    TrainScheduleResponse,
    TrainScheduleStop,
    TrainSummary,
)

from . import settings as settings_module
from .autobook import (
    DEFAULT_POLL_INTERVAL_S,
    MIN_POLL_INTERVAL_S,
    AutoBooker,
    BookingOptions,
    BookingResult,
    BookingSession,
    Outcome,
    PartialTransferError,
    Target,
    fare_text,
    payment_deadline_text,
    reserve_once,
)
from .holds import Held, is_expired, now_kst, parse_deadline, remaining_text
from .journeys import (
    TIGHT_TRANSFER_MINUTES,
    Journey,
    JourneyKey,
    JourneySource,
    SeatPreference,
    books_as_one_reservation,
    first_leg_key,
    format_clock,
    format_duration,
    group_by_first_leg,
    is_tight_transfer,
    normalize_clock,
    one_line,
    unbookable_detail,
    unbookable_reason,
)
from .logfmt import format_entry
from .notify import ResolvedChat, TelegramConfig, TelegramNotifier, looks_like_chat_id
from .search import (
    TRANSFER_CUSTOM,
    TRANSFER_SERVER,
    SearchRequest,
    filter_station_names,
    return_request,
    search_journeys,
    transfer_station_candidates,
)
from .session import build_client
from .session import login as do_login


#: 열차 종별. 거르는 방식이 **부분일치**라 ``"KTX"`` 하나로 ``KTX-산천`` 과
#: ``KTX-이음`` 까지 함께 잡힙니다. 여러 개를 고르면 그중 하나라도 맞으면
#: 통과입니다. 아무것도 고르지 않으면 전부 봅니다.
TRAIN_KINDS = (
    "KTX",
    "KTX-산천",
    "KTX-이음",
    "ITX-새마을",
    "ITX-마음",
    "무궁화",
    "새마을",
    "누리로",
)
SEAT_CHOICES = (("무관", SeatPreference.ANY), ("일반실", SeatPreference.GENERAL),
                ("특실", SeatPreference.SPECIAL))
#: 시각 선택지. 빈 값은 "제한 없음"입니다.
CLOCK_CHOICES = (
    "",
    *(f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in (0, 30)),
)
#: 환승시간 기본값. 위쪽을 열어 두면 몇 시간씩 기다리는 조합까지 다 딸려옵니다.
DEFAULT_MIN_TRANSFER_MINUTES = 0
DEFAULT_MAX_TRANSFER_MINUTES = 30
POLL_HINT = f"{MIN_POLL_INTERVAL_S:g}초 이상"
#: 표의 칸과 폭. 좌우로 나뉘면 좁아지므로 가로 스크롤이 함께 붙습니다.
TREE_COLUMNS = {
    "kind": ("구분", 110),
    "train": ("열차", 140),
    "departure_station": ("출발역", 80),
    "departure": ("출발", 65),
    "arrival_station": ("도착역", 80),
    "arrival": ("도착", 65),
    "duration": ("총 소요", 95),
    "transfer": ("환승 대기", 140),
    # 좌석 문구에는 "매진" 만 오는 것이 아니라 운임과 적립 안내까지 담겨
    # 옵니다. 좁으면 글자가 잘립니다.
    "general": ("일반실", 175),
    "special": ("특실", 175),
    "extras": ("입석·자유석·대기", 150),
}

WEEKDAY_NAMES = ("월", "화", "수", "목", "금", "토", "일")
#: 달력 바탕. 뒤쪽 창(대개 #f0f0f0)보다 **살짝** 어둡습니다. 많이
#: 어두우면 글씨가 안 읽히고, 같으면 어디까지가 달력인지 안 보입니다.
CALENDAR_BG = "#e2e2e2"
#: 달력 테두리. 바탕보다 확실히 진해야 경계가 섭니다.
CALENDAR_BORDER = "#7a7a7a"
#: 칸 하나가 최소 높이 말고도 먹는 몫 — 손잡이와 위아래 여백.
PANE_CHROME = 13
#: 열차 표 칸의 최소 높이 — 머리글·상태 줄에 네 줄 남짓.
RESULTS_MIN_HEIGHT = 185
#: 스스로 굴러가는 위젯. 이 위에서는 휠을 그쪽에 양보합니다.
SELF_SCROLLING = frozenset({"Text", "Treeview", "Listbox"})
#: 잡은 예약 표의 칸 — (이름, 폭, 정렬). **가운데 아홉 칸은 조회 결과·예매
#: 대상과 같습니다**(:meth:`BookerApp._journey_row_values`) — "여정" 한
#: 칸으로 뭉뚱그리면 구간별 홀드가 같은 줄처럼 보여 중복 예약으로
#: 오인하기 쉬웠습니다. 종류(좌석 예약/N구간)는 조회 결과에 없는, 이 표만의
#: 칸이라 따로 둡니다.
HOLD_LAYOUT = (
    ("구분", 100, "center"),
    ("종류", 90, "center"),
    ("열차", 120, "w"),
    ("출발역", 70, "center"),
    ("출발", 60, "center"),
    ("도착역", 70, "center"),
    ("도착", 60, "center"),
    ("총 소요", 85, "center"),
    ("환승 대기", 140, "center"),
    ("일반실", 150, "w"),
    ("특실", 150, "w"),
    ("입석·자유석·대기", 130, "w"),
    ("PNR", 130, "center"),
    ("운임", 90, "e"),
    ("결제 기한", 140, "center"),
    ("남은 시간", 110, "center"),
)
HOLD_COLUMNS = tuple(name for name, _width, _anchor in HOLD_LAYOUT)
#: 예매 대상 표의 칸. **가운데 아홉 칸은 조회 결과(TREE_COLUMNS)와 같습니다**
#: — 여정을 문장 하나로 뭉뚱그리면, 조회 결과에서는 구분해 보던 열차·시각·
#: 소요가 담는 순간 사라집니다. 앞뒤에 이 표에서만 뜻이 있는 칸(상태·조회
#: 주기·남은 감시)만 더합니다.
TARGET_LAYOUT = (
    # 구간별로 사는 것인지가 상태 칸에 붙습니다 — 맨 앞이라 표를 아무리
    # 좁혀도 잘리지 않습니다. 여정 칸 끝에 달았더니 실제로 안 보였습니다.
    ("상태", 130, "center"),
    ("구분", 100, "center"),
    ("열차", 120, "w"),
    ("출발역", 70, "center"),
    ("출발", 60, "center"),
    ("도착역", 70, "center"),
    ("도착", 60, "center"),
    ("총 소요", 85, "center"),
    ("환승 대기", 140, "center"),
    # 조회 결과에서 보던 것이 여기서 사라지면, 담고 나서 좌석이 어땠는지
    # 다시 위 표를 뒤져야 합니다.
    ("일반실", 165, "w"),
    ("특실", 165, "w"),
    ("입석·자유석·대기", 150, "w"),
    ("조회 주기", 80, "center"),
    ("남은 감시", 110, "center"),
)
TARGET_COLUMNS = tuple(name for name, _width, _anchor in TARGET_LAYOUT)
#: 촉박한 환승 앞에 붙는 표. 색만으로는 매진(빨강)과 구별되지 않습니다.
TIGHT_MARK = "\u26a0 "
#: "무관" -- 좌석 체크박스 둘 다 켜진 기본값. 이 값과 같으면
#: :meth:`BookerApp._seat_choices_for` 가 ``None`` 을 돌려줘 옛 방식(묶음
#: 공통 좌석 콤보박스)을 그대로 따릅니다.
_BOTH_SEAT_CLASSES = frozenset({KorailSeatClass.GENERAL, KorailSeatClass.SPECIAL})


def cancel_consent() -> MutationConsent:
    """취소 하나만 여는 consent. **이 파일에서만 엽니다.**

    ``autobook.py`` 는 자동으로 도는 감시라, 취소 consent 를 절대 만들지
    않는다고 그 파일 스스로 약속합니다(파일 맨 위 docstring). 자동으로
    무언가를 취소하면 사람이 모르는 사이에 표가 사라질 수 있기 때문입니다.
    그래서 취소는 사람이 [잡은 예약] 목록에서 줄을 고르고 확인 창까지
    지나야만 여기서 나갑니다 — 이 프로그램이 처음 여는 취소 consent 이므로
    다른 범주는 절대 함께 켜지 않는다는 것을 단언으로 남깁니다.
    """
    consent = MutationConsent(allow_cancel=True, dry_run=False)
    assert not consent.allow_reserve
    assert not consent.allow_payment
    assert not consent.allow_refund
    assert not consent.allow_cart
    return consent


def _transfer_text(station: str, journey: Journey, *, suffix: str = "") -> str:
    """환승 대기 칸의 글. 촉박하면 표를 답니다.

    ttk 의 표는 **칸 하나만 따로 물들일 수 없습니다** — 색은 줄 단위입니다.
    그래서 색은 줄에 걸고(``tight`` 태그), 어느 칸 때문인지는 이 표가
    말해 줍니다.
    """
    body = f"{station} {format_duration(journey.transfer_minutes)}{suffix}"
    if not is_tight_transfer(journey):
        return body
    return f"{TIGHT_MARK}{body} — 촉박"


#: 로그인 상태 글자색. 가장 자주 확인하는 것이라 색으로 먼저 말합니다.
LOGIN_OK_COLOUR = "#1a7f37"
LOGIN_BAD_COLOUR = "#b3261e"
LOGIN_OFF_COLOUR = "#666666"
#: 감시 묶음의 꼬리표. 기록에서 어느 묶음의 줄인지 이것으로 압니다.
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
#: 자동완성이 무시하는 키. 방향키와 기능키로는 목록을 다시 좁히지 않습니다.
_NAVIGATION_KEYS = frozenset(
    {
        "Up", "Down", "Left", "Right", "Return", "Escape", "Tab",
        "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    }
)


def parse_clock_field(text: str, *, label: str) -> str:
    """``"08:00"``/``"0800"``/``"080000"`` → ``HHMMSS``. 빈 값은 빈 값."""
    raw = text.strip().replace(":", "")
    if not raw:
        return ""
    if not raw.isdigit() or len(raw) not in (4, 6):
        raise ValueError(f"{label} 은 HH:MM 형식이어야 합니다")
    padded = raw if len(raw) == 6 else raw + "00"
    if int(padded[:2]) > 23 or int(padded[2:4]) > 59:
        raise ValueError(f"{label} 이 시각이 아닙니다")
    return padded


def parse_date_field(text: str) -> str:
    """``2026-08-10`` 이나 ``20260810`` → ``YYYYMMDD``."""
    raw = text.strip().replace("-", "").replace("/", "")
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다")
    try:
        # 모양만 보면 20261131 이나 20269999 가 그대로 서버로 나갑니다.
        # 달력에 있는 날인지까지 봅니다.
        date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    except ValueError:
        raise ValueError("달력에 없는 날짜입니다") from None
    # 코레일의 '오늘' 은 한국 시각입니다. 노트북 시계로 보면, 한국에서는 아직
    # 오늘인 날짜를 "지난 날짜" 라고 거절하는 시간대가 생깁니다.
    if raw < now_kst().strftime("%Y%m%d"):
        raise ValueError("지난 날짜는 조회할 수 없습니다")
    return raw


def parse_int_field(text: str, *, label: str, minimum: int = 0) -> int:
    raw = text.strip() or "0"
    if not raw.isdigit():
        raise ValueError(f"{label} 은 숫자여야 합니다")
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{label} 은 {minimum} 이상이어야 합니다")
    return value


#: 서버도 후보로 준 역에 붙는 꼬리표. 화면에만 붙고 조회에는 안 나갑니다.
VERIFIED_MARK = " (검증)"


def _plain_station(text: str) -> str:
    """화면 글에서 역 이름만. ``"대전 (검증)"`` → ``"대전"``."""
    name = text.strip()
    if name.endswith(VERIFIED_MARK.strip()):
        name = name[: -len(VERIFIED_MARK.strip())]
    return name.strip()


class AutocompleteCombobox(ttk.Combobox):
    """치는 대로 목록이 좁혀지는 콤보. 직접 입력도 그대로 됩니다.

    드롭다운을 **스스로 펼치지는 않습니다.** 한글 입력기가 글자를 조합하는
    도중에 목록을 펼치면 조합이 끊어집니다. 목록만 좁혀 두고, 펼치는 것은
    아래 화살표나 ``Down`` 키에 맡깁니다.
    """

    def __init__(self, master: tk.Misc, **kwargs: object) -> None:
        super().__init__(master, **kwargs)  # type: ignore[arg-type]
        self._completions: tuple[str, ...] = ()
        self.bind("<KeyRelease>", self._on_key_release)
        self.swallow_wheel(self)

    @staticmethod
    def swallow_wheel(widget: tk.Misc) -> None:
        """이 위젯 위에서는 휠이 **값을 바꾸지 못하게** 막습니다.

        ttk 의 Combobox 는 휠에 반응해 값을 바꿉니다. 창 전체가 굴러가는 이
        프로그램에서는 목록을 굴리려던 휠이 좌석 등급이나 출발역을 조용히
        바꾸고, 바뀐 값은 그대로 스크롤 밖으로 밀려나 보이지도 않습니다.
        """
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(sequence, lambda _event: "break")

    def set_completions(self, names: Sequence[str]) -> None:
        self._completions = tuple(names)
        self.configure(values=list(self._completions))

    def _on_key_release(self, event: tk.Event) -> None:
        if not self._completions or event.keysym in _NAVIGATION_KEYS:
            return
        if not self.get().strip():
            # 다 지웠으면 목록도 통째로 되돌립니다. 좁힌 결과(앞의 30개)를
            # 그대로 두면, 지운 뒤에는 전국 역이 아니라 그 30개만 남습니다.
            self.configure(values=list(self._completions))
            return
        matches = filter_station_names(self._completions, self.get())
        self.configure(values=matches or list(self._completions))


class CalendarPanel(tk.Frame):
    """같은 창 안에 겹쳐 뜨는 달 달력. 팝업이 아닙니다.

    tkcalendar 같은 것을 새로 들이지 않으려고 직접 그립니다 — 이 프로그램의
    의존성은 라이브러리와 같아야 합니다(``httpx``, ``cryptography``).

    ttk 가 아니라 tk 위젯으로 짭니다. 겹쳐 뜨는 것이라 **뒤쪽 창과 색이 같으면
    어디까지가 달력인지 보이지 않는데**, ttk 는 테마에 따라 배경색 지정을
    무시합니다. 고전 위젯은 어느 테마에서든 지정한 색 그대로 칠합니다.
    """

    def __init__(self, master: tk.Misc, on_pick: Callable[[date], None]):
        super().__init__(
            master,
            background=CALENDAR_BG,
            # 테두리를 두 겹으로 둡니다 — 바깥은 진한 선, 안쪽은 살짝 도드라지게.
            highlightbackground=CALENDAR_BORDER,
            highlightcolor=CALENDAR_BORDER,
            highlightthickness=2,
            relief="raised",
            borderwidth=1,
        )
        self._on_pick = on_pick
        self._shown = now_kst().date().replace(day=1)
        self._header = tk.StringVar()
        self._title = tk.StringVar(value="가는 날")
        #: 오는 날 달력을 열 때, 가는 날보다 앞선 날은 이 값 때문에 잠깁니다.
        #: ``None`` 이면 오늘보다 이전인 날만 잠급니다(가는 날 달력, 또는
        #: 편도).
        self._minimum: date | None = None
        top = tk.Frame(self, background=CALENDAR_BG)
        top.grid(row=0, column=0, padx=8, pady=(6, 2), sticky="ew")
        tk.Label(top, textvariable=self._title, foreground="#1f6feb",
                 background=CALENDAR_BG).pack(side="left")
        ttk.Button(top, text="◀", width=3, command=lambda: self._shift(-1)).pack(
            side="left", padx=(10, 0)
        )
        tk.Label(top, textvariable=self._header, width=12, anchor="center",
                 background=CALENDAR_BG).pack(side="left")
        ttk.Button(top, text="▶", width=3, command=lambda: self._shift(1)).pack(
            side="left"
        )
        ttk.Button(top, text="오늘", width=5, command=lambda: self._choose(self._today)).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(top, text="닫기", width=5, command=self.hide).pack(side="left", padx=4)
        self._grid = tk.Frame(self, background=CALENDAR_BG)
        self._grid.grid(row=1, column=0, padx=8, pady=(0, 8))
        self._draw()

    @property
    def _today(self) -> date:
        """**부를 때마다** 다시 봅니다. 기준은 한국 시각입니다.

        생성 시점에 잡아 두었더니, 밤새 켜 둔 창에서는 자정을 넘긴 뒤 [오늘]
        이 어제를 넣고 지난 날짜의 회색 처리도 하루씩 밀렸습니다. 어제를 넣은
        조회는 ``parse_date_field`` 가 거절합니다.
        """
        return now_kst().date()

    def open_for(
        self,
        title: str,
        current: date,
        *,
        over: tk.Misc,
        x: int,
        y: int,
        minimum: date | None = None,
    ) -> None:
        """조회 묶음 위에 겹쳐 띄웁니다.

        ``grid`` 로 한 줄을 차지하면 열릴 때마다 아래의 결과 표가 밀려 내려가고,
        창 밖으로 나가기까지 합니다. ``place`` 는 배치를 건드리지 않습니다 —
        팝업 창이 아니라 같은 창 안에 겹치는 것입니다.

        ``minimum`` 은 오는 날 달력에서 씁니다 — 가는 날보다 이전인 날은
        골라도 서버가 거절하는 왕복이 되므로, 애초에 못 고르게 잠급니다.
        """
        self._title.set(title)
        self._minimum = minimum
        self._shown = current.replace(day=1)
        self._draw()
        self.place(in_=over, x=x, y=y)
        self.lift()

    def hide(self) -> None:
        self.place_forget()

    def _shift(self, months: int) -> None:
        month = self._shown.month + months
        year = self._shown.year + (month - 1) // 12
        if not date.min.year <= year <= date.max.year:
            # 끝에서 멈춥니다. 예전에는 ValueError 가 단추 콜백에서 새어 나가
            # 그 화살표가 그 뒤로 영영 죽었습니다.
            return
        self._shown = date(year, (month - 1) % 12 + 1, 1)
        self._draw()

    def _choose(self, chosen: date) -> None:
        self._on_pick(chosen)
        self.hide()

    def _draw(self) -> None:
        for child in self._grid.winfo_children():
            child.destroy()
        self._header.set(f"{self._shown.year}년 {self._shown.month}월")
        for column, name in enumerate(WEEKDAY_NAMES):
            colour = "#b42318" if column == 6 else ("#1f6feb" if column == 5 else "#000")
            tk.Label(self._grid, text=name, width=4, anchor="center",
                     foreground=colour, background=CALENDAR_BG).grid(
                row=0, column=column, padx=1, pady=2
            )
        weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(
            self._shown.year, self._shown.month
        )
        for row, week in enumerate(weeks, start=1):
            for column, day in enumerate(week):
                if day == 0:
                    continue
                current = date(self._shown.year, self._shown.month, day)
                button = ttk.Button(
                    self._grid,
                    text=str(day),
                    width=4,
                    command=lambda picked=current: self._choose(picked),
                )
                # 지난 날짜는 조회할 수 없습니다 — 서버가 주지 않습니다.
                # 오는 날 달력이면, 가는 날보다 이전인 날도 마찬가지로
                # 잠급니다 — 그런 왕복은 서버가 받지 않습니다.
                if current < self._today or (
                    self._minimum is not None and current < self._minimum
                ):
                    button.state(["disabled"])
                button.grid(row=row, column=column, padx=1, pady=1)


@dataclass
class Watch:
    """돌고 있는 감시 하나 — 자동예매 묶음 하나.

    담긴 열차 전부를 한꺼번에 돌리던 것을 쪼갠 결과입니다. 어느 여정을 보고
    있는지 **여정 열쇠로** 기억합니다 — 목록의 자리 번호로 기억하면 사이에서
    하나를 빼는 순간 전부 어긋납니다.
    """

    #: 기록에 붙는 한 글자 꼬리표(``A``, ``B``…). 여럿이 돌 때 어느 줄이
    #: 어느 묶음 것인지 이것으로 압니다.
    tag: str
    session: BookingSession
    keys: frozenset[JourneyKey]
    #: 이 묶음이 노리는 방향들. 예매 대상 목록에서 그 줄을 빼도 남습니다 —
    #: 화면에 줄이 있는지와 "지금 그 방향을 노리는 중인지" 는 다른 이야기입니다.
    directions: frozenset[tuple[str, str, str]]
    #: 사람이 읽는 이름. 알림과 기록에 씁니다.
    title: str
    #: 시작할 때 읽은 조건. 도는 중에 화면을 고쳐도 이 묶음은 이것으로 돕니다.
    options: BookingOptions
    #: 감시가 끝나는 시각 — **엔진과 같은 자**입니다(``time.monotonic``).
    #: 벽시계로 세면 노트북을 덮었다 열거나 시계가 맞춰지는 순간 화면이
    #: "기한 지남" 이라고 말하는데 감시는 멀쩡히 돌고 있습니다.
    deadline: float | None

    @property
    def running(self) -> bool:
        return self.session.running

    def remaining(self, now: float) -> str:
        if not self.running:
            return "-"
        if self.deadline is None:
            return "무제한"
        left = int(self.deadline - now)
        if left <= 0:
            return "곧 끝납니다"
        hours, rest = divmod(left, 3600)
        minutes, seconds = divmod(rest, 60)
        if hours:
            return f"{hours}시간 {minutes}분 남음"
        if minutes:
            return f"{minutes}분 {seconds}초 남음"
        return f"{seconds}초 남음"


class BookerApp:
    """창 하나에 로그인·조회·자동예매가 다 들어간 화면."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = settings_module.load()
        self.client: KorailClient | None = None
        self.identity = ""
        self.logged_in = False
        self.journeys: list[Journey] = []
        #: (표, 항목) → 결과 번호. 표마다 항목 id 가 따로 매겨집니다.
        self.item_journeys: dict[tuple[str, str], int] = {}
        #: 지금 돌고 있는 감시들. 하나가 아니라 여럿입니다 — 담긴 것 중 고른
        #: 것만 따로 시작하고 따로 멈출 수 있어야 하기 때문입니다.
        self.watches: list[Watch] = []
        self._next_tag = 0
        self.events: queue.Queue[Callable[[], None]] = queue.Queue()
        self._credentials: tuple[str, str] | None = None
        #: 환승역 목록이 어느 구간 것인지. 같은 구간이면 다시 묻지 않습니다.
        self._transfer_route: tuple[str, str] | None = None
        #: 전국 역 이름. 자동완성과 환승역 추가가 이것을 씁니다.
        self.station_names: tuple[str, ...] = ()
        #: 묶음의 부모 줄 → 그 아래 여정들의 번호.
        self._group_children: dict[tuple[str, str], list[int]] = {}
        #: 여정이 아니라 구간 하나만 보여 주는 줄(1구간/2구간 정보 줄) →
        #: (그 여정의 번호, 구간 번호). 이 줄만 따로 고르면 그 구간 하나만의
        #: 예매 대상을 만듭니다(:meth:`_leg_only_target`).
        self._leg_items: dict[tuple[str, str], tuple[int, int]] = {}
        #: 이번 실행에만 쓰는 텔레그램 값. 설정 파일에는 쓰지 않습니다.
        #: 남의 컴퓨터에서 한 번만 쓰고 싶을 때를 위한 것입니다. 있으면
        #: 저장된 값보다 이쪽을 씁니다.
        self._telegram_once: TelegramConfig | None = None
        #: 조회마다 번호를 매깁니다. [조회 중지] 는 그 번호를 버림 표시에
        #: 넣고, 작업 스레드가 그것을 보고 조용히 끝냅니다.
        self._search_serial = 0
        self._search_token: int | None = None
        self._search_cancelled: set[int] = set()
        #: 조회 조건·환승 조건 칸이 지금 통째로 잠겨 있는지
        #: (:meth:`_lock_query_fields`). :meth:`sync_transfer_state` 와
        #: :meth:`sync_round_trip_state` 가 이것을 봅니다 — 조회 스레드가
        #: 그 구간의 환승역 후보를 스스로 받아 와 이 두 함수를 다시 부르는데
        #: (:meth:`_refresh_transfer_stations` → :meth:`_server_candidates_loaded`),
        #: 그 호출이 이 값을 안 보면 "환승이 켜져 있으니까" 라는 이유만으로
        #: 조회가 도는 중인데도 환승 조건 칸이 도로 풀립니다 — 실제로 그랬습니다.
        self._query_locked = False
        #: 서버가 이 구간 후보로 준 역들. 목록에 (검증) 을 붙이는 데 씁니다.
        self._server_stations: set[str] = set()
        #: 떠 있는 로그인 팝업. 없으면 ``None``.
        self._login_window: tk.Toplevel | None = None
        #: 잡아 둔 예약들. 결제 기한 카운트다운이 이것을 봅니다.
        self.holds: list[Held] = []
        self._hold_items: dict[int, str] = {}
        #: 잡은 예약 표에서 묶은 부모 줄의 항목 id → 그 아래 번호들.
        #: 예매 대상 표의 :attr:`_target_group_children` 과 같은 구실입니다.
        self._hold_group_children: dict[str, list[int]] = {}
        #: 예매 대상 표의 줄 번호 → 항목 id.
        self._target_items: dict[int, str] = {}
        #: 예매 대상 표에서 묶은 부모 줄의 항목 id → 그 아래 번호들.
        #:
        #: 조회 결과처럼 1구간이 같은 직접 조합을 접습니다 — :attr:`_group_children`
        #: 과 같은 구실이지만, 이 표는 트리가 하나뿐이라 ``(트리, 항목)`` 쌍이
        #: 필요 없습니다.
        self._target_group_children: dict[str, list[int]] = {}
        #: 붙인 칸들 — (담은 PanedWindow, 묶음, 지정된 최소 높이 또는 None).
        self._panes: list[tuple[tk.PanedWindow, ttk.Widget, int | None]] = []
        #: 각 칸의 최소 높이. 본문 높이를 여기서 더해 냅니다.
        self._pane_minimums: list[int] = []
        #: 달력이 지금 어느 칸을 고치는 중인지.
        self._calendar_for_return = False
        #: 마지막 조회가 실제로 쓴 오는 편 시간대. 설정에 남길 때 이것을
        #: 씁니다 — 저장하려고 화면 값을 다시 파싱하다 [조회] 가 죽었습니다.
        self._last_return_after = ""
        self._last_return_before = ""
        #: [바로 예약] 요청이 나가 있는 중인지. 닫기 전에 이것을 봅니다.
        self._reserving = False
        #: 답을 기다리는 [바로 예약]의 조합들. 그 몇 초 사이에 같은 조합에
        #: 감시를 걸거나 또 예약하는 것을 막습니다.
        self._reserving_keys: set[JourneyKey] = set()
        #: 클라이언트를 만드는 것은 한 번뿐이어야 합니다 — 스레드 둘이 동시에
        #: 만들면 로그인이 버려지는 쪽에 붙습니다.
        self._client_lock = threading.Lock()
        #: 조회 결과와, 자동예매에 담아 둔 것.
        self.results: list[Target] = []
        self.targets: list[Target] = []
        self._build()
        self._restore()
        self._watch_for_changes(
            self.departure,
            self.arrival,
            self.date,
            self.after_time,
            self.before_time,
            self.seat_choice,
            self.min_transfer,
            self.max_transfer,
            # 오는 편 칸도 조건입니다. 빼 두면 오는 날짜를 고쳐도 표가
            # 초록으로 "지금 조건의 결과" 라고 계속 말합니다.
            self.return_date,
            self.return_after_time,
            self.return_before_time,
            self.round_trip,
            *self.passenger_vars.values(),
        )
        # 가는 날짜가 오는 날짜를 넘어서면 오는 날짜를 따라 옮깁니다 —
        # :meth:`_watch_for_changes` 와는 별개의 trace 입니다(그쪽은
        # mark_stale 만 합니다).
        self.date.trace_add("write", lambda *_args: self._clamp_return_date())
        self.root.after(120, self._drain)
        # 결제 기한 카운트다운. 1초마다 목록의 '남은 시간' 칸만 다시 씁니다.
        self.root.after(1000, self._tick_holds)
        # 역 목록은 로그인 없이도 받을 수 있습니다. 켜자마자 받아 두면 자동완성이
        # 처음부터 돕니다 — 단추를 눌러야 채워지는 이유를 아무도 모릅니다.
        self.root.after(200, self.on_load_stations)

    # -- 화면 만들기 ---------------------------------------------------------

    def _build(self) -> None:
        """창 하나를 통째로 굴러가게 짓습니다.

        기능이 늘면서 어떤 화면에서도 다 보이지는 않게 됐습니다. 그래서 두
        가지를 함께 둡니다 — **창 전체가 세로로 스크롤**되고, 그 안의 여섯
        묶음은 **PanedWindow 로 서로 크기를 나눕니다.** 손잡이를 끌면 목록을
        키우고 조회 칸을 줄일 수 있습니다.
        """
        self.root.title("뉴레일 - 코레일의 새로운 예매 도우미")
        # 첫 크기는 다 지은 뒤에 정합니다(:meth:`_fit_to_screen`) — 안에 무엇이
        # 들어갈지 알아야 얼마가 필요한지 알 수 있고, 화면보다 커서도 안 됩니다.
        # 스크롤이 있으므로 최소 크기를 크게 잡을 이유가 없습니다. 작은
        # 노트북에서도 창이 화면 밖으로 나가지 않아야 합니다.
        self.root.minsize(900, 480)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        canvas = tk.Canvas(self.root, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.root, orient="vertical", command=canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        # 가로도 굴러갑니다. 없으면 창보다 넓은 묶음의 오른쪽이 **그냥
        # 잘립니다** — 환승 조건 칸의 [후보 갱신]·[비우기] 가 실제로 그렇게
        # 사라져 있었습니다. 세로만 굴러가는 창에서는 그 사실조차 보이지
        # 않습니다.
        hscroll = ttk.Scrollbar(self.root, orient="horizontal", command=canvas.xview)
        hscroll.grid(row=1, column=0, sticky="ew")
        canvas.configure(yscrollcommand=scroll.set, xscrollcommand=hscroll.set)
        self.canvas = canvas

        # ttk 가 아니라 tk 의 PanedWindow 입니다. ttk 쪽은 칸마다 **최소 높이를
        # 줄 수 없어서**, 처음 뜰 때 로그인·조회 묶음이 0 픽셀로 눌렸습니다.
        body = tk.PanedWindow(
            canvas,
            orient="vertical",
            sashwidth=7,
            sashrelief="raised",
            borderwidth=0,
            background="#d9d9d9",
        )
        window = canvas.create_window((0, 0), window=body, anchor="nw")

        def fit(width: int, visible: int) -> None:
            # 안쪽은 창보다 좁아지지 않고, 묶음들의 최소 높이 합보다 낮아지지도
            # 않습니다. 낮아지면 PanedWindow 가 묶음을 눌러 버리고, 그때는
            # 스크롤로 볼 것도 남지 않습니다.
            #
            # 가로도 같습니다 — 안쪽이 요구하는 폭보다 좁게 잡으면 오른쪽이
            # 잘려 나가고, 잘렸다는 것조차 보이지 않습니다. 넓게 잡고
            # 스크롤로 보게 합니다.
            body.update_idletasks()
            span = max(body.winfo_reqwidth(), width)
            height = max(self._body_height(), visible)
            canvas.itemconfigure(window, width=span, height=height)
            canvas.configure(scrollregion=(0, 0, span, height))

        canvas.bind("<Configure>", lambda event: fit(event.width, event.height))
        # 휠은 창 어디서 굴려도 듣습니다. 다만 스스로 굴러가는 위젯 위에서는
        # 그쪽에 양보합니다 — 표를 굴리려는데 창이 굴러가면 못 씁니다.
        canvas.bind_all("<MouseWheel>", self._on_wheel)
        canvas.bind_all("<Button-4>", self._on_wheel)
        canvas.bind_all("<Button-5>", self._on_wheel)
        # Shift 를 누르고 굴리면 가로입니다 — 창 어디서나 쓰는 몸짓입니다.
        canvas.bind_all("<Shift-MouseWheel>", lambda e: self._on_wheel(e, "x"))
        canvas.bind_all("<Shift-Button-4>", lambda e: self._on_wheel(e, "x"))
        canvas.bind_all("<Shift-Button-5>", lambda e: self._on_wheel(e, "x"))

        self._build_login(body)
        self._build_query(body)
        self._build_results(body)
        self._build_targets(body)
        self._build_holds(body)
        self._build_log(body)
        # 묶음을 다 붙인 뒤라야 최소 높이를 잴 수 있고, 그 합을 알아야 스크롤
        # 영역을 정할 수 있다 — 창이 그보다 작으면 굴려서 본다.
        self._settle_panes()
        self._fit_to_screen(body)
        # 창 크기가 바뀌면 감싸는 라벨이 줄 수를 다시 잡습니다 — 그러면 아까
        # 잰 최소 높이가 틀리고, 그 칸의 아래쪽이 잘립니다. 실제로 [조회]
        # 단추가 그렇게 잘렸습니다. 그래서 **새 크기로 배치가 끝난 뒤에**
        # 다시 잽니다. ``update_idletasks`` 만으로는 이르고, 창이 처음 그려질
        # 때까지 한 박자 더 걸리는 것이 있어 그 뒤에 한 번 더 잽니다.
        self.root.update()
        self._settle_panes()
        fit(canvas.winfo_width(), canvas.winfo_height())
        self.root.after(80, lambda: self._resettle(fit))
        # Enter 는 칸마다 답니다. 창 전체에 걸면 어느 칸에 있든 조회가
        # 돌았습니다 — 눈이 가 있는 칸이 무엇을 뜻하는지가 사람의 기대입니다.
        # 로그인 칸의 Enter 는 팝업 안에서 따로 답니다.
        for widget in self.query_fields:
            widget.bind("<Return>", lambda _event: self.on_search())
        # 켜자마자 로그인부터 묻습니다. 본 창은 그동안 눌리지 않습니다.
        self.root.after(300, self.open_login)

    def _resettle(self, fit: Callable[[int, int], None]) -> None:
        """다 그려진 뒤 최소 높이를 한 번 더 맞춥니다. 값이 같으면 아무 일도
        일어나지 않습니다."""
        self._settle_panes()
        fit(self.canvas.winfo_width(), self.canvas.winfo_height())

    def _fit_to_screen(self, body: tk.PanedWindow) -> None:
        """첫 창 크기를 **안에 든 것과 화면 둘 다** 보고 정합니다.

        고정값(``1240x1000``)으로 잡아 두었더니 두 가지가 한꺼번에 틀어졌습니다
        — 넓은 화면에서도 환승 조건 칸의 오른쪽이 잘려 나갔고, 칸들의 최소
        높이 합이 창보다 커서 열차 표가 켜자마자 한 줄로 눌렸습니다.

        그래서 필요한 만큼 잡되 화면의 92% 를 넘지 않습니다. 모자라면 스크롤이
        받습니다 — 화면 밖으로 나간 창은 스크롤로도 되돌릴 수 없습니다.
        """
        self.root.update_idletasks()
        wanted_w = body.winfo_reqwidth() + 30
        wanted_h = self._body_height() + 30
        width = max(900, min(wanted_w, int(self.root.winfo_screenwidth() * 0.92)))
        height = max(480, min(wanted_h, int(self.root.winfo_screenheight() * 0.92)))
        self.root.geometry(f"{width}x{height}")

    def _add_pane(
        self,
        parent: tk.PanedWindow,
        frame: ttk.Widget,
        *,
        stretch: str,
        minsize: int | None = None,
    ) -> None:
        """묶음 하나를 칸으로 붙입니다.

        ``minsize`` 를 주지 않으면 **그 묶음이 스스로 요구하는 높이를 재서**
        씁니다. 손으로 적어 두면 반드시 어긋납니다 — 실제로 예매 대상 묶음의
        최소 높이를 96 으로 적어 두는 바람에 [담기]·[빼기]·[비우기] 가 창이
        조금만 작아져도 잘려 나갔습니다. 위젯을 더 붙일수록 그 값은 더
        틀려집니다.

        줄여도 되는 묶음(표·기록처럼 줄이면 줄 수만 줄어드는 것)만 숫자를
        적습니다. ``stretch`` 는 창이 커질 때 남는 자리를 받을지입니다 —
        서식 묶음은 ``"never"`` 입니다. 늘려 봐야 빈칸만 늘어납니다.
        """
        # 지금 재면 1 이 나옵니다 — 이 함수는 묶음을 **만들자마자** 불리고,
        # 안의 위젯은 그 뒤에 붙기 때문입니다. 그래서 여기서는 자리만 잡고,
        # 실제 최소 높이는 :meth:`_settle_panes` 가 다 지은 뒤에 정합니다.
        parent.add(frame, minsize=minsize or 1, stretch=stretch, sticky="nsew",
                   padx=8, pady=3)
        self._panes.append((parent, frame, minsize))

    def _settle_panes(self) -> None:
        """다 지은 뒤에 각 칸의 최소 높이를 정합니다.

        ``minsize`` 를 주지 않은 칸은 **제가 요구하는 높이**를 씁니다. 그래야
        안에 있는 단추가 잘리지 않습니다.
        """
        self.root.update_idletasks()
        self._pane_minimums = []
        for parent, frame, given in self._panes:
            minsize = given if given is not None else frame.winfo_reqheight()
            parent.paneconfigure(frame, minsize=minsize)
            self._pane_minimums.append(minsize)

    def _body_height(self) -> int:
        """묶음들이 눌리지 않는 최소 본문 높이.

        상수로 적어 두면 위젯을 붙일 때마다 틀려집니다. 각 칸의 최소 높이를
        더하고, 손잡이와 여백 몫을 얹습니다.
        """
        return sum(self._pane_minimums) + PANE_CHROME * len(self._pane_minimums)

    def _on_wheel(self, event: tk.Event, axis: str = "y") -> None:
        widget = event.widget
        if not isinstance(widget, str) and widget.winfo_class() in SELF_SCROLLING:
            return
        # Windows/macOS 는 delta, X11 은 단추 4/5 로 옵니다.
        if getattr(event, "num", 0) == 4:
            step = -1
        elif getattr(event, "num", 0) == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        if axis == "x":
            self.canvas.xview_scroll(step, "units")
        else:
            self.canvas.yview_scroll(step, "units")

    def _build_login(self, parent: tk.PanedWindow) -> None:
        """로그인 **상태**만 보이는 줄. 입력은 팝업이 받습니다.

        아이디와 비밀번호 칸을 여기 두면, 프로그램을 켠 사람이 그 칸을 못 보고
        조회부터 눌렀다가 "예약이 왜 안 되지" 로 갑니다. 켜자마자 팝업이
        뜨면 로그인할지 조회만 할지를 먼저 정하게 됩니다.
        """
        frame = ttk.LabelFrame(parent, text="1. 로그인")
        self._add_pane(parent, frame, stretch="never")
        self.login_id = tk.StringVar()
        self.login_state = tk.StringVar(value="로그인하지 않았습니다 — 조회만 됩니다")
        self.login_label = ttk.Label(
            frame, textvariable=self.login_state, foreground=LOGIN_OFF_COLOUR
        )
        self.login_label.grid(row=0, column=0, padx=(8, 12), pady=8, sticky="w")
        self.login_button = ttk.Button(frame, text="로그인", command=self.open_login)
        self.login_button.grid(row=0, column=1, padx=2)
        self.logout_button = ttk.Button(
            frame, text="로그아웃", command=self.on_logout
        )
        self.logout_button.grid(row=0, column=2, padx=2)
        ttk.Label(
            frame,
            text="비밀번호는 저장하지 않습니다. 비회원 예매는 지원하지 않습니다.",
            foreground="#666666",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 6))
        self.sync_login_buttons()

    def sync_login_buttons(self) -> None:
        """단추를 지금 상태에 맞춥니다.

        로그인했으면 [다른 아이디로 로그인] 과 [로그아웃], 아니면 [로그인]
        하나뿐입니다 — 로그아웃할 것이 없는데 단추가 있으면 눌러 보게 됩니다.
        """
        if self.logged_in:
            self.login_button.configure(text="다른 아이디로 로그인")
            self.logout_button.grid()
        else:
            self.login_button.configure(text="로그인")
            self.logout_button.grid_remove()

    def open_login(self) -> None:
        """로그인 팝업. 떠 있는 동안 **본 창은 눌리지 않습니다.**

        ``grab_set`` 이 입력을 이 창으로 모읍니다. 뒤에서 조회를 눌러 놓고
        로그인 창을 찾는 일이 없게 하려는 것입니다. 창을 닫거나 [비로그인]
        을 누르면 비로그인 상태로 그냥 씁니다.

        **감시가 도는 중에는 열지 않습니다.** 여기서 로그인에 성공하면 목록을
        통째로 비우는데(:meth:`_reset_session_lists`), 감시가 쓰고 있는
        예매 대상·잡은 예약을 그 밑에서 지워 버리면 감시가 다음 순간 무엇을
        예약하는지 알 수 없게 됩니다. :meth:`on_logout` 과 같은 이유의 같은
        방비입니다.
        """
        if self.any_running():
            messagebox.showwarning(
                "로그인", "자동예매가 돌고 있습니다. [중지] 를 먼저 누르세요"
            )
            return
        existing = self._login_window
        if existing is not None and existing.winfo_exists():
            # 두 번째 팝업을 쌓으면 첫 번째의 grab 이 남아 어느 쪽도 못 씁니다.
            existing.lift()
            existing.focus_set()
            return
        window = tk.Toplevel(self.root)
        self._login_window = window
        window.title("코레일 로그인")
        window.transient(self.root)
        window.resizable(False, False)
        password = tk.StringVar()
        note = tk.StringVar(value="")

        ttk.Label(
            window,
            text="아이디·휴대폰번호·회원번호 중 아무거나 됩니다.\n"
            "휴대폰번호는 하이픈(-) 없이 숫자만 입력하세요.\n"
            "로그인하지 않아도 열차 조회는 됩니다 — 예약과 결제는 못 합니다.",
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 8))
        ttk.Label(window, text="아이디").grid(row=1, column=0, sticky="e", padx=(12, 4))
        id_entry = ttk.Entry(window, textvariable=self.login_id, width=24)
        id_entry.grid(row=1, column=1, sticky="w", padx=(0, 12), pady=2)
        ttk.Label(window, text="비밀번호").grid(row=2, column=0, sticky="e", padx=(12, 4))
        pw_entry = ttk.Entry(window, textvariable=password, show="*", width=24)
        pw_entry.grid(row=2, column=1, sticky="w", padx=(0, 12), pady=2)
        ttk.Label(window, textvariable=note, foreground=LOGIN_BAD_COLOUR).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=12, pady=(6, 0)
        )

        buttons = ttk.Frame(window)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", padx=12, pady=(12, 0))
        ttk.Label(
            window,
            text="[비로그인] 은 열차 조회만 됩니다 — 예약과 결제는 못 합니다.",
            foreground="#666666",
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=12, pady=(2, 12))

        def close() -> None:
            self._login_window = None
            password.set("")
            if window.winfo_exists():
                window.grab_release()
                window.destroy()
            self.sync_login_buttons()

        def skip() -> None:
            self._write_log(
                "비로그인으로 시작합니다 — 열차 조회만 됩니다 (예약·결제는 못 합니다)."
            )
            if not self.any_running():
                self._reset_session_lists()
            close()

        def attempt() -> None:
            # Enter 는 단추가 잠겨 있어도 듭니다. 두 번 보내면 뒤의 로그인이
            # 앞의 로그인이 막 세운 세션을 지웁니다.
            if str(login_button.cget("state")) == "disabled":
                return
            member_no = self.login_id.get().strip()
            secret = password.get()
            if not member_no or not secret:
                note.set("아이디와 비밀번호를 입력하세요")
                return
            login_button.configure(state="disabled")
            skip_button.configure(state="disabled")
            note.set("")
            self._set_login_state("로그인 중…", LOGIN_OFF_COLOUR)

            def done(message: str | None) -> None:
                if not window.winfo_exists():
                    # 로그인이 도는 동안 사람이 창을 닫았습니다(X 는 잠기지
                    # 않습니다). 없는 위젯을 만지면 TclError 가 나고 실패 사유가
                    # 통째로 사라집니다 — 기록에는 이미 남아 있습니다.
                    return
                if message is None:
                    close()
                    return
                # 실패하면 창을 닫지 않습니다 — 다시 치게 해야 합니다.
                note.set(message)
                login_button.configure(state="normal")
                skip_button.configure(state="normal")

            self._start_login(member_no, secret, done)

        login_button = ttk.Button(buttons, text="로그인", command=attempt)
        login_button.pack(side="left", padx=(0, 6))
        skip_button = ttk.Button(buttons, text="비로그인", command=skip)
        skip_button.pack(side="left")

        for field in (id_entry, pw_entry):
            field.bind("<Return>", lambda _event: attempt())
        window.protocol("WM_DELETE_WINDOW", skip)
        window.grab_set()
        (pw_entry if self.login_id.get().strip() else id_entry).focus_set()

    def _build_query(self, parent: tk.PanedWindow) -> None:
        frame = ttk.LabelFrame(parent, text="2. 열차 조회")
        # 최소 높이를 재서 씁니다. 줄일 수 있게 하면 맨 아래 [조회] 단추가
        # 잘려 나가고, 그러면 조회할 방법이 없어집니다. 좁은 화면에서는 줄이는
        # 대신 스크롤합니다.
        self._add_pane(parent, frame, stretch="never")
        self.departure = tk.StringVar()
        self.arrival = tk.StringVar()
        self.date = tk.StringVar(value=time.strftime("%Y-%m-%d"))
        self.return_date = tk.StringVar(value=time.strftime("%Y-%m-%d"))
        self.round_trip = tk.BooleanVar(value=False)
        self.after_time = tk.StringVar()
        self.before_time = tk.StringVar()
        self.return_after_time = tk.StringVar()
        self.return_before_time = tk.StringVar()
        self.train_kind_vars = {kind: tk.BooleanVar(value=False) for kind in TRAIN_KINDS}
        self.train_kind_label = tk.StringVar(value="전체")
        self.seat_choice = tk.StringVar(value="무관")
        # 예매 대상에 담을 때 이 여정에 받아들일 좌석 등급을 고릅니다.
        # 기본은 전부 체크(=무관) — 그러면 :meth:`_seat_choices_for` 가
        # ``None`` 을 돌려주어 옛 방식(묶음 공통 :attr:`seat_choice`)을
        # 그대로 따릅니다. 하나라도 체크를 떼야만 이 여정 전용 선택이
        # 됩니다 — 손대지 않은 사람은 지금까지와 똑같이 씁니다.
        self.pick_general = tk.BooleanVar(value=True)
        self.pick_special = tk.BooleanVar(value=True)
        # '이어서'(직접 조합 환승)는 구간마다 따로 사므로, 구간마다 독립적인
        # 체크박스를 둡니다 — 직통·서버 추천 환승은 위 둘로 충분합니다
        # (그쪽은 두 구간을 같은 등급으로만 살 수 있습니다).
        self.pick_leg1_general = tk.BooleanVar(value=True)
        self.pick_leg1_special = tk.BooleanVar(value=True)
        self.pick_leg2_general = tk.BooleanVar(value=True)
        self.pick_leg2_special = tk.BooleanVar(value=True)
        # [담기] 옆에 지금 위 여섯 값을 한 줄로 보여 줍니다 — 체크박스
        # 자체는 팝업(:meth:`open_seat_pick_dialog`) 뒤에 숨어 있으므로,
        # 이 글자가 없으면 지난번에 무엇을 바꿔 뒀는지 잊기 쉽습니다.
        self.seat_pick_summary = tk.StringVar(value="무관")
        for _var in (
            self.pick_general, self.pick_special,
            self.pick_leg1_general, self.pick_leg1_special,
            self.pick_leg2_general, self.pick_leg2_special,
        ):
            _var.trace_add("write", self._refresh_seat_pick_summary)
        self.include_direct = tk.BooleanVar(value=True)
        self.include_transfer = tk.BooleanVar(value=False)
        self.transfer_mode = tk.StringVar(value=TRANSFER_SERVER)
        self.transfer_role = tk.StringVar()
        self.min_transfer = tk.StringVar(value=str(DEFAULT_MIN_TRANSFER_MINUTES))
        self.max_transfer = tk.StringVar(value=str(DEFAULT_MAX_TRANSFER_MINUTES))
        self.passenger_vars = {
            "adult": tk.StringVar(value="1"),
            "teenager": tk.StringVar(value="0"),
            "child": tk.StringVar(value="0"),
            "infant": tk.StringVar(value="0"),
            "senior": tk.StringVar(value="0"),
        }

        # 줄마다 무엇을 정하는 줄인지 앞머리에 적습니다. 칸이 스무 개가 넘으면
        # 이름 없이 늘어놓은 줄은 읽히지 않습니다.
        # 왼쪽 줄은 모두 오른쪽 '환승 조건' 옆에 서야 하므로 좁아야 합니다.
        # 그래서 승객과 열차 종류를 따로 줄로 뗐습니다 — 한 줄에 몰면 1000px
        # 가까이 되어 옆자리가 남지 않고, 그러면 환승 조건이 아래로 내려가
        # 묶음이 통째로 길어집니다.
        route = self._section(frame, 0, "구간")
        ttk.Label(route, text="출발").pack(side="left")
        self.departure_box = AutocompleteCombobox(
            route, textvariable=self.departure, width=12
        )
        self.departure_box.pack(side="left", padx=(2, 6))
        ttk.Label(route, text="→ 도착").pack(side="left")
        self.arrival_box = AutocompleteCombobox(
            route, textvariable=self.arrival, width=12
        )
        self.arrival_box.pack(side="left", padx=(2, 14))
        #: 이 칸들에서 Enter 를 누르면 조회합니다. 로그인 칸과 갈라 둡니다.
        self.query_fields = (self.departure_box, self.arrival_box)
        self.station_state = tk.StringVar(value="역 불러오는 중…")

        people = self._section(frame, 1, "승객")
        for label, key in (
            ("어른", "adult"),
            ("청소년", "teenager"),
            ("어린이", "child"),
            ("유아", "infant"),
            ("경로", "senior"),
        ):
            ttk.Label(people, text=label).pack(side="left", padx=(0, 1))
            ttk.Entry(people, textvariable=self.passenger_vars[key], width=3).pack(
                side="left", padx=(0, 8)
            )

        # 가는 편과 오는 편은 같은 모양의 줄입니다 — 날짜와 시간대를 각각.
        outbound = self._section(frame, 2, "가는 편")
        self._leg_fields(outbound, self.date, self.after_time, self.before_time, False)
        ttk.Checkbutton(
            outbound,
            text="왕복",
            variable=self.round_trip,
            command=self._round_trip_toggled,
        ).pack(side="left", padx=(16, 0))

        inbound = self._section(frame, 3, "오는 편")
        self.return_widgets = self._leg_fields(
            inbound, self.return_date, self.return_after_time, self.return_before_time, True
        )

        # 여덟 종을 한 줄에 늘어놓으면 950px 입니다. 두 줄로 접어 좁힙니다.
        kinds = self._section(frame, 5, "열차 종류")
        half = (len(TRAIN_KINDS) + 1) // 2
        for index, kind in enumerate(TRAIN_KINDS):
            ttk.Checkbutton(
                kinds,
                text=kind,
                variable=self.train_kind_vars[kind],
                command=self.sync_train_kinds,
            ).grid(row=index // half, column=index % half, sticky="w", padx=(0, 6))
        # 고른 개수는 체크박스 옆에 붙습니다. 아랫줄로 내리면 그 줄이 넓어져
        # 오른쪽 환승 조건이 옆에 못 섭니다.
        ttk.Label(kinds, textvariable=self.train_kind_label, foreground="#1f6feb").grid(
            row=0, column=half, rowspan=2, sticky="w", padx=(10, 0)
        )
        tail = self._section(frame, 6, "")
        ttk.Button(tail, text="모두 선택", command=self.select_all_train_kinds).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(tail, text="모두 지우기", command=self.clear_train_kinds).pack(
            side="left", padx=(0, 6)
        )
        # 역 목록 상태와 [새로고침] 은 구간 줄에 있었습니다. 그 줄이 길어지면
        # 오른쪽 환승 조건이 옆에 못 서므로 여기로 내렸습니다 — 자주 누르는
        # 단추가 아닙니다(켜자마자 자동으로 불러옵니다).
        ttk.Label(tail, textvariable=self.station_state, foreground="#666666").pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(tail, text="역 새로고침", width=10, command=self.on_load_stations).pack(
            side="left"
        )

        seats = self._section(frame, 4, "좌석")
        seat_box = ttk.Combobox(
            seats,
            textvariable=self.seat_choice,
            values=[label for label, _ in SEAT_CHOICES],
            width=6,
            state="readonly",
        )
        # 휠이 값을 바꾸지 못하게 막습니다. 창 전체가 굴러가는 화면이라, 굴리려던
        # 휠이 좌석 등급을 조용히 바꾸고 바뀐 값은 스크롤 밖으로 밀려납니다.
        AutocompleteCombobox.swallow_wheel(seat_box)
        seat_box.pack(side="left", padx=(0, 14))
        ttk.Checkbutton(
            seats, text="직통", variable=self.include_direct, command=self.mark_stale
        ).pack(side="left")
        ttk.Checkbutton(
            seats,
            text="환승",
            variable=self.include_transfer,
            command=self._transfer_toggled,
        ).pack(side="left", padx=(6, 0))
        # 입석은 고를 수 있는 등급이 아닙니다. 왜인지는 화면이 말해야 합니다 —
        # 안 그러면 "왜 입석이 없지" 로 남습니다.
        ttk.Label(
            seats,
            text="입석은 따로 고를 수 없습니다 (아래 '입석·자유석·대기' 칸 참고)",
            foreground="#666666",
        ).pack(side="left", padx=(14, 0))

        # 환승 조건은 환승을 켰을 때만 만질 수 있습니다. 꺼져 있으면 아무 효과도
        # 없는 칸이라 켜 두면 헷갈리기만 합니다.
        self.transfer_frame = ttk.LabelFrame(
            frame, text="환승 조건 (직통 열차에는 영향을 주지 않습니다)"
        )
        # 왼쪽 다섯 줄(구간·가는 편·오는 편·열차 종류·좌석) 오른쪽의 빈자리에
        # 세웁니다. 아래에 두면 묶음이 200px 넘게 길어지고, 그만큼 열차 목록과
        # 예매 대상이 눌립니다. 옆에 두면 세로로는 그 다섯 줄과 겹칩니다.
        self.transfer_frame.grid(
            row=0, column=1, rowspan=7, sticky="nw", padx=(12, 4), pady=(2, 6)
        )
        self.query_frame = frame
        self.calendar = CalendarPanel(frame, self._calendar_picked)
        self._build_search_button(frame)
        left = ttk.Frame(self.transfer_frame)
        left.grid(row=0, column=0, sticky="nw", padx=4, pady=4)
        self.server_radio = ttk.Radiobutton(
            left,
            text="서버 추천 (검증됨)",
            variable=self.transfer_mode,
            value=TRANSFER_SERVER,
            command=self._transfer_toggled,
        )
        self.server_radio.pack(anchor="w")
        self.custom_radio = ttk.Radiobutton(
            left,
            text="직접 지정 (서버 미검증)",
            variable=self.transfer_mode,
            value=TRANSFER_CUSTOM,
            command=self._transfer_toggled,
        )
        self.custom_radio.pack(anchor="w")
        self.transfer_time_row = ttk.Frame(left)
        self.transfer_time_row.pack(anchor="w", pady=(6, 0))
        ttk.Label(self.transfer_time_row, text="환승시간").pack(side="left")
        self.min_transfer_entry = ttk.Entry(
            self.transfer_time_row, textvariable=self.min_transfer, width=4
        )
        self.min_transfer_entry.pack(side="left", padx=2)
        ttk.Label(self.transfer_time_row, text="분 이상").pack(side="left", padx=(1, 6))
        self.max_transfer_entry = ttk.Entry(
            self.transfer_time_row, textvariable=self.max_transfer, width=4
        )
        self.max_transfer_entry.pack(side="left", padx=2)
        ttk.Label(self.transfer_time_row, text="분 이하 (0=제한없음)").pack(side="left")

        right = ttk.Frame(self.transfer_frame)
        right.grid(row=0, column=1, sticky="nw", padx=(10, 4), pady=4)
        ttk.Label(right, text="환승역 (Ctrl+클릭으로 여러 개)").pack(anchor="w")
        # 이 목록이 무엇이고 지금 무슨 구실을 하는지는 모드마다 다릅니다.
        # 화면이 그것을 말하지 않으면 고른 역이 필터인지 조회 대상인지 알 수
        # 없습니다.
        # 감싸 주지 않으면 이 한 줄이 470px 을 먹고, 그만큼 조회 묶음이
        # 오른쪽으로 삐져나가 잘립니다(창에는 가로 스크롤이 없습니다).
        ttk.Label(
            right,
            textvariable=self.transfer_role,
            foreground="#1f6feb",
            wraplength=230,
            justify="left",
        ).pack(anchor="w")
        picker = ttk.Frame(right)
        picker.pack(anchor="w")
        self.transfer_list = tk.Listbox(
            picker, selectmode="extended", height=4, width=20, exportselection=False
        )
        self.transfer_list.bind("<<ListboxSelect>>", self.mark_stale)
        self.transfer_list.pack(side="left")
        list_scroll = ttk.Scrollbar(
            picker, orient="vertical", command=self.transfer_list.yview
        )
        self.transfer_list.configure(yscrollcommand=list_scroll.set)
        list_scroll.pack(side="left", fill="y")
        adder = ttk.Frame(right)
        adder.pack(anchor="w", pady=(4, 0))
        # 서버가 준 후보에 없는 역으로도 갈아탈 수 있습니다. 직접 지정 모드는
        # 어차피 두 구간을 따로 조회하는 것이라, 역 이름만 알면 됩니다.
        self.transfer_query = tk.StringVar()
        self.transfer_entry = AutocompleteCombobox(
            adder, textvariable=self.transfer_query, width=9
        )
        self.transfer_entry.pack(side="left")
        self.transfer_entry.bind("<Return>", lambda _event: self.add_transfer_station())
        self.transfer_add_button = ttk.Button(
            adder, text="추가", width=5, command=self.add_transfer_station
        )
        self.transfer_add_button.pack(side="left", padx=4)
        # 넣기만 되고 빼기가 없으면, 잘못 넣은 역을 지우려고 목록을 통째로
        # 다시 불러와야 합니다.
        self.transfer_remove_button = ttk.Button(
            adder, text="빼기", width=5, command=self.remove_transfer_station
        )
        self.transfer_remove_button.pack(side="left")
        # 단추 넷이 한 줄입니다. 아랫줄로 떼면 이 묶음이 100px 높아지고, 그만큼
        # 아래 표들이 눌립니다 — 실제로 그랬습니다. 이름을 줄여 한 줄에 넣습니다.
        self.transfer_load_button = ttk.Button(
            adder, text="후보 갱신", width=8,
            command=self.on_load_transfer_stations
        )
        self.transfer_load_button.pack(side="left", padx=(4, 0))
        # 갱신이 더는 지우지 않으므로, 처음부터 다시 하려면 지우는 단추가
        # 따로 있어야 합니다.
        self.transfer_clear_button = ttk.Button(
            adder, text="비우기", width=6,
            command=self.clear_transfer_stations
        )
        self.transfer_clear_button.pack(side="left", padx=(2, 0))
        ttk.Label(
            right,
            text="코레일이 이 구간에 답한 역입니다(qry.chtnStn.do) — 목록에 "
            "(검증) 이 붙습니다. [후보 갱신] 은 더하기만 하고 지우지 않습니다. "
            "지우려면 [빼기]·[비우기].",
            foreground="#666666",
            wraplength=230,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

    def _section(
        self,
        frame: ttk.LabelFrame,
        row: int,
        title: str,
        *,
        span: int = 1,
    ) -> ttk.Frame:
        """이름 붙은 한 줄. 이름은 왼쪽에 고정 폭으로 세워 눈이 따라가게 합니다.

        ``span`` 이 2 면 두 칸을 가로지릅니다 — 옆에 환승 조건을 세울 수 없을
        만큼 긴 줄(구간, 열차 종류)이 그렇습니다.
        """
        line = ttk.Frame(frame)
        line.grid(row=row, column=0, columnspan=span, sticky="w", padx=4, pady=3)
        ttk.Label(line, text=title, width=9, anchor="w", foreground="#1f6feb").pack(
            side="left"
        )
        holder = ttk.Frame(line)
        holder.pack(side="left")
        return holder

    def _leg_fields(
        self,
        parent: ttk.Frame,
        date_var: tk.StringVar,
        after_var: tk.StringVar,
        before_var: tk.StringVar,
        for_return: bool,
    ) -> tuple[ttk.Entry, ttk.Button, ttk.Combobox, ttk.Combobox]:
        """한 방향의 날짜와 시간대. 가는 편과 오는 편이 같은 모양입니다."""
        entry = ttk.Entry(parent, textvariable=date_var, width=12)
        entry.pack(side="left", padx=(0, 2))
        button = ttk.Button(
            parent,
            text="달력",
            width=5,
            command=lambda: self.open_calendar(for_return),
        )
        button.pack(side="left", padx=(0, 12))
        ttk.Label(parent, text="시간").pack(side="left")
        # 시각은 고르는 것입니다. 손으로 치면 형식을 틀리기 쉽고, 틀린 값은
        # 조회 전에 경고창으로만 돌아옵니다.
        after = ttk.Combobox(
            parent, textvariable=after_var, values=CLOCK_CHOICES, width=7,
            state="readonly",
        )
        AutocompleteCombobox.swallow_wheel(after)
        after.pack(side="left", padx=2)
        ttk.Label(parent, text="~").pack(side="left")
        before = ttk.Combobox(
            parent, textvariable=before_var, values=CLOCK_CHOICES, width=7,
            state="readonly",
        )
        AutocompleteCombobox.swallow_wheel(before)
        before.pack(side="left", padx=2)
        return (entry, button, after, before)

    def _build_search_button(self, frame: ttk.LabelFrame) -> None:
        """조회 단추는 조건 **아래**에 크게 둡니다.

        조건을 고치고 나서 누르는 것이라, 조건 줄 사이에 끼어 있으면 눈이
        찾지 못합니다. 환승역을 바꾼 뒤 다시 누르는 일이 잦습니다.
        """
        bar = ttk.Frame(frame)
        bar.grid(row=7, column=0, columnspan=2, sticky="ew", padx=4, pady=(2, 8))
        style = ttk.Style(self.root)
        style.configure("Search.TButton", font=("", 11, "bold"), padding=(24, 8))
        self.search_button = ttk.Button(
            bar, text="조회", style="Search.TButton", command=self.on_search
        )
        self.search_button.pack(side="left")
        # 조회는 하루치를 훑느라 몇 초 걸립니다. 아무 표시가 없으면 눌렸는지
        # 아닌지 알 수 없어 다시 누르게 되고, 그러면 요청이 두 배로 나갑니다.
        # 얼마나 남았는지는 알 수 없으므로 진행률이 아니라 움직이는 막대입니다.
        self.search_progress = ttk.Progressbar(bar, mode="indeterminate", length=140)
        self.search_progress.pack(side="left", padx=10)
        self.search_progress.pack_forget()
        # 하루치를 훑느라 요청이 여러 번 나갑니다. 잘못 누른 조회를 끝까지
        # 기다릴 이유가 없습니다.
        self.search_stop_button = ttk.Button(
            bar, text="조회 중지", width=10, command=self.on_stop_search
        )
        self.search_stop_button.pack(side="left")
        self.search_stop_button.pack_forget()
        ttk.Label(
            bar,
            text="조건을 바꾼 뒤에는 다시 눌러야 합니다 (Enter 로도 됩니다).",
            foreground="#666666",
        ).pack(side="left", padx=10)

    def open_calendar(self, for_return: bool) -> None:
        """같은 창 안에서 달력을 폅니다. 칸에 직접 쳐 넣어도 그대로 됩니다."""
        self._calendar_for_return = for_return
        variable = self.return_date if for_return else self.date
        try:
            current = date.fromisoformat(variable.get().strip())
        except ValueError:
            # 기준은 한국 시각입니다 — 달력의 '오늘' 과 같아야 합니다.
            current = now_kst().date()
        # 오는 날 달력은 가는 날보다 이전을 못 고르게 잠급니다 — 그런
        # 왕복은 서버가 거절합니다. 가는 날을 못 읽으면(칸이 비었거나
        # 모양이 틀렸으면) 잠글 기준이 없으니 그냥 둡니다.
        minimum: date | None = None
        if for_return:
            try:
                minimum = date.fromisoformat(self.date.get().strip())
            except ValueError:
                minimum = None
        self.calendar.open_for(
            "오는 날" if for_return else "가는 날",
            current,
            over=self.query_frame,
            # 날짜 칸 바로 아래입니다. 조회 조건 위에 겹칩니다.
            x=330 if not for_return else 560,
            y=36,
            minimum=minimum,
        )

    def _calendar_picked(self, picked: date) -> None:
        variable = self.return_date if self._calendar_for_return else self.date
        variable.set(picked.isoformat())

    def _clamp_return_date(self) -> None:
        """오는 날짜가 가는 날짜보다 앞서지 않게 합니다.

        왕복인데 가는 날짜를 뒤로 미루면, 오는 날짜가 그보다 이전인 채로
        남아 서버가 거절하는 조합이 됩니다(도착이 출발보다 이른 왕복).
        그래서 가는 날짜가 오는 날짜를 넘어서면 오는 날짜를 가는 날짜와
        같게 맞춥니다. 편도면 손대지 않습니다 — 오는 날짜가 뜻이 없고,
        칸도 잠겨 있습니다.

        어느 한쪽이라도 날짜 모양이 아니면(사람이 치는 중일 수 있습니다)
        건드리지 않습니다 — 잘못 읽고 지어낸 날짜를 넣는 것보다 그대로
        두는 편이 낫습니다.
        """
        if not self.round_trip.get():
            return
        try:
            departure = date.fromisoformat(self.date.get().strip())
            arrival = date.fromisoformat(self.return_date.get().strip())
        except ValueError:
            return
        if arrival < departure:
            self.return_date.set(departure.isoformat())

    def _round_trip_toggled(self) -> None:
        self.sync_round_trip_panes()
        self.sync_round_trip_state()
        self._clamp_return_date()
        self.mark_stale()

    def sync_round_trip_state(self) -> None:
        """오는 편 칸을 왕복 여부에 맞춥니다. 결과를 낡게 만들지 않습니다.

        :meth:`_round_trip_toggled` 에서 갈라낸 자리입니다 — 조회가 끝나고
        조건 칸 잠금을 풀 때도 이 상태를 다시 맞춰야 하는데, 그때
        ``mark_stale()`` 까지 함께 부르면 방금 받은 결과를 "조건이
        바뀌었다" 며 그 자리에서 낡게 만듭니다.

        :attr:`_query_locked` 도 봅니다 — :meth:`sync_transfer_state` 와 같은
        이유입니다. 지금은 이 함수를 조회 중에 다시 부르는 자리가 없지만,
        "잠겨 있으면 무조건 잠긴다" 는 것을 여기서도 지켜 두면 나중에 그런
        자리가 생겨도 같은 버그가 되살아나지 않습니다.
        """
        enabled = self.round_trip.get() and not self._query_locked
        entry, button, after, before = self.return_widgets
        entry.configure(state="normal" if enabled else "disabled")
        button.configure(state="normal" if enabled else "disabled")
        # 콤보는 켜도 "normal" 이 아니라 "readonly" 입니다 — 고르는 칸이지
        # 쳐 넣는 칸이 아닙니다.
        for combo in (after, before):
            combo.configure(state="readonly" if enabled else "disabled")

    @staticmethod
    def _configure_journey_columns(tree: ttk.Treeview, *, indicator_width: int = 28) -> None:
        """:data:`TREE_COLUMNS` 칸과 색 태그를 답니다.

        조회 결과 표와 '이어지는 구간 고르기' 팝업이 함께 씁니다 — 두 곳이
        따로 칸을 그리면 반드시 어긋납니다(실제로 팝업만 다른 모양이라는
        신고가 있었습니다).
        """
        tree.column("#0", width=indicator_width, stretch=False)
        for name, (title, width) in TREE_COLUMNS.items():
            tree.heading(name, text=title)
            tree.column(name, width=width, anchor="center", stretch=False)
        tree.tag_configure("custom", foreground="#a15c00")
        tree.tag_configure("leg", foreground="#555555")
        # 자동예매가 노리는 것은 매진입니다. 한눈에 갈리게 색을 답니다.
        tree.tag_configure("open", foreground="#1a7f37")
        tree.tag_configure("soldout", foreground="#b42318")
        tree.tag_configure("unbookable", foreground="#8a8a8a")
        # 환승 대기가 짧으면 빨갛게. 서버가 그런 조합을 거절하는 일이 있고
        # (ERR911193), 실제로 갈아타지 못할 수도 있습니다.
        tree.tag_configure("tight", foreground="#d1242f")
        # 묶음의 부모 줄은 여정이 아닙니다 — 1구간을 알려 주는 머리글입니다.
        tree.tag_configure("group", foreground="#1f6feb")

    def _make_tree(self, parent: ttk.Frame) -> ttk.Treeview:
        """열차 표 하나. 왕복이면 이것이 둘, 편도면 하나입니다."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            parent,
            columns=tuple(TREE_COLUMNS),
            show="tree headings",
            selectmode="extended",
            # 최소 높이입니다. 없으면 위쪽 조건이 커질 때 표가 0줄로 눌립니다.
            height=9,
        )
        self._configure_journey_columns(tree)
        # 두 번 누르면 담깁니다. 고르고 단추를 찾는 것보다 빠릅니다.
        tree.bind("<Double-Button-1>", self._result_double_clicked)
        # 오른쪽 눌러 "운행 일정" — Button-3 이 Windows·Linux, Button-2 가
        # macOS 트랙패드의 오른쪽 클릭입니다. 둘 다 걸어야 어느 쪽에서도 됩니다.
        tree.bind("<Button-3>", self._show_train_schedule_menu)
        tree.bind("<Button-2>", self._show_train_schedule_menu)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        # 좌우로 나뉘면 칸이 화면보다 넓어집니다. 가로 스크롤이 없으면 '그 밖'
        # 칸이 보이지 않습니다.
        horizontal = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        return tree

    def _build_results(self, parent: tk.PanedWindow) -> None:
        frame = ttk.LabelFrame(parent, text="3. 열차 (고른 것을 [담기] 로 예매 대상에 넣습니다)")
        self.results_frame = frame
        # 표는 줄여도 됩니다 — 보이는 줄 수만 줄어듭니다. 다만 **한 줄까지
        # 줄어들면 쓸모가 없습니다.** 110 으로 두었더니 켜자마자 딱 한 줄만
        # 보였습니다(칸들의 최소 높이 합이 창보다 커서 전부 최소로 눌립니다).
        # 이 창에서 가장 자주 보는 표이므로 네 줄은 남깁니다.
        self._add_pane(parent, frame, minsize=RESULTS_MIN_HEIGHT, stretch="always")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        # 단추 줄은 표와 **같은 칸을 나누지 않습니다.** 표 칸(TREE_COLUMNS)
        # 은 열한 개나 되어 자기 폭만으로도 웬만한 노트북 화면보다 넓은데,
        # 예전에는 이 단추들이 표와 같은 그리드 칸에 있어 그 폭을 그대로
        # 물려받았습니다 — 창을 최대화해도 화면 밖으로 밀려 안 보였습니다
        # (실제로 찍어서 확인했습니다). 따로 한 줄을 통째로 차지하고 왼쪽에
        # 붙이면, 표가 아무리 넓어도 이 줄은 늘 화면 맨 왼쪽에 보입니다.
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="w", padx=4, pady=(4, 2))
        ttk.Button(
            toolbar, text="↓ 예매 대상에 담기", command=self.add_targets
        ).pack(side="left")
        ttk.Button(
            toolbar, text="결과 비우기", width=10, command=self.clear_results
        ).pack(side="left", padx=(6, 0))
        # 체크박스 여섯 개를 늘 펴 두면 화면이 붐빕니다 — 대부분은 손대지
        # 않는(무관) 값입니다. 단추 하나로 감춰 두고, 지금 값은 옆 글자로만
        # 보여 줍니다("손대지 않았다" 를 한눈에 알 수 있어야 합니다).
        ttk.Button(
            toolbar, text="좌석 등급…", width=9, command=self.open_seat_pick_dialog
        ).pack(side="left", padx=(6, 0))
        ttk.Label(toolbar, textvariable=self.seat_pick_summary, foreground="#1f6feb").pack(
            side="left", padx=(4, 0)
        )

        self.outbound_title = ttk.Label(frame, text="가는 편", foreground="#1f6feb")
        self.outbound_title.grid(row=1, column=0, sticky="w", padx=6)
        self.outbound_title.grid_remove()
        self.inbound_title = ttk.Label(frame, text="오는 편", foreground="#1f6feb")
        self.inbound_title.grid(row=1, column=1, sticky="w", padx=6)
        self.inbound_title.grid_remove()

        outbound_pane = ttk.Frame(frame)
        outbound_pane.grid(row=2, column=0, sticky="nsew")
        self.tree = self._make_tree(outbound_pane)
        # 오는 편 표는 왕복일 때만 폅니다. 편도면 가는 편이 폭을 다 씁니다.
        self.inbound_pane = ttk.Frame(frame)
        self.inbound_pane.grid(row=2, column=1, sticky="nsew", padx=(6, 0))
        self.return_tree = self._make_tree(self.inbound_pane)
        self.inbound_pane.grid_remove()

        # 조건을 바꿔도 표는 그대로 남습니다. 그 표가 지금 조건의 결과인지
        # 아닌지를 말해 주지 않으면 "바꿨는데 아무 일도 안 일어난다" 가 됩니다.
        self.results_status = tk.StringVar(value="조건을 정하고 [조회] 를 누르세요.")
        self.results_label = ttk.Label(frame, textvariable=self.results_status)
        self.results_label.grid(row=3, column=0, columnspan=2, sticky="w", padx=4, pady=(2, 4))

    def open_seat_pick_dialog(self) -> None:
        """"담을 때" 좌석 등급 체크박스 — 단추를 눌러야만 뜨는 팝업.

        전부 체크된 기본값(무관)에서는 아무것도 바꾸지 않습니다 — 조회
        조건의 좌석 콤보박스(공통 설정)를 그대로 따릅니다. 하나라도 떼면
        그 뒤로 담기는 여정만 이 값으로 굳습니다(:meth:`_seat_choices_for`).
        '이어서'(직접 조합 환승)는 구간마다 따로 사므로 구간마다 따로
        고릅니다 — 직통·서버 추천 환승은 두 구간을 같은 등급으로만 살 수
        있어(라이브러리 제약) 한 벌이면 됩니다. 여기서 고른 값은 [담기]
        옆 글자로 계속 보입니다 — 닫아도 잊히지 않게.
        """
        window = tk.Toplevel(self.root)
        window.title("담을 좌석 등급")
        window.transient(self.root)
        ttk.Label(
            window,
            text="예매 대상에 담을 때 이 여정이 받아들일 좌석 등급을 고릅니다.\n"
            "전부 체크(무관)하면 감시를 시작할 때의 공통 설정(위 '좌석' 칸)을 "
            "그대로 따릅니다.",
            justify="left",
            wraplength=380,
        ).pack(anchor="w", padx=12, pady=(12, 8))

        direct = ttk.LabelFrame(window, text="직통 · 서버 추천 환승 (두 구간 같은 등급)")
        direct.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Checkbutton(direct, text="일반실", variable=self.pick_general).pack(
            side="left", padx=8, pady=6
        )
        ttk.Checkbutton(direct, text="특실", variable=self.pick_special).pack(
            side="left", padx=8, pady=6
        )

        custom = ttk.LabelFrame(window, text="이어서(직접 조합 환승) — 구간마다 따로")
        custom.pack(fill="x", padx=12, pady=(0, 8))
        leg1_row = ttk.Frame(custom)
        leg1_row.pack(anchor="w", padx=8, pady=(6, 2))
        ttk.Label(leg1_row, text="1구간:").pack(side="left")
        ttk.Checkbutton(leg1_row, text="일반실", variable=self.pick_leg1_general).pack(
            side="left", padx=(6, 0)
        )
        ttk.Checkbutton(leg1_row, text="특실", variable=self.pick_leg1_special).pack(
            side="left", padx=(2, 0)
        )
        leg2_row = ttk.Frame(custom)
        leg2_row.pack(anchor="w", padx=8, pady=(2, 6))
        ttk.Label(leg2_row, text="2구간:").pack(side="left")
        ttk.Checkbutton(leg2_row, text="일반실", variable=self.pick_leg2_general).pack(
            side="left", padx=(6, 0)
        )
        ttk.Checkbutton(leg2_row, text="특실", variable=self.pick_leg2_special).pack(
            side="left", padx=(2, 0)
        )

        ttk.Button(window, text="닫기", command=window.destroy).pack(pady=(0, 12))
        window.grab_set()

    def _seat_pick_summary_text(self) -> str:
        """지금 좌석 체크박스 상태를 [담기] 옆에 보일 한 줄로.

        팝업을 열지 않아도 지금 무엇이 골라져 있는지 알아야, 예전에 다른
        여정을 담으며 바꿔 둔 값을 잊고 엉뚱한 등급으로 담는 일이 없습니다.
        """

        def name(classes: frozenset[KorailSeatClass]) -> str:
            names = [
                label
                for seat_class, label in (
                    (KorailSeatClass.GENERAL, "일반실"),
                    (KorailSeatClass.SPECIAL, "특실"),
                )
                if seat_class in classes
            ]
            return "·".join(names) or "선택 없음"

        direct = self._checked_classes(self.pick_general, self.pick_special)
        leg1 = self._checked_classes(self.pick_leg1_general, self.pick_leg1_special)
        leg2 = self._checked_classes(self.pick_leg2_general, self.pick_leg2_special)
        both = _BOTH_SEAT_CLASSES
        if direct == both and leg1 == both and leg2 == both:
            return "무관"
        parts = []
        if direct != both:
            parts.append(f"직통·환승 {name(direct)}")
        if leg1 != both or leg2 != both:
            parts.append(f"이어서 1구간 {name(leg1)}·2구간 {name(leg2)}")
        return " / ".join(parts)

    def _refresh_seat_pick_summary(self, *_args: object) -> None:
        self.seat_pick_summary.set(self._seat_pick_summary_text())

    def sync_round_trip_panes(self) -> None:
        """왕복이면 표를 좌우로 나눕니다. 편도면 왼쪽 하나만 씁니다."""
        frame = self.results_frame
        if self.round_trip.get():
            self.outbound_title.grid()
            self.inbound_title.grid()
            self.inbound_pane.grid()
            frame.columnconfigure(1, weight=1)
        else:
            self.outbound_title.grid_remove()
            self.inbound_title.grid_remove()
            self.inbound_pane.grid_remove()
            frame.columnconfigure(1, weight=0)
            # 안 보이는 표의 선택은 지웁니다. 남겨 두면 [담기] 가 사람이
            # 볼 수 없는 오는 편 열차를 조용히 담습니다.
            self.return_tree.selection_remove(*self.return_tree.selection())

    def _build_targets(self, parent: tk.PanedWindow) -> None:
        """담아 둔 열차와, 그것을 노리는 조건을 **한 묶음**에 둡니다.

        예전에는 목록이 4번, 조건과 [시작] 이 5번으로 나뉘어 있었습니다. 그런데
        그 둘은 늘 함께 씁니다 — 담고, 주기를 정하고, 시작합니다. 사이에 묶음
        머리가 하나 더 있으면 그 몸짓이 두 번 끊기고, 세로 자리도 그만큼
        먹습니다. 합치면 표에 줄 자리가 그만큼 늘어납니다.
        """
        frame = ttk.LabelFrame(
            parent, text="4. 예매 대상과 자동예매 (여기 담긴 것만 노립니다)"
        )
        # 재서 씁니다. 96 으로 적어 뒀다가 [담기]·[빼기]·[비우기] 가 잘렸습니다.
        self._add_pane(parent, frame, stretch="always")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        # 단추는 표 옆(세로 줄)이 아니라 **표 위, 가로 한 줄**에 둡니다 —
        # 표 칸(TARGET_LAYOUT)이 열네 개라 표 옆에 세로로 세우면 그 폭을
        # 그대로 물려받아 창을 최대화해도 화면 밖으로 밀립니다(3번과 같은
        # 이유, 실제로 확인했습니다).
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="w", padx=4, pady=(4, 2))
        # 예약은 **담은 것** 중에서 합니다. 조회 결과에 두면 담기 전 줄까지
        # 잡을 수 있어 "담아 둔 것만 노린다" 는 규칙이 흐려집니다.
        self.reserve_now_button = ttk.Button(
            toolbar, text="바로 예약", command=self.on_reserve_now
        )
        self.reserve_now_button.pack(side="left")
        ttk.Button(toolbar, text="빼기", command=self.remove_targets).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(toolbar, text="비우기", command=self.clear_targets).pack(
            side="left", padx=(6, 0)
        )
        # 조건은 시작할 때 한 번 읽습니다. 도는 중에 위 칸을 고쳐도 그 묶음은
        # 옛 조건으로 계속 돕니다 — 이 단추가 멈추고 새 조건으로 다시 겁니다.
        ttk.Button(
            toolbar, text="조건 바꿔 재시작", command=self.restart_selected
        ).pack(side="left", padx=(14, 0))

        # 목록이 아니라 표입니다. 줄마다 상태·주기·남은 시간을 따로 적어야
        # 여럿을 돌릴 때 무엇이 언제까지 도는지 보입니다.
        self.target_list = ttk.Treeview(
            frame,
            columns=TARGET_COLUMNS,
            # "tree" 도 켭니다 — 1구간이 같은 후보를 묶으면(:meth:`sync_target_list`)
            # 그 부모 줄을 접고 펼 +/- 표시가 이 칸에서 나옵니다. 조회 결과와
            # 같은 자리(``Treeitem.indicator``)입니다.
            show="tree headings",
            selectmode="extended",
            height=4,
        )
        self.target_list.column("#0", width=20, stretch=False)
        for name, width, anchor in TARGET_LAYOUT:
            self.target_list.heading(name, text=name)
            self.target_list.column(
                name,
                width=width,
                anchor="w" if anchor == "w" else "center",
                stretch=(name == "여정"),
            )
        # 두 번 누르면 뺍니다. 담는 것과 빼는 것이 같은 몸짓의 앞뒤입니다.
        self.target_list.bind("<Double-Button-1>", self._target_double_clicked)
        self.target_list.bind("<Button-3>", self._show_train_schedule_menu)
        self.target_list.bind("<Button-2>", self._show_train_schedule_menu)
        self.target_list.grid(row=1, column=0, sticky="nsew", padx=(4, 0), pady=4)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.target_list.yview)
        self.target_list.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky="ns", pady=4)
        # 도는 것과 안 도는 것을 색으로 가릅니다. 줄 앞의 글자만으로는
        # 여러 줄이 섞였을 때 한눈에 안 들어옵니다.
        self.target_list.tag_configure("watching", foreground="#1a7f37")
        self.target_list.tag_configure("idle", foreground="#666666")
        self.target_list.tag_configure("tight", foreground="#d1242f")
        self.target_list.tag_configure("group", foreground="#1f6feb")
        self._build_booking_controls(frame)

    def clear_results(self) -> None:
        """열차 표를 비웁니다. **예매 대상과 감시는 건드리지 않습니다.**

        표에 지난 조회 결과가 남아 있으면 지금 조건의 것인지 헷갈립니다.
        담아 둔 것까지 사라지면 곤란하므로 표만 비웁니다.
        """
        for tree in (self.tree, self.return_tree):
            tree.delete(*tree.get_children())
        self.results = []
        self.journeys = []
        self.item_journeys.clear()
        self.results_status.set("결과를 비웠습니다. 조건을 정하고 [조회] 를 누르세요.")
        self.results_label.configure(foreground="")
        self._write_log("열차 목록을 비웠습니다 (예매 대상은 그대로입니다).")

    def on_reserve_now(self) -> None:
        """**예매 대상에 담긴 것 중** 고른 것을 지금 잡습니다.

        조회 결과에서 바로 잡을 수 있게 두었더니 "담아 둔 것만 노린다" 는
        규칙이 흐려졌습니다. 담는 것과 잡는 것을 같은 목록에서 하면, 무엇을
        잡았는지도 그 목록에 그대로 남습니다.

        여럿 고를 수 있습니다. 다만 **같은 방향을 둘 이상 고르면 막습니다** —
        같은 여정을 두 번 잡는 것은 중복 예약이고, 하나는 기한이 지나 버려질
        뿐입니다.
        """
        if not self.targets:
            messagebox.showwarning(
                "바로 예약", "먼저 위 표에서 고르고 [예매 대상에 담기] 를 누르세요"
            )
            return
        indices = self.selected_indices()
        if not indices:
            messagebox.showwarning("바로 예약", "예매 대상에서 잡을 열차를 고르세요")
            return
        if not self.logged_in:
            messagebox.showwarning("바로 예약", "먼저 로그인하세요")
            return
        picked = [self.targets[index] for index in indices]

        # 감시가 노리는 중이거나 이미 잡아 둔 **정확히 같은 조합**은
        # 건드리지 않습니다 — 그것만 다시 잡으면 진짜 중복 예약이고, 이
        # 프로그램은 취소를 하지 않습니다. 1구간이 같고 2구간만 다른
        # 서로 다른 조합은 막지 않습니다 — 그건 중복이 아닙니다.
        busy = self._busy_journeys()
        conflicting = [t for t in picked if t.journey.key() in busy]
        if conflicting:
            messagebox.showwarning(
                "바로 예약",
                f"아래 {len(conflicting)}편은 정확히 같은 조합을 이미 감시 중이거나 "
                "예약을 잡아 두었습니다. 지금 또 잡으면 중복 예약입니다.\n\n"
                + "\n".join(f"· {t.describe()}" for t in conflicting)
                + "\n\n먼저 그 감시를 멈추거나 잡은 예약을 정리하세요.",
            )
            return
        keys = [target.journey.key() for target in picked]
        repeated_targets = [
            target for target in picked if keys.count(target.journey.key()) > 1
        ]
        if repeated_targets:
            seen: set[JourneyKey] = set()
            lines = []
            for target in repeated_targets:
                if target.journey.key() in seen:
                    continue
                seen.add(target.journey.key())
                lines.append(f"· {target.describe()}")
            messagebox.showwarning(
                "바로 예약",
                "정확히 같은 조합을 둘 이상 골랐습니다. 같은 조합은 한 건만 "
                "잡습니다 — 둘을 잡으면 하나는 중복 예약이고 기한이 지나 "
                "버려집니다.\n\n" + "\n".join(lines),
            )
            return

        preference = dict(SEAT_CHOICES).get(self.seat_choice.get(), SeatPreference.ANY)
        plan: list[tuple[Target, tuple[KorailSeatClass, ...]]] = []
        blocked: list[str] = []
        for target in picked:
            journey = target.journey
            reason = unbookable_reason(journey)
            if reason is not None:
                blocked.append(
                    f"· {journey.summary()}\n   {unbookable_detail(journey) or reason}"
                )
                continue
            # 담을 때 이 여정만의 등급을 골라 두었으면(:meth:`_seat_choices_for`)
            # 그것을 따릅니다 — 없으면 옛 방식대로 지금 좌석 콤보박스를 씁니다.
            if target.seat_choices is not None:
                seat_classes = journey.bookable_seat_classes(target.seat_choices)
            else:
                seat_class = journey.bookable_seat_class(preference)
                seat_classes = None if seat_class is None else (seat_class,) * len(journey.legs)
            if seat_classes is None:
                blocked.append(
                    f"· {journey.summary()}\n   지금 이 등급으로 자리가 없습니다."
                )
                continue
            plan.append((target, seat_classes))

        if not plan:
            messagebox.showinfo(
                "바로 예약",
                "지금 잡을 수 있는 것이 없습니다.\n\n"
                + "\n".join(blocked)
                + "\n\n만석을 노리려면 이 줄을 고른 채 [고른 것만 시작] 으로 "
                "자동예매를 거세요 — 자리가 열리는 순간 잡습니다.",
            )
            return

        lines = "\n".join(f"· {target.describe()}" for target, _seat in plan)
        skipped = ("\n\n지금 못 잡는 것(건너뜁니다):\n" + "\n".join(blocked)) if blocked else ""
        if not messagebox.askyesno(
            "바로 예약",
            f"아래 {len(plan)}편을 지금 잡습니다 (결제 전 홀드).\n\n"
            f"{lines}{skipped}\n"
            f"{self._split_warning([target for target, _seat in plan])}\n"
            "결제는 하지 않습니다 — 잡은 뒤 기한 안에 코레일 앱에서 결제하거나 "
            "취소해야 합니다.\n\n계속할까요?",
        ):
            return

        self.reserve_now_button.configure(state="disabled")
        self._reserving = True
        self._reserving_keys = {target.journey.key() for target, _seat in plan}
        for target, _seat in plan:
            self._write_log(f"바로 예약 시도 — {target.describe()}")

        def work() -> None:
            client = self._ensure_client()
            for target, seat_class in plan:
                try:
                    result = reserve_once(
                        client,
                        target.journey,
                        passengers=target.request.passengers,
                        seat_class=seat_class,
                    )
                except PartialTransferError as exc:
                    # 앞 구간은 이미 잡혔습니다. 그 사실을 잃으면 사람이
                    # 아무것도 안 됐다고 믿은 채 기한을 넘깁니다.
                    self.events.put(
                        lambda t=target, e=exc: self._reserve_now_partial(t, e)
                    )
                    continue
                except KorailTransportError as exc:
                    # 요청이 나간 뒤 끊긴 것인지 나가기 전인지 알 수 없습니다.
                    # "실패" 라고만 적으면 사람이 다시 눌러 중복 예약을
                    # 만듭니다 — 엔진 쪽(_settle_broken)과 같은 규칙입니다.
                    self.events.put(
                        lambda t=target, e=exc: self._reserve_now_broken(t, e)
                    )
                    continue
                except KorailApiError as exc:
                    # 하나가 실패해도 나머지는 계속합니다. 여럿을 골랐다면
                    # 그중 되는 것은 잡히는 편이 낫습니다.
                    message = f"{target.describe()}: {type(exc).__name__}: {exc}"
                    self.events.put(
                        lambda m=message: self._write_log(
                            f"바로 예약 실패 — {m}", "bad"
                        )
                    )
                    continue
                self.events.put(
                    lambda t=target, r=result: self._reserve_now_done(t, r)
                )
            self.events.put(self._reserve_now_finished)

        self._in_thread(work, "바로 예약")

    def _reserve_now_finished(self) -> None:
        self._reserving = False
        self._reserving_keys = set()
        self.reserve_now_button.configure(state="normal")

    def _split_warning(self, targets: Sequence[Target]) -> str:
        """구간마다 따로 사는 것이 섞여 있으면 그렇다고 말합니다.

        예약이 하나가 아니라 둘이 되고, 한쪽만 잡힐 수도 있습니다. 그것을
        모르고 누르면 안 됩니다.
        """
        split = [t for t in targets if not books_as_one_reservation(t.journey)]
        if not split:
            return ""
        names = "\n".join(f"· {t.describe()}" for t in split)
        return (
            f"\n[구간별 예약] 아래 {len(split)}편은 코레일이 검증한 환승 조합이 "
            "아니라, 한 건이 아니라 구간마다 따로 삽니다.\n"
            f"{names}\n"
            "· 예약도 결제도 구간 수만큼 따로 생깁니다.\n"
            "· 앞 구간만 잡히고 뒤 구간을 놓칠 수 있습니다. 그러면 잡힌 것을 "
            "코레일 앱에서 취소하거나 결제해야 합니다 — 이 프로그램은 취소를 "
            "하지 않습니다.\n"
        )

    def _reserve_now_done(
        self,
        target: Target,
        results: Sequence[MutationPreview | ReservationHoldResponse],
    ) -> None:
        """잡힌 것을 목록에 넣고 알립니다. **여럿일 수 있습니다.**

        직접 조합 환승은 구간마다 따로 사므로 예약이 구간 수만큼 나옵니다.
        """
        holds = [item for item in results if isinstance(item, ReservationHoldResponse)]
        if not holds:
            self._write_log("바로 예약: 미리보기라 아무것도 보내지 않았습니다.", "warn")
            return
        split = len(holds) > 1
        # 구간마다 홀드가 따로 나와도 이번 한 번의 [바로 예약] 시도에서
        # 나온 것임을 표시해야 화면이 묶어 보여 줄 수 있습니다.
        batch = uuid.uuid4().hex[:12]
        lines = []
        for number, result in enumerate(holds, start=1):
            kind = f"좌석 예약({number}구간)" if split else "좌석 예약"
            # 구간마다 따로 샀으면 '여정' 칸도 구간별로 다르게 적습니다.
            # 전체 여정 요약을 그대로 두 번 쓰면 PNR·운임이 다른데 칸만
            # 똑같아 보여 중복 예약으로 오인하게 됩니다.
            summary = (
                target.journey.leg_hold_label(number - 1)
                if split
                else target.journey.summary()
            )
            hold_journey = (
                Journey(
                    legs=(target.journey.legs[number - 1],), source=target.journey.source
                )
                if split
                else target.journey
            )
            held = self._held_from(
                target.label,
                summary,
                kind,
                result,
                target.direction,
                batch,
                target.journey.summary(),
                hold_journey,
            )
            self.remember_hold(held)
            self._write_log(
                f"예약했습니다 — {kind} {held.summary}\nPNR {held.pnr}\n"
                f"운임 {held.fare}\n결제 기한 {held.deadline_text}",
                "good",
            )
            lines.append(
                f"{kind}\nPNR {held.pnr}\n운임 {held.fare}\n"
                f"결제 기한 {held.deadline_text}"
            )
        note = (
            "\n\n구간마다 따로 산 예약입니다 — 결제도 따로 하셔야 합니다.\n"
            if split
            else "\n\n"
        )
        messagebox.showinfo(
            "예약했습니다 (아직 결제 전)",
            f"{target.journey.summary()}\n\n"
            + "\n\n".join(lines)
            + note
            + "5번 '잡은 예약' 에 남은 시간이 셉니다. 기한 안에 코레일 앱에서 "
            "결제하세요.",
        )
        # 잡혔으면 예매 대상에서 뺍니다 — 안 빼면 4번과 5번에 같은 열차가
        # 나란히 남아 두 번 잡은 것처럼 보입니다. 실제로는 한 번뿐입니다.
        if target in self.targets:
            self.targets.remove(target)
            self.sync_target_list()

    def _reserve_now_broken(self, target: Target, exc: Exception) -> None:
        """예약 요청이 전송 중에 끊겼습니다. **결과를 알 수 없습니다.**

        서버가 받아 예약을 만들었는지 판단할 근거가 없습니다. "실패했습니다"
        라고만 적으면 사람이 한 번 더 누르고, 이미 생겼다면 그것이 중복
        예약입니다 — 이 프로그램은 취소를 하지 않습니다.
        """
        self._write_log(
            f"바로 예약: 요청이 전송 중에 끊겼습니다 — {target.describe()}\n"
            f"{type(exc).__name__}: {exc}",
            "bad",
        )
        messagebox.showwarning(
            "예약 요청이 끊겼습니다",
            f"{target.journey.summary()}\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "서버에 예약이 생겼는지 여기서는 알 수 없습니다.\n"
            "**다시 누르기 전에** 코레일 앱에서 예약 내역을 확인하세요 — "
            "이미 생겼다면 한 번 더 누르는 것이 중복 예약입니다.",
        )

    def _reserve_now_partial(self, target: Target, exc: PartialTransferError) -> None:
        """앞 구간만 잡히고 뒤 구간에서 막혔습니다. 크게 알립니다."""
        holds = [h for h in exc.held if isinstance(h, ReservationHoldResponse)]
        # 한 번의 [바로 예약] 시도입니다 — 같은 batch 로 묶습니다.
        batch = uuid.uuid4().hex[:12]
        for number, hold in enumerate(holds, start=1):
            held = self._held_from(
                target.label,
                # **여정 전체가 아니라 그 구간**입니다. summary() 를 그대로
                # 쓰면 앞 구간만 잡혔는데도 전체를 산 것처럼 적힙니다.
                target.journey.leg_hold_label(number - 1, partial=True),
                f"좌석 예약({number}구간)",
                hold,
                target.direction,
                batch,
                target.journey.summary(),
                Journey(
                    legs=(target.journey.legs[number - 1],), source=target.journey.source
                ),
            )
            self.remember_hold(held)
        pnrs = ", ".join(h.pnr_no or "?" for h in holds) or "(없음)"
        self._write_log(
            f"환승 일부만 잡혔습니다 — {target.describe()}\n"
            f"잡힌 PNR {pnrs}\n{exc.leg_number}구간 실패: {exc.reason}",
            "bad",
        )
        messagebox.showwarning(
            "환승 일부만 잡혔습니다",
            f"{target.journey.summary()}\n\n"
            f"잡힌 구간의 PNR: {pnrs}\n"
            f"{exc.leg_number}구간 실패: {exc.reason}\n\n"
            "코레일이 검증한 환승 조합이 아니라 구간마다 따로 샀기 때문에 "
            "한쪽만 남았습니다. 코레일 앱에서 잡힌 구간을 결제하거나 "
            "취소하세요 — 이 프로그램은 취소를 하지 않습니다.",
        )
        # 다시 시도하지 않습니다 — 예매 대상에도 남겨 두지 않습니다. 남겨
        # 두면 한 번 더 누르고 싶어지는데, 그러면 이미 잡힌 앞 구간을 또
        # 잡는 중복 예약이 됩니다.
        if target in self.targets:
            self.targets.remove(target)
            self.sync_target_list()


    def _searching(self, busy: bool) -> None:
        """조회 중임을 막대로 보입니다. 끝나면 자리까지 거둡니다.

        **조건 칸도 함께 잠급니다.** 조회는 시작할 때 조건을 한 번 읽어
        나갑니다 — 도는 중에 구간이나 환승 조건을 바꾸면 화면은 새 조건을
        보여 주는데 실제로 나간 요청은 옛 조건입니다. 결과가 도착했을 때
        그것이 어느 조건의 것인지 사람이 알 방법이 없어집니다.
        """
        if busy:
            self.search_progress.pack(side="left", padx=10)
            self.search_progress.start(12)
            self.search_stop_button.pack(side="left")
        else:
            self.search_progress.stop()
            self.search_progress.pack_forget()
            self.search_stop_button.pack_forget()
        self._lock_query_fields(busy)

    def _lock_query_fields(self, locked: bool) -> None:
        """조회 조건·환승 조건 칸을 통째로 잠급니다/풉니다.

        [조회] 와 [조회 중지] 는 뺍니다 — 잠그는 동안에도 중지는 눌러야
        하고, [조회] 자체는 ``search_button.configure`` 가 따로 관리합니다.
        """
        # 다른 함수(:meth:`sync_transfer_state`, :meth:`sync_round_trip_state`)
        # 가 이 값을 보고서야 옳게 잠그므로, 위젯을 만지기 **전에** 먼저
        # 바꿔 둡니다 — 조회 스레드가 이 사이에 끼어들어(환승역 후보를
        # 받아 와 그 두 함수를 다시 부릅니다) 옛 값을 보면 도로 풀립니다.
        self._query_locked = locked
        exempt = {
            self.search_button,
            self.search_stop_button,
            self.search_progress,
            # 달력은 통째로 뺍니다 — 지난 날짜 단추를 저 스스로 잠가 두는데
            # (``CalendarPanel._draw``), 여기서 한 번 더 풀면 지난 날짜를
            # 다시 고를 수 있게 됩니다.
            self.calendar,
        }
        self._set_widget_locked(self.query_frame, locked=locked, exempt=exempt)
        if not locked:
            # 통째로 풀면 왕복이 꺼져 있어도 오는 편 칸이, 환승이 꺼져
            # 있거나 서버 추천이어도 직접 지정 전용 칸이 도로 눌리게 됩니다
            # — 그 둘은 잠그기 전에도 조건에 따라 잠겨 있었습니다.
            self.sync_round_trip_state()
            self.sync_transfer_state()

    def _set_widget_locked(
        self, widget: tk.Misc, *, locked: bool, exempt: set[tk.Misc]
    ) -> None:
        """이 위젯 아래를 재귀적으로 잠급니다/풉니다.

        ttk 위젯과 고전 tk 위젯(``tk.Listbox`` 등)이 상태를 다루는 방법이
        다릅니다 — ttk 는 ``state()``, 고전 tk 는 ``configure(state=...)``.
        둘 다 시도하고, 그 위젯이 상태 자체를 모르면(``ttk.Label`` 등)
        조용히 넘어갑니다 — 잠글 것이 없을 뿐 오류가 아닙니다.

        ``exempt`` 에 든 위젯은 **그 아래까지 통째로** 건드리지 않습니다 —
        달력처럼 스스로 상태를 관리하는 것을 한 번 더 풀면, 지난 날짜처럼
        원래 잠가 둬야 하는 것까지 함께 풀립니다.
        """
        for child in widget.winfo_children():
            if child in exempt:
                continue
            if isinstance(child, ttk.Widget):
                try:
                    child.state(["disabled" if locked else "!disabled"])
                except tk.TclError:
                    pass
            else:
                # 고전 tk 위젯(``tk.Listbox`` 등)은 파이썬 쪽 타입 정의가
                # ``state`` 를 모릅니다 — Tcl 수준에서는 있는 옵션이라
                # 실행에는 문제가 없습니다.
                try:
                    child.configure(state="disabled" if locked else "normal")  # type: ignore[call-overload]
                except tk.TclError:
                    pass
            self._set_widget_locked(child, locked=locked, exempt=exempt)

    def on_stop_search(self) -> None:
        """조회를 그만둡니다.

        나가 있는 요청을 도중에 끊지는 않습니다 — 그 답은 어차피 옵니다.
        대신 **이번 조회를 버린다는 표시**를 세워, 다음 페이지를 묻지 않고
        받아 온 것도 화면에 올리지 않습니다.
        """
        if self._search_token is None:
            return
        self._search_cancelled.add(self._search_token)
        # **표시를 내려야 합니다.** on_search 는 이 값이 남아 있으면 "이미 조회
        # 중" 으로 보고 돌려보냅니다 — 중지 한 번에 [조회] 가 그 뒤로 영영
        # 죽었습니다. 버림 표시는 위에 남아 있으므로, 나가 있는 조회가 늦게
        # 끝나도 그 결과가 화면에 오르지는 않습니다.
        self._search_token = None
        self._write_log("조회를 중지했습니다.", "warn")
        self.search_button.configure(state="normal")
        self._searching(False)
        self.results_status.set("조회를 중지했습니다. 다시 [조회] 를 누르세요.")
        self.results_label.configure(foreground="#a15c00")

    def _build_holds(self, parent: tk.PanedWindow) -> None:
        """잡아 둔 예약과 결제 기한. 남은 시간이 1초마다 줄어듭니다.

        잡고 끝이 아니라 **기한 안에 결제해야** 표가 남습니다. 기한을 기록
        한 줄로만 알리면 그 줄은 곧 위로 밀려 올라가고, 그러면 아무도 보지
        않습니다.
        """
        frame = ttk.LabelFrame(parent, text="5. 잡은 예약 (기한 안에 코레일 앱에서 결제하세요)")
        self._add_pane(parent, frame, stretch="always")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        # 단추는 표 옆이 아니라 표 위, 가로 한 줄에 둡니다 — 3·4번과 같은
        # 이유(표 칸이 열여섯 개나 되어 옆에 세우면 화면 밖으로 밀립니다).
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="w", padx=4, pady=(4, 2))
        # 이 프로그램이 취소 요청을 만드는 **유일한** 자리입니다. 자동예매는
        # 절대 취소를 부르지 않습니다 — 사람이 줄을 고르고 확인 창을 지나야만
        # 나갑니다.
        self.cancel_hold_button = ttk.Button(
            toolbar, text="선택 취소", command=self.on_cancel_hold
        )
        self.cancel_hold_button.pack(side="left")
        ttk.Button(
            toolbar, text="만료된 것 지우기", command=self.clear_expired_holds
        ).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="비우기", command=self.clear_holds).pack(
            side="left", padx=(6, 0)
        )
        # 로그인할 때 자동으로 한 번 불러오지만(:meth:`_login_succeeded`),
        # 그 뒤 코레일 앱에서 직접 잡거나 취소했을 수 있습니다 — 이 프로그램을
        # 다시 켜지 않고도 지금 상태를 다시 물어볼 수 있어야 합니다.
        self.load_reservations_button = ttk.Button(
            toolbar, text="서버에서 불러오기", command=self.on_load_reservations
        )
        self.load_reservations_button.pack(side="left", padx=(14, 0))

        self.hold_tree = ttk.Treeview(
            frame,
            columns=HOLD_COLUMNS,
            # "tree" 도 켭니다 — 구간별 홀드를 묶으면(:meth:`sync_holds`) 그
            # 부모 줄을 접고 펼 +/- 표시가 이 칸에서 나옵니다.
            show="tree headings",
            selectmode="browse",
            height=3,
        )
        self.hold_tree.column("#0", width=20, stretch=False)
        for name, width, anchor in HOLD_LAYOUT:
            self.hold_tree.heading(name, text=name)
            self.hold_tree.column(
                name,
                width=width,
                anchor="w" if anchor == "w" else ("e" if anchor == "e" else "center"),
                stretch=(name == "여정"),
            )
        self.hold_tree.bind("<Button-3>", self._show_train_schedule_menu)
        self.hold_tree.bind("<Button-2>", self._show_train_schedule_menu)
        self.hold_tree.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.hold_tree.yview)
        self.hold_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky="ns", pady=4)
        # 기한이 가까우면 눈에 띄어야 합니다. 지난 것은 흐리게 — 지웠다고
        # 착각하지 않도록 남기되, 살아 있는 것과 구별합니다.
        self.hold_tree.tag_configure("urgent", foreground="#b3261e")
        self.hold_tree.tag_configure("expired", foreground="#8a8a8a")
        self.hold_tree.tag_configure("group", foreground="#1f6feb")
        ttk.Label(
            frame,
            text="이 프로그램은 결제하지 않습니다. 기한이 지나면 코레일이 예약을 "
            "스스로 취소합니다. [만료된 것 지우기] 는 서버에 아무것도 보내지 "
            "않고 이 목록에서만 지웁니다 — 이미 코레일이 취소했을 예약입니다.\n"
            "[선택 취소] 는 코레일에 실제로 취소를 요청합니다. 되돌릴 수 없습니다.\n"
            "[서버에서 불러오기] 로 채운 줄(종류: 불러온 예약)은 결제 기한·좌석 "
            "등급을 이 조회가 주지 않아 '코레일 앱에서 확인' 으로 비어 있고, "
            "취소도 이 프로그램에서 걸 수 없습니다 — 코레일 앱에서 취소하세요.",
            foreground="#666666",
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 6))

    def _tick_holds(self) -> None:
        """1초마다 줄어드는 것들을 다시 셉니다. 다른 일은 걸지 않습니다."""
        now = now_kst()
        for index, held in enumerate(self.holds):
            item = self._hold_items.get(index)
            if item is None:
                continue
            # 통째로 다시 그리지 않고 '남은 시간' 칸만 고쳐 씁니다 — 나머지
            # 열세 칸은 매초 다시 셀 이유가 없는 값입니다.
            values = list(self.hold_tree.item(item, "values"))
            if len(values) == len(HOLD_COLUMNS):
                values[-1] = remaining_text(held.deadline, now)
                self.hold_tree.item(item, values=values, tags=(held.tag(now),))
        # 예매 대상의 '남은 감시' 도 여기서 셉니다. 표를 통째로 다시 그리면
        # 고른 줄이 깜빡이므로 그 칸 하나만 고쳐 씁니다.
        for index, item in self._target_items.items():
            if index >= len(self.targets):
                continue
            watch = self._watch_of(self.targets[index])
            values = list(self.target_list.item(item, "values"))
            if len(values) == len(TARGET_COLUMNS):
                # 감시 시계는 벽시계가 아니라 monotonic 입니다 — 엔진과 같은 자.
                values[-1] = watch.remaining(time.monotonic()) if watch else "-"
                self.target_list.item(item, values=values)
        self.root.after(1000, self._tick_holds)

    def _held_from(
        self,
        label: str,
        summary: str,
        kind: str,
        hold: ReservationHoldResponse,
        direction: tuple[str, str, str] = ("", "", ""),
        group: str = "",
        full_summary: str = "",
        held_journey: Journey | None = None,
    ) -> Held:
        """서버 응답에서 화면이 쓸 것만 뽑습니다. 없는 값은 지어내지 않습니다."""
        return Held(
            label=label,
            summary=summary,
            kind=kind,
            direction=direction,
            pnr=(hold.pnr_no or "").strip() or "(PNR 없음)",
            fare=fare_text(hold),
            deadline=parse_deadline(
                hold.payment_deadline_date, hold.payment_deadline_time
            ),
            deadline_text=payment_deadline_text(hold),
            # 취소 폼은 정확히 이 타입만 받습니다. 다른 타입이 흘러들어 오면
            # (지금 코드 경로로는 없지만) 취소 버튼만 조용히 못 쓰게 둡니다 —
            # 표시나 카운트다운은 그것과 무관하게 그대로 돕니다.
            hold_response=hold if type(hold) is ReservationHoldResponse else None,
            group=group,
            full_summary=full_summary or summary,
            held_journey=held_journey,
        )

    def on_hold_made(
        self,
        label: str,
        summary: str,
        kind: str,
        direction: tuple[str, str, str],
        hold: ReservationHoldResponse,
        group: str = "",
        full_summary: str = "",
        held_journey: Journey | None = None,
    ) -> None:
        """자동예매 스레드에서 불립니다 — 큐를 거쳐 화면에 올립니다."""
        held = self._held_from(
            label, summary, kind, hold, direction, group, full_summary, held_journey
        )
        self.events.put(lambda: self.remember_hold(held))

    def remember_hold(self, held: Held) -> None:
        """잡은 예약 하나를 목록에 올립니다."""
        self.holds.append(held)
        self.sync_holds()
        item = self._hold_items.get(len(self.holds) - 1)
        if item is not None:
            self.hold_tree.see(item)

    def sync_holds(self) -> None:
        """잡은 예약 표를 ``self.holds`` 에서 다시 그립니다.

        취소·지우기 둘 다 목록 가운데를 뺄 수 있습니다. 줄 하나만 지우고
        번호를 밀어 쓰면 어긋나기 쉬우므로, :meth:`sync_target_list` 와
        같은 방식으로 **통째로 다시 그립니다.**

        구간별로 따로 산 홀드는 조회 결과·예매 대상과 같은 모양으로 한 부모
        줄 아래에 접습니다 — :attr:`~korail_booker.holds.Held.group` 이 같은
        것끼리입니다. 묶지 않는(``group`` 이 빈) 홀드는 그대로 혼자 한 줄입니다.
        """
        self.hold_tree.delete(*self.hold_tree.get_children())
        self._hold_items = {}
        self._hold_group_children = {}

        order: list[str] = []
        buckets: dict[str, list[int]] = {}
        for index, held in enumerate(self.holds):
            # 빈 group 은 절대 묶지 않습니다 — 저마다 유일한 열쇠를 줘서
            # 서로 다른 "묶지 않는" 홀드끼리 우연히 섞이지 않게 합니다.
            key = held.group if held.group else f"\0solo{index}"
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(index)

        for key in order:
            indices = buckets[key]
            if len(indices) == 1:
                self._insert_hold_row(indices[0], parent="")
            else:
                self._insert_hold_group(indices)

    def _hold_row_values(self, held: Held, now: datetime) -> tuple[str, ...]:
        """잡은 예약 한 줄의 열여섯 칸.

        칸 순서는 :data:`HOLD_LAYOUT` 과 정확히 같아야 합니다 — 구분·**종류**
        (좌석 예약/N구간)·열차·출발역·출발·도착역·도착·총 소요·환승 대기·
        좌석 셋·PNR·운임·결제 기한·남은 시간. 열차부터 좌석까지 열 칸은
        :meth:`_journey_row_values` 로 조회 결과·예매 대상과 **같은 방식으로**
        채웁니다(그 함수의 첫 값은 구분이고, 종류는 이 표에만 있는 칸이라
        끝에 잇지 않고 **구분 바로 다음에 끼워 넣습니다** — 한 번 이것을
        잊어 끝에 이었다가 칸이 통째로 한 칸씩 밀린 적이 있습니다).
        ``held_journey`` 가 없으면(옛 기록 등) 그 칸들만 "-" 로 비웁니다 —
        지어내지 않습니다.
        """
        if held.held_journey is not None:
            kind, *rest = self._journey_row_values(held.label, held.held_journey)
        else:
            kind, rest = held.label or "편도", ["-"] * 10
        return (
            kind,
            held.kind,
            *rest,
            held.pnr,
            held.fare,
            held.deadline_text,
            remaining_text(held.deadline, now),
        )

    def _insert_hold_row(self, index: int, *, parent: str) -> None:
        """잡은 예약 한 줄. 묶음 아래(자식)든 최상위(단독)든 같은 모양입니다."""
        held = self.holds[index]
        now = now_kst()
        item = self.hold_tree.insert(
            parent, "end", values=self._hold_row_values(held, now), tags=(held.tag(now),)
        )
        self._hold_items[index] = item
        # 이 홀드가 가리키는 여정이 (아직 나뉘지 않은) 환승이면, 조회
        # 결과와 똑같이 1구간/2구간 정보 줄을 펼칩니다. 구간별로 이미
        # 나뉜 홀드는 held_journey 자체가 구간 하나짜리라 더 펼칠 것이
        # 없습니다.
        journey = held.held_journey
        if journey is None or not journey.is_transfer:
            return
        for leg_index in range(len(journey.legs)):
            kind, *rest = self._leg_row_values(journey, leg_index)
            self.hold_tree.insert(
                item, "end", values=(kind, "", *rest, "", "", "", ""), tags=("leg",)
            )
        self.hold_tree.item(item, open=True)

    def _insert_hold_group(self, indices: list[int]) -> None:
        """구간별로 따로 산 홀드 여럿을 한 부모 줄 아래에 접습니다.

        부모 줄은 PNR 하나를 가리키지 않으므로 :attr:`_hold_items` 에 넣지
        않습니다 — 골라도 :meth:`_selected_hold_index` 가 찾지 못해 [선택
        취소] 가 "고르세요" 로 막습니다. 취소는 구간(자식 줄)을 직접 골라야
        합니다 — 어느 PNR 을 취소하는지 헷갈리면 안 되는 일이기 때문입니다.
        """
        first = self.holds[indices[0]]
        parent = self.hold_tree.insert(
            "",
            "end",
            values=(
                first.label or "편도",
                "구간별 예약",
                "",
                "",
                "",
                "",
                "",
                "",
                f"{first.full_summary or first.summary} · 구간 {len(indices)}개로 "
                "나누어 샀습니다 — 결제도 구간마다 따로입니다",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ),
            tags=("group",),
            open=True,
        )
        self._hold_group_children[parent] = list(indices)
        for index in indices:
            self._insert_hold_row(index, parent=parent)

    def _selected_hold_index(self) -> int | None:
        """잡은 예약 표에서 고른 한 줄의 번호. 여러 개는 고를 수 없습니다."""
        by_item = {item: index for index, item in self._hold_items.items()}
        for item in self.hold_tree.selection():
            index = by_item.get(item)
            if index is not None and index < len(self.holds):
                return index
        return None

    def remove_holds(self, indices: list[int]) -> None:
        """잡은 예약 목록에서 이 번호들을 뺍니다. **서버에는 아무것도 보내지
        않습니다** — 화면 목록만 정리합니다."""
        for index in sorted(set(indices), reverse=True):
            if 0 <= index < len(self.holds):
                del self.holds[index]
        self.sync_holds()

    def clear_expired_holds(self) -> None:
        """기한이 지난 것만 목록에서 뺍니다.

        기한이 지나면 코레일이 그 홀드를 스스로 취소합니다 — 이 목록에는
        더 실려 있을 이유가 없는 죽은 정보입니다. 그래서 서버에 묻지 않고
        곧바로 지웁니다.
        """
        now = now_kst()
        expired = [i for i, held in enumerate(self.holds) if is_expired(held.deadline, now)]
        if not expired:
            messagebox.showinfo("잡은 예약", "기한이 지난 예약이 없습니다.")
            return
        self.remove_holds(expired)
        self._write_log(f"기한이 지난 예약 {len(expired)}건을 목록에서 지웠습니다.")

    def clear_holds(self) -> None:
        """잡은 예약 목록을 통째로 비웁니다. **서버에는 아무것도 보내지
        않습니다** — 여기서 지워도 서버의 예약은 그대로입니다.

        결제 기한이 아직 남은 것이 있으면 한 번 더 묻습니다 — 이 목록이
        PNR 을 보는 유일한 자리인데, 지우고 나면 화면에서 사라지기
        때문입니다.
        """
        if not self.holds:
            return
        now = now_kst()
        unpaid = [held for held in self.holds if not is_expired(held.deadline, now)]
        if unpaid and not messagebox.askyesno(
            "잡은 예약 비우기",
            f"결제 기한이 남은 예약이 {len(unpaid)}건 있습니다. 지워도 코레일의 "
            "예약은 그대로지만, 이 화면에서는 PNR 을 다시 볼 수 없습니다.\n\n"
            + "\n".join(f"· {held.summary} — PNR {held.pnr}" for held in unpaid[:5])
            + "\n\n그래도 비울까요?",
        ):
            return
        self.holds.clear()
        self.sync_holds()
        self._write_log("잡은 예약 목록을 비웠습니다 (서버의 예약은 그대로입니다).")

    def on_cancel_hold(self) -> None:
        """고른 예약을 코레일에 취소 요청합니다. **되돌릴 수 없습니다.**

        이 프로그램이 만드는 첫 취소 요청입니다 — 지금까지는 예약 consent
        하나만 열었습니다(:func:`cancel_consent`). 그래서 확인을 무겁게
        둡니다: 여정·PNR·운임·기한을 모두 보여 주고, 그래도 좋다고 해야
        나갑니다.
        """
        index = self._selected_hold_index()
        if index is None:
            messagebox.showwarning("예약 취소", "취소할 예약을 목록에서 고르세요")
            return
        held = self.holds[index]
        original = held.hold_response
        if original is None:
            messagebox.showwarning(
                "예약 취소",
                "이 줄에는 취소에 필요한 원본 정보가 없습니다 — 코레일 앱에서 "
                "직접 취소하세요.",
            )
            return
        if is_expired(held.deadline, now_kst()):
            messagebox.showinfo(
                "예약 취소",
                "결제 기한이 이미 지났습니다 — 코레일이 이 홀드를 스스로 "
                "취소했을 것입니다. 취소 요청을 보낼 필요가 없습니다.\n\n"
                "목록에서만 지우려면 [만료된 것 지우기] 를 누르세요.",
            )
            return
        if not self.logged_in:
            messagebox.showwarning("예약 취소", "먼저 로그인하세요")
            return
        if not messagebox.askyesno(
            "예약 취소",
            "코레일에 취소를 요청합니다. 되돌릴 수 없습니다.\n\n"
            f"{held.summary}\n"
            f"PNR {held.pnr}\n"
            f"운임 {held.fare}\n"
            f"결제 기한 {held.deadline_text}\n\n"
            "정말 취소할까요?",
        ):
            return
        self.cancel_hold_button.configure(state="disabled")

        def work() -> None:
            try:
                client = self._ensure_client()
                result = client.cancel_unpaid_hold(
                    original, consent=cancel_consent()
                )
            except KorailApiError as exc:
                message = f"{type(exc).__name__}: {exc}"
                self.events.put(lambda: self._cancel_hold_failed(message))
                return
            self.events.put(lambda: self._cancel_hold_done(held, result))

        self._in_thread(work, "korail-cancel")

    def _cancel_hold_failed(self, message: str) -> None:
        self.cancel_hold_button.configure(state="normal")
        self._write_log(f"예약 취소 실패 — {message}", "bad")
        messagebox.showerror("예약 취소 실패", message)

    def _cancel_hold_done(
        self, held: Held, result: MutationPreview | object
    ) -> None:
        self.cancel_hold_button.configure(state="normal")
        if isinstance(result, MutationPreview):
            # 이 자리는 늘 dry_run=False 로만 부르므로 실제로는 오지 않지만,
            # 다른 경로가 실수로 미리보기를 넘겨도 목록에서 지우지 않도록
            # 지켜 둡니다 — 취소가 안 나갔는데 지우면 예약이 그대로 남은
            # 채로 화면에서만 사라집니다.
            self._write_log("예약 취소: 미리보기라 아무것도 보내지 않았습니다.", "warn")
            return
        index = next((i for i, h in enumerate(self.holds) if h is held), None)
        if index is not None:
            self.remove_holds([index])
        self._write_log(f"예약을 취소했습니다 — {held.summary}\nPNR {held.pnr}", "good")
        messagebox.showinfo(
            "예약을 취소했습니다", f"{held.summary}\n\nPNR {held.pnr}"
        )

    def _build_booking_controls(self, frame: ttk.LabelFrame) -> None:
        """자동예매 조건과 [시작]. 예매 대상 표 **바로 아래**에 붙습니다.

        따로 묶음을 두지 않습니다 — 담은 것과 그것을 노리는 조건은 한 가지
        일입니다.
        """
        self.poll_interval = tk.StringVar(value=f"{DEFAULT_POLL_INTERVAL_S:g}")
        self.watch_minutes = tk.StringVar(value="60")
        self.allow_standby = tk.BooleanVar(value=False)
        self.notify_enabled = tk.BooleanVar(value=True)
        ttk.Separator(frame, orient="horizontal").grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=4, pady=(2, 0)
        )
        row = ttk.Frame(frame)
        row.grid(row=3, column=0, columnspan=2, sticky="w", padx=4, pady=6)
        ttk.Label(row, text=f"조회 주기({POLL_HINT})").pack(side="left")
        ttk.Entry(row, textvariable=self.poll_interval, width=5).pack(side="left", padx=2)
        ttk.Label(row, text="초    감시 시간").pack(side="left")
        ttk.Entry(row, textvariable=self.watch_minutes, width=5).pack(side="left", padx=2)
        ttk.Label(row, text="분 (0=무제한)").pack(side="left", padx=(0, 12))
        ttk.Checkbutton(row, text="예약대기도 시도(직통·일반실)", variable=self.allow_standby).pack(
            side="left"
        )
        ttk.Checkbutton(row, text="텔레그램 알림", variable=self.notify_enabled).pack(
            side="left"
        )
        row2 = ttk.Frame(frame)
        row2.grid(row=4, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 6))
        ttk.Button(row2, text="텔레그램 설정", command=self.on_telegram_settings).pack(
            side="left", padx=12
        )
        # 담긴 것 전부와 고른 것만 — 넷으로 나눕니다. 하나로 두면 여러 여정을
        # 담아 두고 그중 하나만 노릴 수가 없습니다.
        self.start_button = ttk.Button(row2, text="전체 시작", command=self.on_start)
        self.start_button.pack(side="left", padx=4)
        ttk.Button(row2, text="고른 것만 시작", command=self.on_start_selected).pack(
            side="left", padx=2
        )
        self.stop_button = ttk.Button(
            row2, text="전체 중지", command=self.on_stop, state="disabled"
        )
        self.stop_button.pack(side="left", padx=(10, 2))
        ttk.Button(row2, text="고른 것만 중지", command=self.on_stop_selected).pack(
            side="left"
        )
        # 미리보기 스위치는 없앴습니다. 켜는 것을 잊고 미리보기를 진짜라고
        # 믿는 일이 실제로 생겼고, 이 프로그램을 켜는 이유가 진짜 예약이기
        # 때문입니다. 대신 시작할 때 확인 창이 뜨고, 로그인하지 않았으면
        # 시작 자체가 막힙니다.
        ttk.Label(
            row2,
            text="누르면 진짜 예약을 만듭니다 (결제는 하지 않음)",
            foreground="#a11",
        ).pack(side="left", padx=12)
        ttk.Label(
            frame,
            text="위 표(3.)에서 고르고 [담기](줄을 두 번 눌러도 담깁니다). 이 표에서 "
            "두 번 누르면 빠집니다. 왕복이면 가는 편·오는 편을 각각 담으세요 — "
            "방향마다 한 건씩 잡고 멈춥니다.\n"
            "조회 주기·감시 시간은 시작할 때 읽습니다. 도는 중에 바꾸려면 그 줄을 "
            "고르고 [조건 바꿔 재시작]. 결제는 하지 않습니다 — 잡은 뒤 코레일 "
            "앱에서 기한 안에 결제하세요.",
            foreground="#666666",
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 6))

    def _build_log(self, parent: tk.PanedWindow) -> None:
        """기록을 둘로 나눕니다 — 조회 쪽과 자동예매 쪽.

        한 창에 섞어 두면 자동예매가 도는 동안 회차 기록이 조회 기록을 밀어
        올려, 정작 보고 싶은 "지금 몇 번째 조회에서 무엇이 매진인지" 가 흘러가
        버립니다. 가운데 손잡이를 끌어 폭을 정할 수 있습니다.
        """
        paned = ttk.PanedWindow(parent, orient="horizontal")
        # 기록도 줄여도 됩니다. pady 는 tk.PanedWindow 에서 숫자 하나만
        # 받습니다(튜플은 거절).
        self._add_pane(parent, paned, minsize=110, stretch="always")
        self.log_text = self._log_pane(
            paned, "기록 (로그인·조회)", self.clear_log, weight=3
        )
        self.booking_text = self._log_pane(
            paned, "자동예매 기록", self.clear_booking_log, weight=2
        )

    def _log_pane(
        self,
        parent: tk.PanedWindow,
        title: str,
        clear: Callable[[], None],
        *,
        weight: int,
    ) -> tk.Text:
        frame = ttk.LabelFrame(parent, text=title)
        parent.add(frame, weight=weight)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = tk.Text(frame, height=6, width=40, wrap="word", state="disabled")
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        # 시각은 흐리게, 본문은 그대로. 색은 뜻이 있을 때만 씁니다 — 전부
        # 색칠하면 아무것도 눈에 띄지 않습니다.
        text.tag_configure("stamp", foreground="#999999")
        text.tag_configure("detail", foreground="#555555")
        text.tag_configure("good", foreground="#1a7f37")
        text.tag_configure("bad", foreground="#b3261e")
        text.tag_configure("warn", foreground="#a15c00")
        ttk.Button(frame, text="지우기", width=8, command=clear).grid(
            row=0, column=2, sticky="n", padx=6, pady=4
        )
        return text

    # -- 설정 되살리기 -------------------------------------------------------

    def _restore(self) -> None:
        stored = self.settings
        self.login_id.set(stored.login_id)
        self.departure.set(stored.departure)
        self.arrival.set(stored.arrival)
        self.after_time.set(format_clock(stored.depart_after) if stored.depart_after else "")
        self.before_time.set(
            format_clock(stored.depart_before) if stored.depart_before else ""
        )
        self.round_trip.set(stored.round_trip)
        self.return_after_time.set(
            format_clock(stored.return_depart_after) if stored.return_depart_after else ""
        )
        self.return_before_time.set(
            format_clock(stored.return_depart_before)
            if stored.return_depart_before
            else ""
        )
        for kind, var in self.train_kind_vars.items():
            var.set(kind in stored.train_names)
        self.sync_train_kinds()
        for label, preference in SEAT_CHOICES:
            if preference.value == stored.seat_preference:
                self.seat_choice.set(label)
        self.include_direct.set(stored.include_direct)
        self.include_transfer.set(stored.include_transfer)
        self.transfer_mode.set(stored.transfer_mode or TRANSFER_SERVER)
        self._fill_transfer_stations(list(stored.transfer_stations), select_all=True)
        self.min_transfer.set(str(stored.min_transfer_minutes))
        self.max_transfer.set(str(stored.max_transfer_minutes))
        self.poll_interval.set(f"{stored.poll_interval_s:g}")
        self.watch_minutes.set(str(stored.watch_minutes))
        self.allow_standby.set(stored.allow_standby)
        self.notify_enabled.set(stored.notify_enabled)
        for key, var in self.passenger_vars.items():
            var.set(str(getattr(stored, key)))
        self.sync_transfer_state()
        # 켜자마자의 '오는 편' 칸 상태도 체크박스를 따라야 합니다. 이것을
        # 부르지 않으면 왕복이 꺼져 있는데도 오는 날짜와 [달력] 이 눌렸습니다.
        self._round_trip_toggled()

    # -- 결과가 지금 조건의 것인지 -------------------------------------------

    def mark_stale(self, *_event: object) -> None:
        """조건이 바뀌었음을 표시합니다. 표는 그대로 두고 말만 바꿉니다.

        조건을 바꿔도 표는 이전 결과 그대로라, 아무 말이 없으면 "바꿨는데
        아무 일도 안 일어난다" 로 보입니다. 자동으로 다시 조회하지는 않습니다 —
        체크 하나 누를 때마다 서버에 요청이 나가는 편이 더 나쁩니다.
        """
        if not self.journeys:
            return
        self.results_status.set("조건이 바뀌었습니다 — [조회] 를 다시 누르세요.")
        self.results_label.configure(foreground="#a15c00")

    def _watch_for_changes(self, *variables: tk.Variable) -> None:
        for variable in variables:
            variable.trace_add("write", lambda *_args: self.mark_stale())

    # -- 열차 종별 -----------------------------------------------------------

    def selected_train_kinds(self) -> tuple[str, ...]:
        return tuple(kind for kind, var in self.train_kind_vars.items() if var.get())

    def sync_train_kinds(self) -> None:
        self.mark_stale()
        picked = self.selected_train_kinds()
        # 짧게 씁니다. 이 글이 길면 줄이 넓어지고, 줄이 넓어지면 오른쪽 환승
        # 조건이 옆에 못 서서 묶음이 세로로 길어집니다.
        self.train_kind_label.set("전체" if not picked else f"{len(picked)}종")

    def select_all_train_kinds(self) -> None:
        """여덟 종을 다 켭니다.

        아무것도 고르지 않은 것과 전부 고른 것은 **거르는 결과가 같습니다.**
        그래도 단추를 둡니다 — 하나만 빼고 보고 싶을 때 여덟 번 누르는 대신
        전부 켜고 하나만 끄면 되기 때문입니다.
        """
        for var in self.train_kind_vars.values():
            var.set(True)
        self.sync_train_kinds()

    def clear_train_kinds(self) -> None:
        for var in self.train_kind_vars.values():
            var.set(False)
        self.sync_train_kinds()

    # -- 환승 조건 -----------------------------------------------------------

    def _transfer_toggled(self) -> None:
        """환승 체크나 모드가 바뀌었을 때. 상태를 맞추고 결과를 낡음으로."""
        self.sync_transfer_state()
        self.mark_stale()
        self._offer_transfer_candidates()

    def _reset_transfer_load_button(self) -> None:
        """[후보 갱신] 을 지금 상태에 맞춰 되돌립니다."""
        self.transfer_load_button.configure(
            state="normal" if self.include_transfer.get() else "disabled"
        )

    def _offer_transfer_candidates(self) -> None:
        """모드를 바꿨을 때 서버 후보를 **비어 있으면만** 채웁니다.

        두 모드에서 이 목록의 뜻이 다릅니다 — 서버 추천에서는 결과를 거르는
        필터, 직접 지정에서는 조회할 역 그 자체. 그래서 모드를 바꾸고 나면
        대개 후보가 필요합니다.

        그렇다고 **덮지는 않습니다.** 이 저장소가 이미 정한 규칙이 있습니다:
        [조회] 는 목록이 비어 있을 때만 채우고, [후보 갱신] 은 더하기만 하며,
        지우는 것은 [빼기]·[비우기] 뿐입니다. 모드 전환이 그 규칙을 뒤로
        돌아 손으로 만든 목록을 지우면, 빼 둔 역이 조용히 되살아납니다 —
        고쳐 놓은 버그가 새 경로로 되살아나는 것입니다.

        구간이 비어 있거나 불러오다 실패해도 조용히 넘어갑니다. 모드를 바꾸는
        일이 네트워크 때문에 막히면 안 됩니다.
        """
        if not self.include_transfer.get():
            return
        if self.transfer_names():
            # 사람이 만든 목록이 있습니다. 표시만 다시 그리고 길을 알려 줍니다.
            self._redraw_transfer_marks()
            self._write_log(
                "환승 모드를 바꿨습니다 — 환승역 목록은 그대로 둡니다. "
                "서버 후보를 더하려면 [후보 갱신], 지우려면 [빼기]·[비우기]."
            )
            return
        departure = self.departure.get().strip()
        arrival = self.arrival.get().strip()
        if not departure or not arrival:
            return
        self.transfer_load_button.configure(state="disabled")

        def work() -> None:
            try:
                client = self._ensure_client()
                names = transfer_station_candidates(client, departure, arrival)
            except (KorailApiError, ValueError) as exc:
                # 곁가지입니다. 모드 전환은 이미 끝났고, 목록은 손으로도
                # 채울 수 있습니다.
                self.log(f"환승역 후보를 불러오지 못했습니다: {exc}")
                self.events.put(self._reset_transfer_load_button)
                return
            self._transfer_route = (departure, arrival)
            self.events.put(lambda: self._transfer_stations_loaded(names))

        self._in_thread(work, "korail-transfer-stations")

    def sync_transfer_state(self) -> None:
        """환승 조건은 환승을 켰을 때만, 그리고 **조회가 도는 중이 아닐 때만**
        만질 수 있습니다.

        고른 환승역의 구실도 여기서 갱신합니다 — 모드에 따라 뜻이 다릅니다.

        조회 스레드가 그 구간의 환승역 후보를 스스로 받아 와 이 함수를 다시
        부릅니다(:meth:`_refresh_transfer_stations` →
        :meth:`_server_candidates_loaded`/:meth:`_transfer_stations_loaded`).
        그 호출이 :attr:`_query_locked` 를 안 보면 "환승이 켜져 있으니까" 라는
        이유만으로, 조회가 아직 도는 중인데도 이 칸이 도로 풀립니다 — 실제로
        그랬습니다. 그래서 잠겨 있으면 무엇을 골랐든 무조건 잠급니다.
        """
        if not self.include_transfer.get():
            self.transfer_role.set("‘환승’을 켜야 환승 조건을 쓸 수 있습니다.")
        elif self.transfer_mode.get() == TRANSFER_CUSTOM:
            self.transfer_role.set(
                "고른 역을 경유하도록 직접 조회합니다 (하나 이상 필수)."
            )
        else:
            self.transfer_role.set(
                "서버가 준 환승 여정 중 고른 역을 지나는 것만 봅니다"
                " (고르지 않으면 전부)."
            )
        unlocked = self.include_transfer.get() and not self._query_locked
        state = "normal" if unlocked else "disabled"
        for widget in (
            self.server_radio,
            self.custom_radio,
            self.min_transfer_entry,
            self.max_transfer_entry,
            self.transfer_load_button,
            self.transfer_clear_button,
        ):
            widget.configure(state=state)
        # 역을 손으로 넣는 것은 직접 지정 모드에서만 뜻이 있습니다 — 서버 추천
        # 모드에서 없는 역을 넣으면 결과를 0편으로 만드는 필터가 될 뿐입니다.
        adding = (
            "normal"
            if unlocked and self.transfer_mode.get() == TRANSFER_CUSTOM
            else "disabled"
        )
        self.transfer_entry.configure(state=adding)
        self.transfer_add_button.configure(state=adding)
        # 빼기는 서버 추천 모드에서도 됩니다 — 그쪽에서 고른 역은 필터라,
        # 목록에서 지우는 것이 곧 필터에서 빼는 것입니다.
        self.transfer_remove_button.configure(state="normal" if unlocked else "disabled")
        self.transfer_list.configure(state=state)

    def selected_transfer_stations(self) -> tuple[str, ...]:
        """고른 역 이름. 화면에 붙은 ``(검증)`` 은 떼고 돌려줍니다 —
        조회에 나가는 것은 역 이름이지 화면 글이 아닙니다."""
        picked = tuple(
            _plain_station(self.transfer_list.get(index))
            for index in self.transfer_list.curselection()
        )
        return tuple(name for name in picked if name)

    def _server_candidates_loaded(self, route: tuple[str, str], names: list[str]) -> None:
        """조회하면서 받아 온 서버 후보. **사람이 만든 목록을 덮지 않습니다.**

        모드를 가리지 않습니다. 두 모드 모두에서 목록은 사람이 손댄 것입니다 —
        직접 지정에서는 고른 역이 곧 조회 대상이고, 서버 추천에서는 고른 역이
        결과를 거르는 필터입니다. 어느 쪽이든 [조회] 를 눌렀다고 목록이 서버
        후보 전체로 되돌아가면, 빼 둔 역이 조용히 되살아납니다 — 실제로
        그랬습니다.

        그래도 받아 온 것은 버리지 않습니다. 어느 역이 서버도 인정하는
        역인지를 목록에 ``(검증)`` 으로 붙여 주는 데 씁니다. 목록이 아직
        비어 있을 때만 채웁니다 — 그때는 잃을 것이 없습니다.
        """
        self._server_stations = set(names)
        if self.transfer_names():
            # 이미 목록이 있으면 그대로 두고 표시만 다시 그립니다.
            self._redraw_transfer_marks()
            self._write_log(
                f"{route[0]}→{route[1]} 서버 환승역 후보 {len(names)}개를 "
                "받아 (검증) 표시를 새로 달았습니다. 목록 자체는 그대로 "
                "둡니다 — 서버 후보를 목록에 더하려면 [후보 갱신] 을 누르세요."
            )
            return
        self._transfer_stations_loaded(names)

    def transfer_names(self) -> tuple[str, ...]:
        """목록에 있는 역 이름 전부. 붙어 있는 ``(검증)`` 은 뗍니다."""
        return tuple(
            _plain_station(self.transfer_list.get(index))
            for index in range(self.transfer_list.size())
        )

    def _redraw_transfer_marks(self) -> None:
        """이름은 그대로 두고 ``(검증)`` 표시만 다시 답니다."""
        chosen = set(self.selected_transfer_stations())
        self._fill_transfer_stations(
            list(self.transfer_names()), select_all=False, keep=chosen
        )

    def _fill_transfer_stations(
        self,
        names: list[str],
        *,
        select_all: bool = True,
        keep: set[str] | None = None,
    ) -> None:
        """목록을 채웁니다. **기본은 전부 선택** 입니다.

        고른 것이 하나도 없는 상태는 두 모드에서 뜻이 갈립니다 — 서버 추천에서는
        "전부 보기", 직접 지정에서는 "조회할 역이 없음". 전부 선택해 두면 화면에
        보이는 것과 실제로 쓰이는 것이 같아집니다.

        서버도 후보로 준 역에는 ``(검증)`` 을 답니다. 직접 지정으로 넣은 역이
        서버 추천과 겹치는지 아닌지는, 그 조합을 서버가 받아 줄 가능성을 재는
        유일한 단서입니다.
        """
        keeping = set(self.selected_transfer_stations()) if keep is None else keep
        self.transfer_list.configure(state="normal")
        self.transfer_list.delete(0, "end")
        for name in names:
            mark = VERIFIED_MARK if name in self._server_stations else ""
            self.transfer_list.insert("end", f"{name}{mark}")
        for index, name in enumerate(names):
            if select_all or name in keeping:
                self.transfer_list.selection_set(index)
        self.sync_transfer_state()

    def remove_transfer_station(self) -> None:
        """고른 환승역을 목록에서 뺍니다. 통째로 지우려면 [목록 비우기]."""
        chosen = self.transfer_list.curselection()
        if not chosen:
            messagebox.showinfo("환승역", "뺄 역을 목록에서 고르세요")
            return
        for index in sorted(chosen, reverse=True):
            self.transfer_list.delete(index)
        self.mark_stale()

    def add_transfer_station(self) -> None:
        """친 역을 목록에 넣고 고릅니다. 서버 후보에 없어도 됩니다."""
        name = self.transfer_query.get().strip()
        if not name:
            return
        if self.station_names and name not in self.station_names:
            messagebox.showwarning(
                "환승역",
                f"'{name}' 은 역 목록에 없습니다. 이름을 확인하세요 "
                "(예: '동대구', '서대전').",
            )
            return
        existing = list(self.transfer_names())
        if name not in existing:
            existing.append(name)
        # 넣은 역은 골라 둡니다 — 직접 지정에서는 고른 것이 곧 조회 대상입니다.
        keep = set(self.selected_transfer_stations()) | {name}
        self._fill_transfer_stations(existing, select_all=False, keep=keep)
        self.transfer_query.set("")
        self.mark_stale()
        self._write_log(f"환승역 후보에 {name} 을 넣었습니다.")

    def on_load_transfer_stations(self) -> None:
        """이 구간에서 갈아탈 수 있는 역만 불러옵니다. 전국 역 목록이 아닙니다."""
        departure = self.departure.get().strip()
        arrival = self.arrival.get().strip()
        if not departure or not arrival:
            messagebox.showwarning("환승역", "출발역과 도착역을 먼저 입력하세요")
            return
        self.transfer_load_button.configure(state="disabled")

        def work() -> None:
            client = self._ensure_client()
            names = transfer_station_candidates(client, departure, arrival)
            self._transfer_route = (departure, arrival)
            self.events.put(lambda: self._transfer_stations_loaded(names))

        self._in_thread(work, "korail-transfer-stations")

    def _transfer_stations_loaded(self, names: list[str]) -> None:
        """[구간 후보 갱신] 의 결과. **손으로 넣은 역을 지우지 않습니다.**

        예전에는 목록을 통째로 서버가 준 것으로 되돌렸습니다. 그런데 직접
        지정 모드에서 그것은 사람이 방금 만든 목록을 지우는 일입니다 — 서버
        후보에 없는 역을 넣을 수 있게 해 놓고, 후보를 한 번 더 불러오면
        그 역이 사라졌습니다.

        그래서 **합칩니다**: 목록에 있던 것은 그대로 두고, 서버가 준 것 중
        없던 것만 뒤에 붙입니다. 처음부터 다시 하고 싶으면 [목록 비우기] 가
        있습니다 — 지우는 일은 지우는 단추가 합니다.
        """
        self.transfer_load_button.configure(state="normal")
        self._server_stations = set(names)
        existing = list(self.transfer_names())
        added = [name for name in names if name not in existing]
        merged = existing + added
        # 새로 붙은 것은 골라 둡니다. 직접 지정에서는 고른 것이 곧 조회
        # 대상이고, 서버 추천에서는 고른 것이 필터입니다 — 어느 쪽이든
        # 방금 불러온 후보를 꺼 둔 채로 두면 불러온 뜻이 없습니다.
        keep = set(self.selected_transfer_stations()) | set(added)
        self._fill_transfer_stations(merged, select_all=not existing, keep=keep)
        if not names:
            self._write_log("이 구간에는 서버가 알려 주는 환승역이 없습니다.")
            return
        self._write_log(
            f"{self.departure.get()}→{self.arrival.get()} 서버 환승역 "
            f"{len(names)}개를 불러왔습니다 — 새로 붙은 것 {len(added)}개, "
            f"원래 있던 {len(existing)}개는 그대로입니다."
        )

    def clear_transfer_stations(self) -> None:
        """환승역 목록을 통째로 비웁니다. 지우는 일은 이 단추가 합니다."""
        self._fill_transfer_stations([], select_all=False, keep=set())
        self.mark_stale()
        self._write_log("환승역 목록을 비웠습니다.")

    def _remember_booking_options(self) -> None:
        """감시 조건도 설정 파일에 남깁니다.

        ``_restore`` 는 켤 때 이 셋을 되읽는데, 정작 아무도 쓰지 않았습니다 —
        고쳐 놓아도 다음에 켜면 기본값으로 돌아갔습니다.
        """
        try:
            options = self.build_options()
        except (ValueError, TypeError):
            return
        self.settings = replace(
            self.settings,
            poll_interval_s=options.poll_interval_s,
            watch_minutes=options.watch_minutes,
            allow_standby=options.allow_standby,
        )

    def _remember(self, request: SearchRequest) -> None:
        self.settings = replace(
            self.settings,
            login_id=self.login_id.get().strip(),
            departure=request.departure,
            arrival=request.arrival,
            depart_after=request.depart_after,
            depart_before=request.depart_before,
            round_trip=self.round_trip.get(),
            # 여기서 다시 파싱하지 않습니다. 저장은 곁가지인데, 예외가 나면
            # [조회] 콜백 전체가 조용히 죽어 단추가 아무 일도 안 하는 것처럼
            # 보였습니다. 값은 이미 조회 때 검사한 것을 그대로 씁니다.
            return_depart_after=self._last_return_after,
            return_depart_before=self._last_return_before,
            train_names=list(request.train_names),
            seat_preference=request.seat_preference.value,
            include_direct=request.include_direct,
            include_transfer=request.include_transfer,
            transfer_mode=request.transfer_mode,
            transfer_stations=list(request.transfer_stations),
            min_transfer_minutes=request.min_transfer_minutes,
            max_transfer_minutes=request.max_transfer_minutes,
            notify_enabled=self.notify_enabled.get(),
            adult=request.passengers.adult,
            teenager=request.passengers.teenager,
            child=request.passengers.child,
            infant=request.passengers.infant,
            senior=request.passengers.senior,
        )
        self._remember_booking_options()
        settings_module.save(self.settings)

    # -- 로그와 스레드 -------------------------------------------------------

    def log(self, message: str) -> None:
        """스레드 어디서 불러도 됩니다 — 실제 쓰기는 :meth:`_drain` 에서."""
        self.events.put(lambda: self._write_log(message))

    def log_booking(self, message: str) -> None:
        """자동예매 쪽 기록. :class:`AutoBooker` 가 이것을 부릅니다."""
        self.events.put(lambda: self._write_booking(message))

    def _write_log(self, message: str, level: str = "info") -> None:
        self._append(self.log_text, message, level)

    def _write_booking(self, message: str, level: str = "info") -> None:
        self._append(self.booking_text, message, level)

    def _append(self, widget: tk.Text, message: str, level: str = "info") -> None:
        """기록 한 덩어리를 씁니다. 어떻게 그릴지는 :mod:`logfmt` 가 정합니다."""
        entry = format_entry(message, stamp=time.strftime("%H:%M:%S"), level=level)
        if entry is None:
            return
        widget.configure(state="normal")
        if entry.blank_before and widget.index("end-1c") != "1.0":
            widget.insert("end", "\n")
        for text, tag in entry.pieces:
            widget.insert("end", text, tag)
        widget.see("end")
        widget.configure(state="disabled")

    def clear_log(self) -> None:
        """기록을 비웁니다. 오래 돌리면 스크롤이 감당이 안 됩니다."""
        self._clear(self.log_text)

    def clear_booking_log(self) -> None:
        self._clear(self.booking_text)

    @staticmethod
    def _clear(widget: tk.Text) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.configure(state="disabled")

    def _drain(self) -> None:
        while True:
            try:
                action = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                action()
            except Exception as exc:
                self._write_log(f"화면 갱신 오류: {type(exc).__name__}: {exc}")
        # 큐를 비우는 이 자리는 120ms 마다 돕니다. 여기에 다른 일을 걸면 그 일도
        # 초당 여덟 번씩 돕니다 — 역 목록 조회가 실제로 그렇게 새어 나갔습니다.
        self.root.after(120, self._drain)

    def _in_thread(self, work: Callable[[], None], name: str) -> None:
        """작업 스레드 하나. 무슨 예외가 나든 조용히 죽지 않습니다.

        스레드에서 새는 예외는 아무 데도 찍히지 않고 사라집니다. 그러면 눌러
        둔 단추가 영영 잠긴 채로 화면만 멀쩡해 보입니다 — 실제로 그렇게
        보였습니다. 여기서 붙잡아 기록에 남기고 단추를 되돌립니다.
        """

        def guarded() -> None:
            try:
                work()
            except Exception as exc:  # 화면까지 죽이지 않는다
                detail = f"{type(exc).__name__}: {exc}"
                self.events.put(lambda: self._worker_failed(name, detail))

        threading.Thread(target=guarded, name=name, daemon=True).start()

    def _worker_failed(self, name: str, detail: str) -> None:
        self._write_log(f"[{name}] 예상 못 한 오류: {detail}")
        self._reset_buttons()
        # 창은 **큐를 비운 뒤에** 엽니다. 여기서 바로 열면 모달이 제 이벤트
        # 고리를 돌리는 동안 :meth:`_drain` 이 다음 회차를 예약하지 못해,
        # 사람이 [확인] 을 누를 때까지 화면 갱신이 통째로 멈춥니다.
        self.root.after(0, lambda: messagebox.showerror("오류", detail))

    def _reset_buttons(self) -> None:
        self.login_button.configure(state="normal")
        # 요청이 아직 나가 있으면 되살리지 않습니다. 한 번 더 누르면 같은
        # 열차에 두 번째 예약이 나갑니다.
        if not self._reserving:
            self.reserve_now_button.configure(state="normal")
        # 조회가 아직 돌고 있으면 건드리지 않습니다. 다른 일이 실패했다고
        # [조회] 를 되살리고 진행 막대를 거두면, 도는 조회가 없는 것처럼
        # 보여 사람이 하나 더 겁니다.
        if self._search_token is None:
            self.search_button.configure(state="normal")
            self._searching(False)
        self.transfer_load_button.configure(
            state="normal" if self.include_transfer.get() else "disabled"
        )
        if not self.any_running():
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")

    # -- 클라이언트 ----------------------------------------------------------

    def _ensure_client(self) -> KorailClient:
        """클라이언트 하나. **스레드 둘이 동시에 불러도 하나입니다.**

        잠금이 없던 때에는 조회 스레드와 로그인 스레드가 같은 순간에 들어와
        클라이언트를 둘 만들었고, 로그인이 버려지는 쪽에 붙으면 그 뒤의 예약이
        전부 "로그인하지 않았습니다" 로 막혔습니다.
        """
        with self._client_lock:
            if self.client is None:
                client, identity = build_client()
                self.client = client
                self.identity = identity
                self.log(f"클라이언트를 만들었습니다 (기기 신원: {identity})")
            return self.client

    # -- 동작: 로그인 --------------------------------------------------------

    def _start_login(
        self,
        member_no: str,
        password: str,
        done: Callable[[str | None], None],
    ) -> None:
        """작업 스레드에서 로그인하고, 끝나면 ``done`` 을 부릅니다.

        ``done(None)`` 이 성공이고, 문구가 오면 실패입니다. 팝업이 그것을
        보고 닫을지 다시 치게 할지 정합니다.
        """

        def work() -> None:
            try:
                client = self._ensure_client()
                do_login(client, member_no, password)
            except (KorailApiError, ValueError) as exc:
                # 문구를 지금 붙잡습니다. except 블록을 벗어나면 파이썬이
                # 예외 이름을 지우므로, 나중에 도는 람다 안에서는 못 읽습니다.
                message = str(exc)
                self.events.put(lambda: self._login_failed(message, done))
                return
            self._credentials = (member_no, password)
            self.events.put(lambda: self._login_succeeded(done))

        self._in_thread(work, "korail-login")

    def _set_login_state(self, text: str, colour: str) -> None:
        self.login_state.set(text)
        self.login_label.configure(foreground=colour)

    def _login_succeeded(self, done: Callable[[str | None], None] | None = None) -> None:
        self.logged_in = True
        who = self.login_id.get().strip()
        self._set_login_state(f"로그인됨 ({who})" if who else "로그인됨", LOGIN_OK_COLOUR)
        self._write_log("로그인했습니다.", "good")
        self.settings = replace(self.settings, login_id=who)
        settings_module.save(self.settings)
        self.sync_login_buttons()
        # 다른 계정으로 로그인한 것일 수 있습니다 — 이전 계정/이전 조회의
        # 목록이 남아 있으면 남의 예약을 내 것으로 착각합니다. open_login()
        # 이 감시가 도는 중에는 이 팝업 자체를 막으므로, 여기 다다랐다는
        # 것은 곧 any_running() 이 이미 거짓이라는 뜻입니다 — 그래도 한 번
        # 더 확인합니다(방어적으로).
        if not self.any_running():
            self._reset_session_lists()
            # 로그인할 때마다(껐다 켜거나, 재로그인하거나) 서버에 지금
            # 살아 있는 예약을 다시 불러옵니다 — 이 프로그램을 새로
            # 켜면 방금 비운 잡은 예약 목록은 이 세션이 만든 적이 없으니
            # 비어 있고, 서버에는 예약이 그대로 있을 수 있습니다.
            self._load_reservations_from_server(announce=False)
        if done is not None:
            done(None)

    def _login_failed(
        self,
        message: str,
        done: Callable[[str | None], None] | None = None,
    ) -> None:
        self.logged_in = False
        self._set_login_state("로그인 실패", LOGIN_BAD_COLOUR)
        self._write_log(f"로그인 실패: {message}", "bad")
        self.sync_login_buttons()
        if done is not None:
            done(message)
        else:
            messagebox.showerror("로그인 실패", message)

    def _reset_session_lists(self) -> None:
        """조회 결과·예매 대상·잡은 예약을 모두 비웁니다.

        로그아웃, 다른 아이디로 로그인, 비로그인 전환 — 이 셋 뒤에 부릅니다.
        계정이 바뀌거나 사라지는 순간인데 이전 목록이 화면에 남아 있으면,
        남의(또는 지난) 조회·예매 대상·PNR 을 지금 계정의 것으로 착각하게
        됩니다. **감시가 도는 중에는 부르지 않습니다** — 그 목록을 감시가
        쓰고 있습니다. 호출부가 모두 :meth:`any_running` 을 먼저 확인합니다.

        잡은 예약 중 결제 기한이 남은 것이 있어도 여기서는 묻지 않고 그냥
        비웁니다 — :meth:`clear_holds` 와 달리, 이 호출은 "계정이 바뀌었다"
        는 사실 자체가 이미 그 목록이 이제 이 계정의 것이 아니라는 뜻이기
        때문입니다. 서버의 예약은 그대로 남고, PNR 은 코레일 앱에서 여전히
        볼 수 있습니다.
        """
        had_anything = bool(self.results or self.targets or self.holds)
        for tree in (self.tree, self.return_tree):
            tree.delete(*tree.get_children())
        self.results = []
        self.journeys = []
        self.item_journeys.clear()
        self._group_children.clear()
        self._leg_items.clear()
        self.results_status.set(
            "계정이 바뀌어 목록을 비웠습니다. 조건을 정하고 [조회] 를 누르세요."
        )
        self.results_label.configure(foreground="")
        self.targets.clear()
        self.sync_target_list()
        self.holds.clear()
        self.sync_holds()
        if had_anything:
            self._write_log(
                "계정이 바뀌어 조회 결과·예매 대상·잡은 예약 목록을 모두 "
                "비웠습니다 (서버의 예약은 그대로입니다)."
            )

    # -- 동작: 서버에서 잡은 예약 불러오기 ------------------------------------

    def on_load_reservations(self) -> None:
        """[서버에서 불러오기] 단추 — 사람이 눌러서 새로고침합니다."""
        if not self.logged_in:
            messagebox.showwarning("잡은 예약 불러오기", "먼저 로그인하세요")
            return
        self._load_reservations_from_server(announce=True)

    def _load_reservations_from_server(self, *, announce: bool) -> None:
        """코레일 서버에 지금 살아 있는 예약을 물어 목록에 채웁니다.

        :meth:`~korail_mobile_api.client.KorailClient.get_reservation_history`
        는 순수 조회(consent 없음)라 로그인만 하면 부를 수 있습니다 — 이
        프로그램이 만든 적 없는 예약(전에 켰을 때 잡았거나, 코레일 앱에서
        직접 잡은 것)도 여기서 알 수 있습니다.

        **이 조회는 결제 기한도, 좌석 등급도, "예약대기인지 결제 완료인지"
        도 주지 않습니다** — 그 칸들을 지어내지 않고 "코레일 앱에서
        확인하세요" 라고 적습니다. 취소도 이 목록에서 불러온 줄에는 걸 수
        없습니다 — 코레일 취소 폼은 **이 세션에서 방금 예약해서 받은
        응답 객체**만 받아들이는데(:func:`build_unpaid_reservation_cancel_form`),
        이 조회가 돌려주는 것은 다른 모양의 값이기 때문입니다. 그 값을
        억지로 짜맞춰 취소 요청을 만들면, 실서버에서 확인된 적 없는 채로
        되돌릴 수 없는 취소를 보내게 됩니다 — 하지 않습니다.
        """
        self.load_reservations_button.configure(state="disabled")

        def work() -> None:
            try:
                client = self._ensure_client()
                response = client.get_reservation_history()
            except KorailApiError as exc:
                message = f"{type(exc).__name__}: {exc}"
                self.events.put(lambda: self._load_reservations_failed(message))
                return
            self.events.put(
                lambda: self._reservations_loaded(response, announce=announce)
            )

        self._in_thread(work, "korail-reservation-history")

    def _load_reservations_failed(self, message: str) -> None:
        self.load_reservations_button.configure(state="normal")
        self._write_log(f"서버에서 예약을 불러오지 못했습니다 — {message}", "bad")
        messagebox.showerror("잡은 예약 불러오기 실패", message)

    def _reservations_loaded(
        self, response: ReservationHistoryResponse, *, announce: bool
    ) -> None:
        """불러온 예약을 목록에 채웁니다. **이 세션이 이미 아는 것은 덮지 않습니다.**

        방금 이 세션에서 [바로 예약]·자동예매로 직접 잡은 것은 취소 버튼이
        되는 더 정확한 값(``hold_response``, 결제 기한)을 이미 들고
        있습니다 — 서버 조회 결과로 갈아 끼우면 그 값을 잃습니다.
        """
        self.load_reservations_button.configure(state="normal")
        session_held = [h for h in self.holds if h.hold_response is not None]
        session_pnrs = {h.pnr.strip() for h in session_held}
        groups: dict[str, list[ReservationHistoryTrain]] = {}
        order: list[str] = []
        for train in response.trains:
            pnr = (train.pnr_no or "").strip()
            if not pnr:
                # PNR 이 없으면 이 줄이 무엇을 가리키는지 알 길이 없습니다 —
                # 지어내지 않고 뺍니다.
                continue
            if pnr not in groups:
                groups[pnr] = []
                order.append(pnr)
            groups[pnr].append(train)
        loaded = [
            self._held_from_history(pnr, groups[pnr])
            for pnr in order
            if pnr not in session_pnrs
        ]
        self.holds = session_held + loaded
        self.sync_holds()
        message = f"서버에서 예약 {len(loaded)}건을 불러왔습니다."
        if loaded:
            message += (
                " 결제 기한·좌석 등급·예약대기 여부는 이 조회로 알 수 없어 "
                "'코레일 앱에서 확인' 으로 남겨 둡니다."
            )
        self._write_log(message, "good" if loaded else "info")
        if announce:
            messagebox.showinfo("잡은 예약 불러오기", message)

    def _held_from_history(
        self, pnr: str, trains: list[ReservationHistoryTrain]
    ) -> Held:
        """서버 조회(:meth:`get_reservation_history`) 한 PNR 묶음을 화면 자료로.

        구간이 둘 이상이면 **하나의 PNR 로 함께 예약된 환승**입니다 — 이
        프로그램이 직접 조합한 것(구간마다 PNR 이 따로임, 3·4번 표의
        "이어서")과 달리, 서버가 이미 그렇게 알고 있는 조합이므로
        ``JourneySource.SERVER_TRANSFER`` 로 표시합니다(지어낸 값이
        아니라, PNR 이 하나로 같다는 사실 자체에서 나오는 값입니다).
        """
        leg_list: list[TrainSummary] = [
            TrainSummary(
                train_no=(train.train_no or "").strip() or "?",
                departure_station_name=train.departure_station,
                arrival_station_name=train.arrival_station,
                departure_time=train.departure_time,
                arrival_time=train.arrival_time,
                departure_date=train.run_date,
                train_class_code=train.train_class_code,
                train_class_name=train.train_class_name,
                raw=dict(train.raw),
            )
            for train in trains
        ]
        journey = Journey(
            legs=tuple(leg_list),
            source=(
                JourneySource.SERVER_TRANSFER
                if len(leg_list) > 1
                else JourneySource.DIRECT
            ),
        )
        first, last = leg_list[0], leg_list[-1]
        return Held(
            label="",
            summary=journey.summary(),
            pnr=pnr,
            fare="알 수 없음(코레일 앱에서 확인)",
            deadline=None,
            deadline_text="서버가 이 목록에서 결제 기한을 주지 않습니다 — 코레일 앱에서 확인하세요",
            kind="불러온 예약",
            direction=(
                first.departure_station_name or "",
                last.arrival_station_name or "",
                first.departure_date or "",
            ),
            hold_response=None,
            held_journey=journey,
        )

    def on_logout(self) -> None:
        """세션을 버립니다. 자동예매가 도는 중이면 먼저 막습니다.

        서버에 로그아웃을 보내지 않습니다 — 이 프로그램은 읽기와 예약 말고는
        아무것도 부르지 않고, 세션을 버리면 이 프로그램은 더 못 씁니다.
        코레일 앱의 로그인까지 끊는다고 약속하지 않습니다.
        """
        if self.any_running():
            messagebox.showwarning(
                "로그아웃", "자동예매가 돌고 있습니다. [중지] 를 먼저 누르세요"
            )
            return
        self.client = None
        self.logged_in = False
        self._credentials = None
        self._set_login_state("로그아웃했습니다 — 조회만 됩니다", LOGIN_OFF_COLOUR)
        self.sync_login_buttons()
        self._reset_session_lists()
        self._write_log(
            "로그아웃했습니다. 이 프로그램의 세션만 버립니다 — 코레일 앱은 "
            "그대로입니다."
        )

    def relogin(self) -> None:
        """자동예매 도중 세션이 끊겼을 때. 자격증명은 메모리에만 있습니다."""
        if self._credentials is None or self.client is None:
            raise RuntimeError("다시 로그인할 자격증명이 없습니다")
        do_login(self.client, *self._credentials)

    # -- 동작: 역 목록 -------------------------------------------------------

    def on_load_stations(self) -> None:
        def work() -> None:
            try:
                client = self._ensure_client()
                stations = client.get_station_data().stations
            except KorailApiError as exc:
                # 작업 스레드입니다. Tk 위젯은 큐를 거쳐서만 건드립니다.
                self.log(f"역 목록을 불러오지 못했습니다: {exc}")
                self.events.put(lambda: self.station_state.set("역 목록 없음"))
                return
            names = sorted({station.name for station in stations if station.name})
            self.events.put(lambda: self._fill_stations(names))

        self._in_thread(work, "korail-stations")

    def _fill_stations(self, names: list[str]) -> None:
        self.station_names = tuple(names)
        for box in (self.departure_box, self.arrival_box, self.transfer_entry):
            box.set_completions(names)
        self.station_state.set(f"역 {len(names)}곳")
        self._write_log(f"역 {len(names)}개를 불러왔습니다. 칸에 치면 좁혀집니다.")

    # -- 동작: 조회 ----------------------------------------------------------

    def build_request(self) -> SearchRequest:
        """화면의 값을 조회 조건으로. 잘못된 입력은 여기서 걸립니다."""
        departure = self.departure.get().strip()
        arrival = self.arrival.get().strip()
        if not departure or not arrival:
            raise ValueError("출발역과 도착역을 입력하세요")
        if departure == arrival:
            raise ValueError("출발역과 도착역이 같습니다")
        after = parse_clock_field(self.after_time.get(), label="시작 시각")
        before = parse_clock_field(self.before_time.get(), label="끝 시각")
        if after and before and after > before:
            raise ValueError("시작 시각이 끝 시각보다 늦습니다")
        if not self.include_direct.get() and not self.include_transfer.get():
            raise ValueError("직통이나 환승 중 하나는 켜야 합니다")
        preference = dict(SEAT_CHOICES).get(self.seat_choice.get(), SeatPreference.ANY)
        stations = self.selected_transfer_stations()
        if (
            self.include_transfer.get()
            and self.transfer_mode.get() == TRANSFER_CUSTOM
            and not stations
        ):
            raise ValueError(
                "환승역을 직접 지정하려면 목록에서 역을 고르세요. "
                "[이 구간의 환승역 불러오기] 를 먼저 누르면 됩니다."
            )
        passengers = KorailPassengerCounts(
            **{
                key: parse_int_field(var.get(), label=key)
                for key, var in self.passenger_vars.items()
            }
        )
        return SearchRequest(
            departure=departure,
            arrival=arrival,
            date=parse_date_field(self.date.get()),
            depart_after=after,
            depart_before=before,
            train_names=self.selected_train_kinds(),
            seat_preference=preference,
            include_direct=self.include_direct.get(),
            include_transfer=self.include_transfer.get(),
            transfer_mode=self.transfer_mode.get(),
            transfer_stations=stations,
            min_transfer_minutes=parse_int_field(
                self.min_transfer.get(), label="최소 환승시간"
            ),
            max_transfer_minutes=parse_int_field(
                self.max_transfer.get(), label="최대 환승시간"
            ),
            passengers=passengers,
        )

    def build_return_request(self, outbound: SearchRequest) -> SearchRequest:
        """오는 편 조회 조건. 날짜와 시간대는 오는 편 줄의 것을 씁니다."""
        after = parse_clock_field(
            self.return_after_time.get(), label="오는 편 시작 시각"
        )
        before = parse_clock_field(
            self.return_before_time.get(), label="오는 편 끝 시각"
        )
        # 가는 편에서는 분명한 오류인 것이, 오는 편에서는 조용히 0편이 됐습니다.
        if after and before and after > before:
            raise ValueError("오는 편 시작 시각이 끝 시각보다 늦습니다")
        self._last_return_after = after
        self._last_return_before = before
        return return_request(
            outbound, date=parse_date_field(self.return_date.get()),
            depart_after=after, depart_before=before,
        )

    def on_search(self) -> None:
        # Enter 는 단추가 잠겨 있어도 그대로 듭니다. 막지 않으면 조회가 겹쳐
        # 돌고, 먼저 시작한 쪽이 늦게 끝나면 **옛 결과가 새 결과를 덮습니다** —
        # 표는 지금 조건의 것이라고 초록으로 말하면서.
        if self._search_token is not None:
            self._write_log("이미 조회 중입니다. 끝나거나 [조회 중지] 를 누른 뒤에 다시 하세요.")
            return
        try:
            request = self.build_request()
        except (ValueError, TypeError) as exc:
            messagebox.showwarning("조회 조건", str(exc))
            return
        self.search_button.configure(state="disabled")
        self._searching(True)
        self.results_status.set("조회 중…")
        self.results_label.configure(foreground="#1f6feb")
        self.log(f"조회: {request.departure}→{request.arrival} {request.date}")
        self._note_if_today(request)

        legs: list[tuple[str, SearchRequest]] = [
            ("가는 편" if self.round_trip.get() else "", request)
        ]
        if self.round_trip.get():
            try:
                legs.append(("오는 편", self.build_return_request(request)))
            except ValueError as exc:
                messagebox.showwarning("조회 조건", str(exc))
                self.search_button.configure(state="normal")
                self._searching(False)
                return

        # **오는 편까지 다 읽은 뒤에** 저장합니다. 앞서 저장하면 오는 편
        # 시간대가 늘 한 번 전 조회의 값으로 남았습니다(build_return_request 가
        # 그 값을 여기서야 채우기 때문입니다).
        self._remember(request)
        self._search_serial += 1
        token = self._search_serial
        self._search_token = token

        def cancelled() -> bool:
            return token in self._search_cancelled

        def work() -> None:
            found: list[Target] = []
            try:
                client = self._ensure_client()
                for label, leg in legs:
                    # 방향 사이에서 한 번 봅니다. 나가 있는 요청은 못 끊지만
                    # 다음 것을 묻지 않는 것만으로도 대부분 금방 끝납니다.
                    if cancelled():
                        return
                    if label:
                        self.log(f"── {label}: {leg.departure}→{leg.arrival} {leg.date}")
                    self._refresh_transfer_stations(client, leg)
                    found.extend(
                        Target(journey=journey, request=leg, label=label)
                        for journey in search_journeys(client, leg, log=self.log)
                    )
            except (KorailApiError, ValueError) as exc:
                message = str(exc)
                if not cancelled():
                    self.events.put(lambda: self._search_failed(message))
                return
            if cancelled():
                # 중지한 조회의 결과는 올리지 않습니다. 지금 조건의 것이
                # 아닌 표가 남으면 그것이 더 나쁩니다.
                return
            self.events.put(lambda: self._show_journeys(found))

        self._in_thread(work, "korail-search")

    def _note_if_today(self, request: SearchRequest) -> None:
        """오늘 조회면 서버가 지금 이후 열차만 준다는 것을 적어 둡니다.

        시작 시각을 아침으로 두고 오후에 조회하면 "왜 이 열차가 없지" 가
        됩니다. 이미 떠난 열차는 서버가 주지 않습니다.
        """
        # 날짜와 시각을 한 번에 읽습니다. 두 번 부르면 자정 언저리에서 어제
        # 날짜와 오늘 시각을 짝지어 엉뚱한 안내가 나갑니다.
        moment = datetime.now()
        if request.date != moment.strftime("%Y%m%d"):
            return
        now = moment.strftime("%H%M%S")
        started = request.depart_after or "000000"
        if started >= now:
            return
        self.log(
            f"오늘 조회입니다 — 이미 떠난 열차는 서버가 주지 않습니다. "
            f"지금은 {now[:2]}:{now[2:4]} 이고 시작 시각은 "
            f"{started[:2]}:{started[2:4]} 입니다."
        )

    def _refresh_transfer_stations(
        self,
        client: KorailClient,
        request: SearchRequest,
    ) -> None:
        """조회할 때 환승역 목록도 그 구간 것으로 맞춰 둡니다.

        구간이 그대로면 다시 묻지 않습니다 — 같은 답을 받으려고 요청을 하나 더
        내보내는 것이라, 페이싱이 걸린 이 프로그램에서는 그냥 느려집니다.
        실패해도 조회는 그대로 진행합니다. 환승역 목록은 곁가지입니다.
        """
        route = (request.departure, request.arrival)
        if route == self._transfer_route:
            return
        try:
            names = transfer_station_candidates(client, *route)
        except (KorailApiError, ValueError) as exc:
            self.log(f"환승역 목록을 갱신하지 못했습니다: {exc}")
            return
        self._transfer_route = route
        self.events.put(lambda: self._server_candidates_loaded(route, names))

    def _search_failed(self, message: str) -> None:
        self.search_button.configure(state="normal")
        self._searching(False)
        self._search_token = None
        self.results_status.set("조회에 실패했습니다. 기록을 확인하세요.")
        self.results_label.configure(foreground="#b42318")
        self._write_log(f"조회 실패: {message}", "bad")
        messagebox.showerror("조회 실패", message)

    def _show_journeys(self, results: list[Target]) -> None:
        self.search_button.configure(state="normal")
        self._searching(False)
        self._search_token = None
        self.results = results
        self.journeys = [target.journey for target in results]
        # 조회가 왕복이었는지는 결과가 말합니다. 살아 있는 체크박스를 보면,
        # 조회가 도는 사이에 사람이 왕복을 껐을 때 오는 편 줄이 숨은 칸에
        # 갇힌 채 개수만 세어집니다.
        if any(target.label == "오는 편" for target in results):
            self.round_trip.set(True)
            # 체크만 되돌리면 오는 편 날짜·시간 칸은 잠긴 채로 남습니다 —
            # 표에는 오는 편이 있는데 그 조건은 못 고치는 화면이 됩니다.
            self._round_trip_toggled()
        self.sync_round_trip_panes()
        self.item_journeys.clear()
        self._group_children.clear()
        self._leg_items.clear()
        for tree in (self.tree, self.return_tree):
            tree.delete(*tree.get_children())
        # 1구간이 같은 직접 조합은 한 줄로 접습니다. 조합이 곱으로 늘어나면
        # 평평한 목록은 눈으로 셀 수 없습니다.
        for tree in (self.tree, self.return_tree):
            wanted = "오는 편" if tree is self.return_tree else None
            numbered = [
                (index, target)
                for index, target in enumerate(results)
                if (target.label == "오는 편") == (wanted == "오는 편")
            ]
            groups = group_by_first_leg([target.journey for _index, target in numbered])
            for _first, members in groups:
                picked = [numbered[position] for position in members]
                if len(picked) == 1:
                    index, target = picked[0]
                    self._insert_row(tree, index, target)
                else:
                    self._insert_group(tree, picked)
        journeys = self.journeys
        direct = sum(1 for journey in journeys if not journey.is_transfer)
        transfer = len(journeys) - direct
        going = sum(1 for target in results if target.label != "오는 편")
        split = (
            f" — 가는 편 {going} · 오는 편 {len(results) - going}"
            if self.round_trip.get()
            else ""
        )
        self.results_status.set(
            f"지금 조건의 결과: 열차 {len(journeys)}편 "
            f"(직통 {direct} · 환승 {transfer}){split}"
        )
        self.results_label.configure(foreground="#1a7f37" if journeys else "#b42318")
        self._write_log(f"열차 {len(journeys)}편을 찾았습니다.", "good")
        if not journeys:
            messagebox.showinfo(
                "조회 결과 없음",
                "조건에 맞는 열차가 없습니다.\n\n"
                "아래 기록 창에 어느 조건이 몇 편을 걸러 냈는지, 걸러진 열차가 "
                "무엇이었는지 찍혀 있습니다.\n\n"
                "자주 걸리는 것: 오늘 날짜로 조회하면 이미 떠난 열차는 서버가 "
                "주지 않습니다. 시간대를 넓히거나 열차 종류 선택을 지워 보세요.",
            )

    def _leg_row_values(self, journey: Journey, leg_index: int) -> tuple[str, ...]:
        """구간 정보 한 줄 — 조회 결과·예매 대상·잡은 예약이 함께 씁니다.

        열한 칸은 :meth:`_journey_row_values` 와 같은 자리입니다("구분"
        자리에는 "1구간"/"2구간" 이 옵니다). 그 구간 하나만의 좌석 상태를
        보여 줍니다 — 부모 줄은 두 구간을 합쳐 하나로 말하므로(한 구간만
        매진이어도 '매진'), 어느 쪽이 막혔는지는 여기서만 보입니다. 한
        구간짜리 여정으로 만들어 같은 계산을 그대로 씁니다 — 규칙을 두 번
        쓰지 않습니다. "환승 대기" 는 구간 하나에는 뜻이 없어 "-" 로 둡니다
        — 그 구간이 어디서 어디로 가는지는 출발역·도착역 칸이 이미 말합니다.
        """
        leg = journey.legs[leg_index]
        alone = Journey(legs=(leg,), source=journey.source)
        return (
            f"{leg_index + 1}구간",
            f"{(leg.train_class_name or '').strip()} {leg.train_no}",
            leg.departure_station_name or "-",
            format_clock(leg.departure_time),
            leg.arrival_station_name or "-",
            format_clock(leg.arrival_time),
            format_duration(journey.leg_minutes(leg_index)),
            "-",
            alone.seat_text(KorailSeatClass.GENERAL),
            alone.seat_text(KorailSeatClass.SPECIAL),
            " · ".join(alone.extras()) or "-",
        )

    def _insert_row(self, tree: ttk.Treeview, index: int, target: Target) -> None:
        journey = target.journey
        item = tree.insert(
            "",
            "end",
            values=self._row_values(target),
            tags=self._row_tags(journey),
        )
        # 항목 id 는 표마다 따로 매겨집니다. 어느 표의 것인지 함께 적어야
        # 두 표가 같은 id 로 부딪치지 않습니다.
        self.item_journeys[(str(tree), item)] = index
        if not journey.is_transfer:
            return
        for leg_index in range(len(journey.legs)):
            leg_item = tree.insert(
                item, "end", values=self._leg_row_values(journey, leg_index), tags=("leg",)
            )
            # 이 구간 줄만 따로 고르면 **이 구간 하나만의 예매 대상**을
            # 만듭니다(:meth:`_leg_only_target`) — 부모 전체를 고른 것으로
            # 되돌리지 않습니다.
            self._leg_items[(str(tree), leg_item)] = (index, leg_index)
        tree.item(item, open=True)

    def _insert_group(self, tree: ttk.Treeview, picked: list[tuple[int, Target]]) -> None:
        """1구간이 같은 조합 여럿을 한 부모 줄 아래에 접습니다.

        부모 줄은 **1구간 열차**입니다. 그 자체로는 여정이 아니므로 좌석 칸을
        비우고, 자식 줄이 저마다 하나의 여정이 됩니다. 부모를 고르면 **그
        아래 전부**를 고른 것으로 봅니다 — 자동예매는 어차피 한 방향에 한 건만
        잡으므로, 1구간이 같은 조합을 여럿 담아 두면 그만큼 먼저 열리는 것을
        잡을 기회가 늘어납니다.
        """
        _index, first = picked[0]
        leg = first.journey.first
        parent = tree.insert(
            "",
            "end",
            values=(
                f"{first.label[:2]}·환승(직접)" if first.label else "환승(직접)",
                one_line(f"{(leg.train_class_name or '').strip()} "
                         f"{(leg.train_no or '').strip().lstrip('0')}"),
                leg.departure_station_name or "-",
                format_clock(normalize_clock(leg.departure_time)),
                leg.arrival_station_name or "-",
                format_clock(normalize_clock(leg.arrival_time)),
                format_duration(first.journey.leg_minutes(0)),
                f"이어지는 편 {len(picked)}개",
                "",
                "",
                "",
            ),
            tags=("group",),
            open=True,
        )
        self._group_children[(str(tree), parent)] = [index for index, _t in picked]
        for index, target in picked:
            item = tree.insert(
                parent,
                "end",
                values=self._combination_values(target),
                tags=self._row_tags(target.journey),
            )
            self.item_journeys[(str(tree), item)] = index

    def _combination_values(self, target: Target) -> tuple[str, ...]:
        """접힌 자식 줄 — **2구간과 총 소요**만 새로 말합니다."""
        journey = target.journey
        second = journey.legs[1]
        station = journey.transfer_station_name or "환승역"
        return (
            "└ 이어서",
            one_line(f"{(second.train_class_name or '').strip()} "
                     f"{(second.train_no or '').strip().lstrip('0')}"),
            second.departure_station_name or "-",
            format_clock(normalize_clock(second.departure_time)),
            second.arrival_station_name or "-",
            format_clock(normalize_clock(second.arrival_time)),
            format_duration(journey.total_minutes),
            _transfer_text(station, journey, suffix=" 대기"),
            journey.seat_text(KorailSeatClass.GENERAL),
            journey.seat_text(KorailSeatClass.SPECIAL),
            "예매 불가" if unbookable_reason(journey) else (" · ".join(journey.extras()) or "-"),
        )

    def _row_values(self, target: Target) -> tuple[str, ...]:
        return self._journey_row_values(target.label, target.journey)

    def _journey_row_values(self, label: str, journey: Journey) -> tuple[str, ...]:
        """구분·열차·출발역·출발·도착역·도착·총 소요·환승 대기·일반실·특실·
        입석 열한 칸.

        조회 결과·예매 대상·잡은 예약, **세 표가 전부 이 함수 하나로** 이
        칸들을 채웁니다. 여정을 문장 하나로 뭉뚱그리면(예전의 "여정"
        칸), 조회 결과에서는 구분해 보던 것이 담거나 잡는 순간 사라집니다
        — 실제로 그런 신고가 있었습니다.
        """
        if journey.is_transfer:
            kind = (
                "환승"
                if journey.source is JourneySource.SERVER_TRANSFER
                else "환승(직접)"
            )
            station = journey.transfer_station_name or "환승역 다름"
            transfer = _transfer_text(station, journey)
        else:
            kind = "직통"
            transfer = "-"
        return (
            f"{label[:2]}·{kind}" if label else kind,
            # 예매 대상·기록·알림과 같은 표기를 씁니다. 한 화면에서 같은
            # 열차가 "00017" 과 "KTX 17" 로 갈리면 같은 것인지 알 수 없습니다.
            one_line(journey.train_label()),
            journey.first.departure_station_name or "-",
            format_clock(journey.departure_clock),
            journey.last.arrival_station_name or "-",
            format_clock(journey.arrival_clock),
            format_duration(journey.total_minutes),
            transfer,
            journey.seat_text(KorailSeatClass.GENERAL),
            journey.seat_text(KorailSeatClass.SPECIAL),
            "예매 불가" if unbookable_reason(journey) else (" · ".join(journey.extras()) or "-"),
        )

    def _row_tags(self, journey: Journey) -> tuple[str, ...]:
        if unbookable_reason(journey) is not None:
            return ("unbookable",)
        # 촉박한 환승이 색을 먼저 가져갑니다. 매진은 글로도 보이지만("매진"),
        # 6분 뒤 갈아탄다는 것은 숫자를 읽어야 보입니다 — 그리고 그것 때문에
        # 서버가 예약을 거절하기도 합니다(ERR911193).
        if is_tight_transfer(journey):
            return ("tight",)
        if journey.source is JourneySource.CUSTOM_TRANSFER:
            return ("custom",)
        if journey.bookable_seat_class(SeatPreference.ANY) is not None:
            return ("open",)
        general = journey.seat_state(KorailSeatClass.GENERAL)
        special = journey.seat_state(KorailSeatClass.SPECIAL)
        return ("soldout",) if general.sold_out or special.sold_out else ()

    # -- 운행 일정 ------------------------------------------------------------
    #
    # 코레일 앱 자신의 "운행 일정" 화면처럼, 열차 한 편이 하루 동안 서는
    # 정차역과 그 역의 도착·출발 시각을 보여 줍니다. 조회 결과·예매 대상·
    # 잡은 예약, 세 표 전부에서 우클릭으로 엽니다.
    #
    # 이 조회(``get_train_schedule``)는 로그인이 필요 없지만, **이
    # 저장소에서 실제 응답으로 값이 채워진 것을 확인한 적이 없습니다** —
    # 지금까지 실측에서는 전부 "EVZ000048 열차가 존재하지 않습니다" 오류만
    # 돌아왔습니다. 그래서 창에도 이 사실을 그대로 적어 둡니다.

    def _schedule_legs_for_row(
        self, tree: ttk.Treeview, item: str
    ) -> tuple[TrainSummary, ...] | None:
        """이 줄이 가리키는 구간들. 알 수 없으면 ``None`` — 지어내지 않습니다.

        이어지는 여정 전체 줄이면 구간 수만큼, 구간 하나짜리 줄(리프
        구간 줄이나 접힌 "1구간 고정" 머리)이면 그 하나만 돌려줍니다.
        표 넷이 저마다 다른 짝(:attr:`item_journeys` 등)을 쓰므로 표를
        가려 따로 찾습니다.
        """
        if tree in (self.tree, self.return_tree):
            key = (str(tree), item)
            members = self._group_children.get(key)
            if members:
                # 부모 줄은 1구간 정보만 보여 줍니다 — 그 구간 하나만 봅니다.
                return (self.results[members[0]].journey.legs[0],)
            leg_key = self._leg_items.get(key)
            if leg_key is not None:
                index, leg_index = leg_key
                return (self.results[index].journey.legs[leg_index],)
            index = self.item_journeys.get(key)
            if index is None:
                index = self.item_journeys.get((str(tree), tree.parent(item)))
            if index is None:
                return None
            return self.results[index].journey.legs

        if tree is self.target_list:
            members = self._target_group_children.get(item)
            if members:
                return (self.targets[members[0]].journey.legs[0],)
            by_item = {value: key for key, value in self._target_items.items()}
            index = by_item.get(item)
            if index is not None:
                return self.targets[index].journey.legs
            parent = tree.parent(item)
            index = by_item.get(parent)
            if index is None:
                return None
            journey = self.targets[index].journey
            siblings = tree.get_children(parent)
            if item in siblings and siblings.index(item) < len(journey.legs):
                return (journey.legs[siblings.index(item)],)
            return journey.legs

        if tree is self.hold_tree:
            members = self._hold_group_children.get(item)
            if members:
                journey = self.holds[members[0]].held_journey
                return journey.legs if journey is not None else None
            by_item = {value: key for key, value in self._hold_items.items()}
            index = by_item.get(item)
            if index is not None:
                journey = self.holds[index].held_journey
                return journey.legs if journey is not None else None
            parent = tree.parent(item)
            index = by_item.get(parent)
            if index is None:
                return None
            journey = self.holds[index].held_journey
            if journey is None:
                return None
            siblings = tree.get_children(parent)
            if item in siblings and siblings.index(item) < len(journey.legs):
                return (journey.legs[siblings.index(item)],)
            return journey.legs

        return None

    def _show_train_schedule_menu(self, event: tk.Event) -> None:
        """우클릭 메뉴 — 구간이 하나면 바로, 여럿이면 구간을 고르게 합니다."""
        tree = event.widget
        if not isinstance(tree, ttk.Treeview):
            return
        if self._on_expander(tree, event):
            return
        item = tree.identify_row(event.y)
        if not item:
            return
        tree.selection_set(item)
        legs = self._schedule_legs_for_row(tree, item)
        if not legs:
            return
        menu = tk.Menu(self.root, tearoff=0)
        if len(legs) == 1:
            leg = legs[0]
            label = one_line(f"{(leg.train_class_name or '').strip()} {leg.train_no}")
            menu.add_command(
                label=f"운행 일정 보기 ({label})",
                command=lambda leg=leg: self.open_train_schedule(leg),
            )
        else:
            for leg_index, leg in enumerate(legs):
                label = one_line(
                    f"{(leg.train_class_name or '').strip()} {leg.train_no}"
                )
                menu.add_command(
                    label=f"{leg_index + 1}구간 운행 일정 보기 ({label})",
                    command=lambda leg=leg: self.open_train_schedule(leg),
                )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def open_train_schedule(self, leg: TrainSummary) -> None:
        """이 열차 한 편이 하루 동안 서는 정차역과 도착·출발 시각을 새 창에.

        날짜나 열차번호를 모르면 조회할 수 없습니다 — 오늘 날짜 등으로
        지어내 넣지 않고 그대로 막습니다.
        """
        run_date = (leg.departure_date or "").strip()
        train_no = (leg.train_no or "").strip()
        if not run_date or not train_no:
            messagebox.showwarning(
                "운행 일정",
                "이 구간은 날짜나 열차번호를 몰라 운행 일정을 조회할 수 없습니다.",
            )
            return

        window = tk.Toplevel(self.root)
        window.title(
            f"운행 일정 — {(leg.train_class_name or '').strip()} {train_no}".strip()
        )
        window.geometry("560x420")

        header = (
            f"{run_date[:4]}-{run_date[4:6]}-{run_date[6:]}  "
            f"{leg.departure_station_name or '-'} → {leg.arrival_station_name or '-'}"
        )
        ttk.Label(window, text=header, font=("", 10, "bold")).pack(
            anchor="w", padx=10, pady=(10, 2)
        )
        # 이 조회를 실제 값 채워진 응답으로 검증한 적이 없다는 사실을
        # 화면에도 그대로 적습니다 — 확인된 것처럼 보이면 안 됩니다.
        ttk.Label(
            window,
            text="이 조회는 코레일 로그인이 필요 없지만, 실제 값이 채워진 응답을 "
            "이 프로그램에서 아직 확인하지 못했습니다. 창이 비거나 오류가 떠도 "
            "이 프로그램 탓이 아닐 수 있습니다 — 코레일 앱과 함께 확인하세요.",
            foreground="#b3261e",
            wraplength=540,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 6))

        status = tk.StringVar(value="불러오는 중…")
        ttk.Label(window, textvariable=status).pack(anchor="w", padx=10)

        columns = ("역", "도착", "출발", "지연")
        stop_tree = ttk.Treeview(window, columns=columns, show="headings", height=14)
        for name in columns:
            stop_tree.heading(name, text=name)
            stop_tree.column(name, width=110 if name == "역" else 90, anchor="center")
        stop_tree.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        def loaded(response: TrainScheduleResponse) -> None:
            if not window.winfo_exists():
                return
            if not response.stops:
                status.set("서버가 정차역을 하나도 주지 않았습니다.")
                return
            status.set(f"정차역 {len(response.stops)}개")
            for stop in response.stops:
                self._insert_schedule_stop(stop_tree, stop)

        def failed(message: str) -> None:
            if not window.winfo_exists():
                return
            status.set(f"불러오지 못했습니다: {message}")

        def work() -> None:
            try:
                client = self._ensure_client()
                response = client.get_train_schedule(run_date, train_no)
            except (KorailApiError, KorailTransportError, ValueError) as exc:
                message = str(exc)
                self.events.put(lambda: failed(message))
                return
            self.events.put(lambda: loaded(response))

        self._in_thread(work, "korail-train-schedule")

    @staticmethod
    def _insert_schedule_stop(tree: ttk.Treeview, stop: TrainScheduleStop) -> None:
        """정차역 한 줄. **실제 시각을 우선**하고, 없으면 계획 시각으로.

        ``actual_*`` 은 그 역을 이미 지났을 때만 옵니다 — 아직 안 지난
        역은 계획 시각(``planned_*``)만 있습니다. 지연은 숫자가 와야만
        적고, 없으면 "-" 로 둡니다(0 인지 안 온 것인지 구분 없이 지연
        없음으로 지어내지 않습니다).
        """
        arrival = stop.actual_arrival_time or stop.planned_arrival_time
        departure = stop.actual_departure_time or stop.planned_departure_time
        delay = (
            f"{stop.actual_arrival_delay_count}분"
            if stop.actual_arrival_delay_count is not None
            else "-"
        )
        tree.insert(
            "",
            "end",
            values=(
                stop.station_name or "-",
                format_clock(arrival),
                format_clock(departure),
                delay,
            ),
        )

    # -- 동작: 자동예매 ------------------------------------------------------

    def selected_results(self) -> list[Target]:
        """두 표에서 고른 것들. 구간 행을 골랐으면 그 구간 하나만의 여정을,
        묶음 머리를 골랐으면 그 아래 후보 전부를 씁니다."""
        direct, groups = self._selected_results_with_groups()
        return direct + [target for candidates in groups for target in candidates]

    def _leg_only_target(self, parent_index: int, leg_index: int) -> Target:
        """1구간/2구간 정보 줄만 따로 골랐을 때 — **그 구간 하나만**의 예매 대상.

        구간마다 독자적인 :class:`SearchRequest` 를 새로 만듭니다(그 구간
        자신의 역·날짜로) — 부모 여정의 것을 그대로 쓰면
        :attr:`Target.direction` 이 여전히 **전체 여정**(첫 역→마지막 역)을
        가리켜, "이 방향은 이미 잡았다" 판정이 엉뚱한 방향을 막거나(이
        구간만 잡았는데 전체가 막힌 것으로) 못 막습니다(전체를 잡았는데
        이 구간은 안 막힌 것으로). 이 구간은 그 자체로 평범한 직통
        예약이므로, 환승 조건은 켜 둘 이유가 없습니다.
        """
        parent = self.results[parent_index]
        leg = parent.journey.legs[leg_index]
        request = replace(
            parent.request,
            departure=leg.departure_station_name,
            arrival=leg.arrival_station_name,
            date=leg.departure_date,
            include_direct=True,
            include_transfer=False,
            transfer_stations=(),
        )
        journey = Journey(legs=(leg,), source=parent.journey.source)
        return Target(journey=journey, request=request, label=parent.label)

    def _selected_results_with_groups(
        self,
    ) -> tuple[list[Target], list[list[Target]]]:
        """바로 담을 것과, **묶음 머리를 골라서 나온 후보 목록**을 나눕니다.

        묶음 머리는 여정이 아니라 1구간입니다. 그것을 곧장 담으면 이어질
        수 있는 2구간이 전부(고른 적 없는 것까지) 예매 대상에 들어갑니다 —
        그래서 여기서는 담지 않고 후보만 돌려주고, 부르는 쪽
        (:meth:`add_targets`)이 팝업으로 하나를 고르게 합니다. 구간 정보
        줄(1구간/2구간)만 따로 골랐으면 그 구간 하나만의 예매 대상을
        만듭니다(:meth:`_leg_only_target`) — 부모 전체로 되돌리지 않습니다.
        """
        direct: list[Target] = []
        groups: list[list[Target]] = []
        for tree in (self.tree, self.return_tree):
            for item in tree.selection():
                # 묶음의 부모를 골랐으면 그 아래 후보 전부를 돌려줍니다 —
                # 여기서는 담지 않습니다.
                members = self._group_children.get((str(tree), item), [])
                if members:
                    groups.append([self.results[index] for index in members])
                    continue
                leg_key = self._leg_items.get((str(tree), item))
                if leg_key is not None:
                    leg_target = self._leg_only_target(*leg_key)
                    if leg_target not in direct:
                        direct.append(leg_target)
                    continue
                index = self.item_journeys.get((str(tree), item))
                if index is None:
                    index = self.item_journeys.get((str(tree), tree.parent(item)))
                if index is not None and self.results[index] not in direct:
                    direct.append(self.results[index])
        return direct, groups

    @staticmethod
    def _on_expander(tree: ttk.Treeview, event: tk.Event) -> bool:
        """두 번 누른 자리가 왼쪽 **+/- 표시**인가.

        Tk 는 그 자리를 ``Treeitem.indicator`` 로 부릅니다(Xvfb 에서 확인:
        폭 40px 짜리 ``#0`` 칸에서 x<20 이 indicator, 그 뒤는 text). 이름 앞에
        스타일 이름이 붙으므로 통째로 비교하지 않고 끝만 봅니다.
        """
        return str(tree.identify_element(event.x, event.y)).endswith("indicator")

    def _result_double_clicked(self, event: tk.Event) -> str | None:
        """두 번 누르면 담습니다. **접거나 펴지는 않습니다.**

        Treeview 는 두 번 누르면 그 줄을 접었다 폈다 하는 것이 기본입니다.
        그래서 환승 여정을 담을 때마다 구간 줄이 제멋대로 접히고 펴졌습니다 —
        접고 펴는 것은 왼쪽 +/- 를 눌러서만 되어야 합니다. ``"break"`` 를
        돌려주면 그 기본 동작이 이어지지 않습니다.
        """
        widget = event.widget
        if not isinstance(widget, ttk.Treeview):
            return None
        if self._on_expander(widget, event):
            # +/- 를 누른 것입니다. 접고 펴는 일은 그대로 두고, 담지 않습니다.
            return None
        item = widget.identify_row(event.y)
        if not item:
            return None
        widget.selection_set(item)
        self.add_targets()
        return "break"

    def add_targets(self) -> None:
        """고른 것을 예매 대상에 담습니다.

        묶음 머리(1구간)를 골랐으면 아무것도 바로 담지 않고 팝업을 엽니다
        (:meth:`_offer_group_picker`) — 이어질 수 있는 2구간을 전부 담으면
        고른 적 없는 열차가 목록에 쌓인 것처럼 보입니다. 구간 정보 줄이나
        여정 줄을 직접 골랐으면 그 자리에서 바로 담습니다.
        """
        direct, groups = self._selected_results_with_groups()
        if not direct and not groups:
            messagebox.showwarning("예매 대상", "위 목록에서 열차를 고르고 [담기] 를 누르세요")
            return
        if direct:
            added = self._add_picked_targets(direct)
            self._write_log(
                f"예매 대상에 {added}편을 담았습니다 (모두 {len(self.targets)}편)."
                if added
                else "이미 담긴 열차입니다."
            )
        for candidates in groups:
            self._offer_group_picker(candidates)

    @staticmethod
    def _checked_classes(
        general: tk.BooleanVar, special: tk.BooleanVar
    ) -> frozenset[KorailSeatClass]:
        classes: set[KorailSeatClass] = set()
        if general.get():
            classes.add(KorailSeatClass.GENERAL)
        if special.get():
            classes.add(KorailSeatClass.SPECIAL)
        return frozenset(classes)

    def _seat_choices_for(
        self, journey: Journey
    ) -> tuple[frozenset[KorailSeatClass], ...] | None:
        """지금 좌석 체크박스로 이 여정에 담을 선택을 만듭니다.

        전부 체크된 기본값(무관)이면 ``None`` 을 돌려줍니다 — 그러면
        :class:`~korail_booker.autobook.AutoBooker` 가 옛 방식대로 묶음
        공통 설정(좌석 콤보박스)을 그대로 따릅니다. 하나라도 뗀 구간이
        있으면 그 여정 전용 선택으로 굳힙니다.

        어느 구간이든 둘 다 떼면(받아들일 등급이 하나도 없으면)
        ``ValueError`` 를 냅니다 — 그런 대상은 영원히 못 잡습니다.
        """
        if journey.is_transfer and journey.source is JourneySource.CUSTOM_TRANSFER:
            leg1 = self._checked_classes(self.pick_leg1_general, self.pick_leg1_special)
            leg2 = self._checked_classes(self.pick_leg2_general, self.pick_leg2_special)
            for number, classes in ((1, leg1), (2, leg2)):
                if not classes:
                    raise ValueError(f"{number}구간에 좌석 등급을 하나도 안 골랐습니다")
            if leg1 == _BOTH_SEAT_CLASSES and leg2 == _BOTH_SEAT_CLASSES:
                return None
            return (leg1, leg2)
        classes = self._checked_classes(self.pick_general, self.pick_special)
        if not classes:
            raise ValueError("좌석 등급을 하나도 안 골랐습니다")
        if classes == _BOTH_SEAT_CLASSES:
            return None
        return (classes,) * len(journey.legs)

    def _add_picked_targets(self, picked: list[Target]) -> int:
        """고른 여정들을 예매 대상에 담습니다. 실제로 들어간 편 수를 돌려줍니다.

        자리가 열려도 폼이 만들어지지 않는 행은 걸러 알리고, 이미 담긴 것과
        같은 여정·방향은 조용히 건너뜁니다 — 두 번 담아도 뜻이 없습니다.
        지금 좌석 체크박스 상태(:meth:`_seat_choices_for`)를 함께 굽습니다.
        """
        added = 0
        for target in picked:
            # 자리가 열려도 폼이 만들어지지 않는 행이 있습니다. 새벽에 자리가
            # 났을 때 알게 되는 것보다 지금 아는 편이 낫습니다.
            reason = unbookable_reason(target.journey)
            if reason is not None:
                detail = unbookable_detail(target.journey) or reason
                self._write_log(f"담지 못했습니다 — {target.describe()}: {detail}", "warn")
                messagebox.showwarning(
                    "예매 대상",
                    f"{target.journey.summary()}\n\n"
                    "이 열차는 조회 결과에 예약 폼이 요구하는 값이 빠져 있어 "
                    "예매를 걸 수 없습니다. 왜 그렇게 오는지는 확인되지 "
                    "않았습니다 — 이 프로그램이 아는 것은 받은 값이 이렇다는 "
                    "것뿐입니다.\n\n"
                    f"{detail}",
                )
                continue
            try:
                choices = self._seat_choices_for(target.journey)
            except ValueError as exc:
                self._write_log(f"담지 못했습니다 — {target.describe()}: {exc}", "warn")
                messagebox.showwarning(
                    "예매 대상", f"{exc}\n\n[담을 등급] 체크박스를 확인하세요."
                )
                continue
            target = replace(target, seat_choices=choices)
            if any(
                existing.journey.key() == target.journey.key()
                and existing.direction == target.direction
                for existing in self.targets
            ):
                continue
            self.targets.append(target)
            added += 1
        self.sync_target_list()
        return added

    def _offer_group_picker(self, candidates: list[Target]) -> None:
        """'이어서' 후보 중 하나를 고르는 팝업.

        묶음의 머리 줄은 여정이 아니라 1구간입니다. 거기서 곧장 담으면
        이어질 수 있는 2구간이 전부(고른 적 없는 것까지) 예매 대상에
        들어갑니다. **이 함수는 그 자리에서 아무것도 담지 않습니다** — 팝업
        안에서 하나를 고를 때만 1구간과 함께 담깁니다.

        후보는 문장 한 줄이 아니라 **조회 결과·예매 대상·잡은 예약과 같은
        표**(:meth:`_configure_journey_columns`)로 보여 줍니다 — 이 팝업만
        구분·열차·시각·좌석이 한 줄로 뭉개져 있으면, 정작 고를 때는 다른
        세 표보다 못한 정보로 골라야 합니다. 후보 줄 아래에는 1구간/2구간
        정보 줄도 그대로 펼칩니다.
        """
        if not candidates:
            return
        leg = candidates[0].journey.first
        name = one_line(
            f"{(leg.train_class_name or '').strip()} "
            f"{(leg.train_no or '').strip().lstrip('0')}"
        ).strip()
        window = tk.Toplevel(self.root)
        window.title("이어지는 구간 고르기")
        window.transient(self.root)
        ttk.Label(
            window,
            text=(
                f"1구간 {name} "
                f"{format_clock(normalize_clock(leg.departure_time))}-"
                f"{format_clock(normalize_clock(leg.arrival_time))} "
                f"({leg.departure_station_name}→{leg.arrival_station_name}) 은 "
                "정해졌습니다.\n"
                f"이어지는 2구간 {len(candidates)}개 중 하나를 고르세요 — 고르면 "
                "1구간과 함께 예매 대상에 담깁니다."
            ),
            justify="left",
            wraplength=420,
        ).pack(anchor="w", padx=12, pady=(12, 6))

        tree_frame = ttk.Frame(window)
        tree_frame.pack(padx=12, pady=(0, 8), fill="both", expand=True)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            tree_frame,
            columns=tuple(TREE_COLUMNS),
            show="tree headings",
            selectmode="browse",
            height=min(8, len(candidates) * 2),
        )
        self._configure_journey_columns(tree, indicator_width=20)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)

        item_targets: dict[str, Target] = {}
        for candidate in candidates:
            item = tree.insert(
                "",
                "end",
                values=self._journey_row_values(candidate.label, candidate.journey),
                tags=self._row_tags(candidate.journey),
            )
            item_targets[item] = candidate
            for leg_index in range(len(candidate.journey.legs)):
                tree.insert(
                    item, "end",
                    values=self._leg_row_values(candidate.journey, leg_index),
                    tags=("leg",),
                )
            tree.item(item, open=True)
        first_item = tree.get_children()
        if first_item:
            tree.selection_set(first_item[0])
            tree.focus(first_item[0])

        def resolve_selected() -> Target | None:
            selection = tree.selection()
            if not selection:
                return None
            item = selection[0]
            return item_targets.get(item) or item_targets.get(tree.parent(item))

        def pick() -> None:
            chosen = resolve_selected()
            if chosen is None:
                messagebox.showinfo("이어지는 구간 고르기", "목록에서 하나를 고르세요")
                return
            added = self._add_picked_targets([chosen])
            window.destroy()
            if added:
                messagebox.showinfo(
                    "예매 대상에 담았습니다",
                    f"1구간과 함께 담겼습니다:\n{chosen.journey.summary()}",
                )
            else:
                self._write_log("이미 담긴 열차입니다.")

        def on_double_click(event: tk.Event) -> str | None:
            if self._on_expander(tree, event):
                # +/- 를 누른 것입니다. 접고 펴는 일은 그대로 두고, 담지 않습니다.
                return None
            pick()
            return "break"

        tree.bind("<Double-Button-1>", on_double_click)

        buttons = ttk.Frame(window)
        buttons.pack(pady=(0, 12))
        ttk.Button(buttons, text="고른 것 담기", command=pick).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="취소", width=8, command=window.destroy).pack(side="left")
        window.grab_set()

    def _target_double_clicked(self, event: tk.Event) -> str | None:
        """두 번 누르면 뺍니다. **접거나 펴지는 않습니다** — 조회 결과와 같은
        이유입니다: 이 표도 이제 1구간이 같은 후보를 묶어 접습니다. 묶음의
        부모 줄을 두 번 누르면 그 아래 후보 전부가 함께 빠집니다
        (:meth:`selected_indices` 가 부모 선택을 자식들로 풀어 줍니다).
        """
        widget = self.target_list
        if self._on_expander(widget, event):
            # +/- 를 누른 것입니다. 접고 펴는 일은 그대로 두고, 빼지 않습니다.
            return None
        item = widget.identify_row(event.y)
        if not item:
            return None
        widget.selection_set(item)
        self.remove_targets()
        return "break"

    def remove_targets(self) -> None:
        """고른 것을 뺍니다. **감시 중인 것은 빼지 않습니다.**

        빼도 그 묶음은 계속 그 열차를 노립니다 — 목록에서 사라졌는데 예약이
        잡히면 무슨 일인지 알 수 없습니다. 감시 중이 아닌 것은 그대로
        빼고, 감시 중인 것만 남겨 두고 왜 안 뺐는지 말합니다 — 골랐다고
        아무것도 안 빼면 나머지도 안 빠진 것으로 오해하기 쉽습니다.
        """
        watching = self.watching_keys()
        chosen = sorted(self.selected_indices(), reverse=True)
        if not chosen:
            messagebox.showinfo("예매 대상", "뺄 열차를 고르세요")
            return
        busy = [
            self.targets[index]
            for index in chosen
            if self.targets[index].journey.key() in watching
        ]
        removable = [
            index for index in chosen if self.targets[index].journey.key() not in watching
        ]
        for index in removable:
            del self.targets[index]
        if removable:
            self.sync_target_list()
        if busy:
            messagebox.showwarning(
                "예매 대상",
                f"감시 중인 열차 {len(busy)}편은 빼지 않았습니다. [고른 것만 중지] 를 "
                "먼저 누르세요.\n\n" + "\n".join(f"· {t.describe()}" for t in busy),
            )
        if removable:
            self._write_log(f"예매 대상 {len(self.targets)}편 남았습니다.")

    def clear_targets(self) -> None:
        """예매 대상을 비웁니다. **감시 중인 것은 남깁니다** — :meth:`remove_targets`
        와 같은 이유입니다. 감시와 무관한 것들까지 [전체 중지] 없이는 못
        비우게 막을 이유가 없습니다."""
        watching = self.watching_keys()
        kept = [t for t in self.targets if t.journey.key() in watching]
        removed = len(self.targets) - len(kept)
        if not removed:
            if kept:
                messagebox.showwarning(
                    "예매 대상",
                    "감시 중인 열차뿐이라 비울 것이 없습니다. [전체 중지] 를 먼저 "
                    "누르세요.",
                )
            else:
                self._write_log("예매 대상을 비웠습니다.")
            return
        self.targets[:] = kept
        self.sync_target_list()
        if kept:
            self._write_log(
                f"예매 대상 {removed}편을 비웠습니다 (감시 중인 {len(kept)}편은 "
                "남았습니다)."
            )
        else:
            self._write_log("예매 대상을 비웠습니다.")

    def build_options(self) -> BookingOptions:
        interval = self.poll_interval.get().strip()
        try:
            interval_s = float(interval)
        except ValueError as exc:
            raise ValueError("조회 주기는 숫자여야 합니다") from exc
        # 'nan' 과 'inf' 는 float() 를 그냥 통과합니다. NaN 은 모든 비교를
        # 거짓으로 만들어 하한 검사(``< MIN_POLL_INTERVAL_S``)를 통째로
        # 지나가고, 그러면 쉬는 시간 없이 도는 감시가 됩니다 — IP 가 막힙니다.
        if not math.isfinite(interval_s):
            raise ValueError("조회 주기는 숫자여야 합니다")
        return BookingOptions(
            seat_preference=dict(SEAT_CHOICES).get(
                self.seat_choice.get(), SeatPreference.ANY
            ),
            poll_interval_s=interval_s,
            watch_minutes=parse_int_field(self.watch_minutes.get(), label="감시 시간"),
            allow_standby=self.allow_standby.get(),
            # 화면에 스위치가 없습니다 — 늘 진짜로 보냅니다.
            live=True,
        )

    # -- 감시 여럿 ------------------------------------------------------------

    def watching_keys(self) -> set[object]:
        """지금 돌고 있는 감시들이 보고 있는 여정 열쇠 전부."""
        keys: set[object] = set()
        for watch in self.watches:
            if watch.running:
                keys |= set(watch.keys)
        return keys

    def any_running(self) -> bool:
        return any(watch.running for watch in self.watches)

    def _next_watch_tag(self) -> str:
        """``A``, ``B``… 스물여섯을 넘으면 다시 ``A`` 로. 꼬리표는 이름표일
        뿐이고, 어느 여정인지는 :attr:`Watch.keys` 가 압니다."""
        tag = ALPHABET[self._next_tag % len(ALPHABET)]
        self._next_tag += 1
        return tag

    def _tagged_log(self, tag: str) -> Callable[[str], None]:
        """기록 줄 앞에 꼬리표를 답니다.

        곁가지 줄(공백으로 시작)은 그 성질을 지켜야 합니다 — 앞에 그냥 붙이면
        더는 곁가지로 보이지 않아 들여쓰기가 깨집니다.
        """

        def write(message: str) -> None:
            if message.startswith(" "):
                self.log_booking(f"    [{tag}] {message.strip()}")
            else:
                self.log_booking(f"[{tag}] {message}")

        return write

    def selected_targets(self) -> list[Target]:
        """고른 줄. 아무것도 안 골랐으면 담긴 것 전부로 봅니다."""
        chosen = [self.targets[index] for index in self.selected_indices()]
        return chosen or list(self.targets)

    def _busy_journeys(self) -> set[JourneyKey]:
        """지금 감시 중이거나 이미 예약이 잡힌 **정확히 같은 조합**들.

        예전에는 방향(출발역·도착역·날짜)으로 막았습니다. 그런데 1구간이
        같고 2구간만 다른 서로 다른 열차 조합("KTX 341+343" 과
        "KTX 341+53" 처럼)까지 같은 방향이라는 이유만으로 "중복" 이라고
        막혔습니다 — 실제로 다른 열차인데 중복이 아니라는 신고가 있었습니다.
        이 프로그램은 잡기만 하고 결제하지 않으므로, 서로 다른 조합을
        나란히 잡아 두는 것 자체를 막을 이유가 없습니다. **정확히 같은
        조합**(``Journey.key()``)을 또 잡으려는 것만 막습니다.

        (자동예매 엔진 안의 "한 묶음에는 방향마다 한 건만" 규칙은 그대로
        입니다 — 그건 한 묶음에 담아 둔 이어서 후보 여럿 중 먼저 열리는
        것 하나만 잡히게 하는 별개의 장치이고, 여기서 건드리지 않습니다.
        여기는 **서로 다른 묶음·[바로 예약]끼리** 겹치는 것만 봅니다.)
        """
        busy: set[JourneyKey] = set()
        for watch in self.watches:
            # 멈춘 묶음은 풀어 줍니다. 안 그러면 한 번 돌린 조합을 다시는
            # 못 노립니다.
            if watch.running:
                busy |= watch.keys
        # 잡아 둔 예약은 **그 예약이 가리키는 여정**으로 셉니다. 없으면
        # (옛 기록 등) 판단할 근거가 없으므로 막지 않습니다 — 지어내지
        # 않습니다.
        #
        # **기한이 지난 예약은 뺍니다.** 이 프로그램의 기록에도 적혀 있듯
        # 기한이 지나면 코레일이 그 홀드를 스스로 취소합니다 — 더는 그
        # 조합을 막고 있지 않은데 화면만 막아 두면, 같은 조합을 다시
        # 노리고 싶어도 "이미 예약이 있다" 는 잘못된 이유로 계속 막힙니다.
        now = now_kst()
        busy |= {
            held.held_journey.key()
            for held in self.holds
            if held.held_journey is not None and not is_expired(held.deadline, now)
        }
        # 아직 답을 못 받은 [바로 예약]도 셉니다. 요청이 나가 있는 몇 초
        # 사이에 감시를 걸면, 같은 열차를 두 번 잡습니다.
        busy |= self._reserving_keys
        return busy

    def _watch_of(self, target: Target) -> Watch | None:
        """이 열차를 지금 보고 있는 묶음. 없으면 ``None``."""
        key = target.journey.key()
        for watch in self.watches:
            if watch.running and key in watch.keys:
                return watch
        return None

    def sync_target_list(self) -> None:
        """표를 다시 그립니다 — 상태·주기·남은 감시 시간까지.

        1구간이 같은 직접 조합은 조회 결과와 같은 모양으로 접습니다 — 2구간만
        다른 후보 여럿을 평평하게 늘어놓으면, 조회 때는 한 줄이던 것이 여기서만
        여러 줄로 흩어져 같은 묶음인지 알아보기 어려워집니다.
        """
        # **줄 번호로** 기억합니다. 항목 id 는 다시 그릴 때마다 새로 매겨지므로,
        # 지우기 전의 id 를 지운 뒤의 목록에서 찾으면 하나도 맞지 않습니다 —
        # 그래서 다시 그릴 때마다 선택이 통째로 사라졌고, [고른 것만 시작]·
        # [고른 것만 중지]·[조건 바꿔 재시작] 이 조용히 '전부' 가 됐습니다.
        # **어느 열차였는지**로 기억합니다. 줄 번호로 되돌리면 [빼기] 로 앞줄이
        # 사라진 뒤 그 번호에 온 다른 열차가 골라집니다 — 고른 적 없는 열차에
        # [고른 것만 시작] 이 걸립니다.
        chosen = {
            (target.journey.key(), target.label) for target in self.selected_targets()
        }
        self.target_list.delete(*self.target_list.get_children())
        self._target_items = {}
        self._target_group_children = {}

        order: list[tuple[object, ...]] = []
        buckets: dict[tuple[object, ...], list[int]] = {}
        for index, target in enumerate(self.targets):
            journey = target.journey
            # 직통·서버 추천 환승은 묶지 않습니다 — 조회 결과를 묶을 때와
            # 같은 이유입니다(값어치가 있는 것은 경우의 수가 곱으로 늘어나는
            # 직접 조합뿐입니다). 라벨(가는 편/오는 편)이 다르면 다른 묶음입니다.
            key: tuple[object, ...] = (
                ("solo", index)
                if journey.source is not JourneySource.CUSTOM_TRANSFER
                else ("group", target.label, *first_leg_key(journey))
            )
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(index)

        for key in order:
            indices = buckets[key]
            if len(indices) == 1:
                self._insert_target_row(indices[0], parent="")
            else:
                self._insert_target_group(indices)

        for index, target in enumerate(self.targets):
            if (target.journey.key(), target.label) not in chosen:
                continue
            item = self._target_items.get(index)
            if item is not None:
                self.target_list.selection_add(item)
        self.stop_button.configure(state="normal" if self.any_running() else "disabled")

    def _insert_target_row(self, index: int, *, parent: str) -> None:
        """예매 대상 한 줄. 묶음 아래(자식)든 최상위(단독)든 같은 모양입니다.

        가운데 아홉 칸은 :meth:`_row_values` 로 조회 결과와 **똑같이** 만듭니다
        — 그 함수 하나가 구분·열차·시각·소요·환승 대기·좌석을 다 압니다.
        여기서 다시 쓰면 둘이 반드시 어긋납니다(실제로 어긋났습니다: 조회
        결과는 칸이 나뉘어 있는데 예매 대상은 "여정" 한 칸으로 뭉뚱그려
        졌습니다).
        """
        target = self.targets[index]
        journey = target.journey
        watch = self._watch_of(target)
        item = self.target_list.insert(
            parent,
            "end",
            values=(
                self._target_state(target, watch),
                *self._row_values(target),
                f"{watch.options.poll_interval_s:g}초" if watch else "-",
                watch.remaining(time.monotonic()) if watch else "-",
            ),
            # 촉박한 환승은 도는지 안 도는지보다 먼저 보여야 합니다.
            tags=(
                "tight"
                if is_tight_transfer(target.journey)
                else ("watching" if watch else "idle"),
            ),
        )
        self._target_items[index] = item
        # 환승 여정이면 조회 결과와 똑같이 1구간/2구간 정보 줄을 펼칩니다
        # — +/- 로 접고 펼 수 있습니다. 상태·조회 주기·남은 감시는 이
        # 구간 하나만의 것이 아니므로 비웁니다.
        if not journey.is_transfer:
            return
        for leg_index in range(len(journey.legs)):
            self.target_list.insert(
                item,
                "end",
                values=("", *self._leg_row_values(journey, leg_index), "", ""),
                tags=("leg",),
            )
        self.target_list.item(item, open=True)

    def _insert_target_group(self, indices: list[int]) -> None:
        """1구간이 같은 예매 대상 여럿을 한 부모 줄 아래에 접습니다.

        부모 줄을 고르면(:meth:`selected_indices`) 그 아래 후보 전부를 고른
        것으로 칩니다 — [빼기]·[고른 것만 시작]·[고른 것만 중지] 가 조회
        결과의 묶음 부모와 같은 뜻으로 동작하게 하려는 것입니다.
        """
        first = self.targets[indices[0]].journey
        leg = first.first
        parent = self.target_list.insert(
            "",
            "end",
            values=(
                "1구간 고정",
                "",
                one_line(f"{(leg.train_class_name or '').strip()} "
                         f"{(leg.train_no or '').strip().lstrip('0')}"),
                leg.departure_station_name or "-",
                format_clock(normalize_clock(leg.departure_time)),
                leg.arrival_station_name or "-",
                format_clock(normalize_clock(leg.arrival_time)),
                format_duration(first.leg_minutes(0)),
                f"이어지는 후보 {len(indices)}개",
                "",
                "",
                "",
                "",
                "",
            ),
            tags=("group",),
            open=True,
        )
        self._target_group_children[parent] = list(indices)
        for index in indices:
            self._insert_target_row(index, parent=parent)

    def _target_state(self, target: Target, watch: Watch | None) -> str:
        """상태 칸. 도는지와 **어떻게 사는지**를 함께 적습니다.

        구간별로 산다는 것은 예약이 둘이 된다는 뜻입니다. 여정 칸 끝에 달아
        두면 표가 조금만 좁아도 잘려 보이지 않습니다 — 실제로 그랬습니다.
        맨 앞 칸은 잘리지 않습니다. 좌석 등급을 이 여정만 따로 골라
        두었으면(:attr:`Target.seat_choices`) 그것도 보여 줍니다 — 안
        그러면 담을 때 무엇을 체크했는지 나중에 알 길이 없습니다.
        """
        state = f"▶ 감시 중 [{watch.tag}]" if watch else "○ 대기"
        if not books_as_one_reservation(target.journey):
            state = f"{state} · 구간별"
        if target.seat_choices is not None:
            state = f"{state} · {self._seat_choice_text(target.seat_choices)}"
        return state

    @staticmethod
    def _seat_choice_text(choices: tuple[frozenset[KorailSeatClass], ...]) -> str:
        """이 여정에 고정된 좌석 선택을 한 줄로. 구간마다 다르면 구간별로 적습니다."""

        def one(classes: frozenset[KorailSeatClass]) -> str:
            names = [
                name
                for seat_class, name in (
                    (KorailSeatClass.GENERAL, "일반실"),
                    (KorailSeatClass.SPECIAL, "특실"),
                )
                if seat_class in classes
            ]
            return "·".join(names) or "선택 없음"

        if len(choices) == 1 or len(set(choices)) == 1:
            return one(choices[0])
        return " / ".join(f"{index + 1}구간 {one(c)}" for index, c in enumerate(choices))

    def selected_indices(self) -> list[int]:
        """표에서 고른 줄의 번호. Treeview 는 항목 id 로 말하므로 되짚습니다.

        **묶음의 부모 줄을 골랐으면 그 아래 후보 전부**를 고른 것으로 칩니다 —
        조회 결과에서 묶음 머리를 고르는 것과 같은 뜻입니다.

        **지금 목록에 있는 번호만** 돌려줍니다. 이 짝(``_target_items``)은 표를
        다시 그릴 때 갱신되는데, 목록이 줄어든 직후에는 아직 옛 번호를 들고
        있습니다 — 그대로 쓰면 ``self.targets[index]`` 가 범위를 벗어납니다.
        """
        by_item = {item: index for index, item in self._target_items.items()}
        chosen: set[int] = set()
        for item in self.target_list.selection():
            members = self._target_group_children.get(item)
            if members:
                chosen.update(i for i in members if i < len(self.targets))
                continue
            index = by_item.get(item)
            if index is not None and index < len(self.targets):
                chosen.add(index)
        return sorted(chosen)

    def on_start_selected(self) -> None:
        self.on_start(selected_only=True)

    def on_start(self, selected_only: bool = False) -> None:
        self._start_targets(
            self.selected_targets() if selected_only else list(self.targets)
        )

    def _start_targets(self, targets: list[Target]) -> None:
        if not targets:
            messagebox.showwarning(
                "자동예매", "먼저 [담기] 로 예매 대상에 열차를 넣으세요"
            )
            return
        # 이미 보고 있는 것을 또 걸면 같은 열차에 두 번 예약이 나갑니다.
        watching = self.watching_keys()
        targets = [t for t in targets if t.journey.key() not in watching]
        if not targets:
            messagebox.showinfo("자동예매", "고른 열차는 이미 감시 중입니다")
            return
        # **정확히 같은 조합도 막습니다.** 다른 묶음·[바로 예약]이 이미
        # 그 조합을 노리는 중이면 같은 조합에 진짜 예약이 두 번 나갑니다 —
        # 이 프로그램은 취소를 하지 않습니다. 1구간이 같고 2구간만 다른
        # 서로 다른 조합은 막지 않습니다(엔진이 **한 묶음 안에서**는 방향당
        # 하나만 잡는 규칙을 그대로 지킵니다 — 여기서 막는 것은 묶음을
        # 넘나드는 중복뿐입니다).
        busy = self._busy_journeys()
        blocked = [t for t in targets if t.journey.key() in busy]
        targets = [t for t in targets if t.journey.key() not in busy]
        if blocked:
            self._write_booking(
                f"{len(blocked)}편은 정확히 같은 조합을 이미 감시 중이거나 잡아 "
                "두어 건너뜁니다.",
                "warn",
            )
        if not targets:
            messagebox.showinfo(
                "자동예매",
                "고른 열차는 정확히 같은 조합을 이미 감시 중이거나 예약이 잡혀 "
                "있습니다.\n같은 조합은 한 건만 잡습니다 — 둘을 잡으면 하나는 "
                "중복 예약입니다.",
            )
            return
        try:
            options = self.build_options()
        except (ValueError, TypeError) as exc:
            messagebox.showwarning("자동예매", str(exc))
            return
        if not self.logged_in:
            messagebox.showwarning("자동예매", "실제 예약을 하려면 먼저 로그인하세요")
            return
        if not self._confirm_live(targets, options):
            return
        custom = [
            target
            for target in targets
            if target.journey.source is JourneySource.CUSTOM_TRANSFER
        ]
        if custom and not messagebox.askyesno(
            "확인",
            "직접 지정한 환승 조합이 들어 있습니다. 서버가 이런 조합을 받아들이는지"
            " 확인된 바 없습니다. 그래도 시도할까요?",
        ):
            return
        tag = self._next_watch_tag()
        booker = AutoBooker(
            self._ensure_client(),
            targets,
            options,
            log=self._tagged_log(tag),
            notify=self._make_notifier(),
            relogin=self.relogin if self._credentials else None,
            on_hold=self.on_hold_made,
        )
        title = " / ".join(target.describe() for target in targets[:2])
        if len(targets) > 2:
            title += f" 외 {len(targets) - 2}편"
        watch = Watch(
            tag=tag,
            session=BookingSession(booker),
            keys=frozenset(target.journey.key() for target in targets),
            directions=frozenset(target.direction for target in targets),
            title=title,
            options=options,
            # 엔진과 같은 자로 잽니다(`AutoBooker._run` 도 monotonic 입니다).
            deadline=(
                None
                if options.watch_minutes == 0
                else time.monotonic() + options.watch_minutes * 60.0
            ),
        )
        self.watches.append(watch)
        directions = len({target.direction for target in targets})
        self._write_booking(
            f"[{tag}] 시작 — {len(targets)}편 감시, 방향 {directions}개\n{title}"
        )
        watch.session.start(on_done=lambda result: self.events.put(
            lambda: self._booking_done(watch, result)
        ))
        self.sync_target_list()

    def _confirm_live(self, targets: list[Target], options: BookingOptions) -> bool:
        """시작 전 확인. **지금 조건을 그대로** 적습니다.

        예전에는 몇 초마다 얼마나 지켜보는지 말하지 않아, 확인 창을 보고도
        무엇에 동의하는지 알 수 없었습니다.
        """
        shown = targets[:5]
        lines = "\n".join(f"· {target.describe()}" for target in shown)
        if len(targets) > len(shown):
            lines += f"\n… 외 {len(targets) - len(shown)}편"
        window = (
            "끌 때까지" if options.watch_minutes == 0 else f"{options.watch_minutes}분 동안"
        )
        standby = "\n· 좌석이 안 열리면 예약대기도 시도합니다(직통·일반실)." if (
            options.allow_standby
        ) else ""
        tight = [t for t in targets if is_tight_transfer(t.journey)]
        tight_note = (
            f"\n환승 대기가 {TIGHT_TRANSFER_MINUTES}분 미만인 것이 "
            f"{len(tight)}편 있습니다. 실제로 갈아타지 못할 수 있고, 서버가 "
            "그런 조합의 예약을 거절하기도 합니다.\n"
            if tight
            else ""
        )
        return messagebox.askyesno(
            "실제 예약을 만듭니다",
            f"아래 {len(targets)}편을 {options.poll_interval_s:g}초마다 다시 "
            f"조회하며 {window} 지켜봅니다.\n"
            "자리가 열리면 방향마다 한 건씩 진짜 예약(결제 전 홀드)을 만들고 "
            f"멈춥니다.{standby}\n\n"
            f"{lines}\n"
            f"{self._split_warning(targets)}{tight_note}\n"
            "결제는 하지 않습니다. 잡은 뒤에는 기한 안에 코레일 앱에서 "
            "결제하거나 취소해야 합니다.\n\n"
            "계속할까요?",
        )

    def _telegram_config(self) -> TelegramConfig | None:
        """지금 이 순간의 텔레그램 설정. 없거나 꺼져 있으면 ``None``.

        이번만 쓰기로 한 값이 있으면 그것이 먼저입니다. 저장된 값이 있어도
        사람이 방금 넣은 쪽을 쓰겠다는 뜻이기 때문입니다.
        """
        if not self.notify_enabled.get():
            return None
        config = self._telegram_once or TelegramConfig(
            token=self.settings.telegram_token,
            chat_id=self.settings.telegram_chat_id,
        )
        return config if config.enabled else None

    def _notify_now(self, message: str) -> None:
        """알림 한 통. **부를 때마다 지금 설정을 읽습니다.**

        예전에는 감시를 시작할 때 설정을 한 번 구워 넘겼습니다. 그래서 감시를
        걸어 놓고 나서 텔레그램을 채우면 그 묶음은 끝까지 알림이 오지
        않았습니다 — 밤새 도는 프로그램에서 그것을 알아채는 길이 없습니다.

        알림이 실패해도 예약을 죽이지 않습니다. 이 함수는 예외를 밖으로
        내보내지 않습니다.
        """
        config = self._telegram_config()
        if config is None:
            return
        try:
            with TelegramNotifier(config) as notifier:
                if not notifier.send(message):
                    detail = f" ({notifier.last_error})" if notifier.last_error else ""
                    self.log_booking(f"텔레그램 전송에 실패했습니다{detail}.")
        except Exception as exc:
            self.log_booking(f"텔레그램 전송에 실패했습니다: {type(exc).__name__}")

    def _make_notifier(self) -> Callable[[str], None] | None:
        """감시에 넘길 알림 함수. **설정을 굽지 않습니다.**

        늘 :meth:`_notify_now` 를 돌려줍니다 — 설정이 지금 비어 있어도
        마찬가지입니다. 나중에 채워 넣으면 그때부터 알림이 갑니다.
        """
        if self._telegram_config() is None:
            self._write_booking(
                "텔레그램 설정이 아직 없습니다. 지금은 알림을 보내지 않지만, "
                "도는 중에 [텔레그램 설정] 을 채우면 그때부터 갑니다.",
                "warn",
            )
        return self._notify_now

    def announce_watches(self) -> None:
        """도는 묶음마다 "지금부터 이렇게 지켜본다" 를 한 통씩 보냅니다.

        텔레그램을 뒤늦게 채운 사람에게 필요한 것입니다 — 설정이 먹혔는지,
        그리고 지금 무엇이 얼마나 남았는지. 감시 시간은 **시작할 때의 값이
        아니라 남은 값**으로 말합니다.
        """
        running = [watch for watch in self.watches if watch.running]
        if not running or self._telegram_config() is None:
            return
        now = time.monotonic()
        for watch in running:
            window = watch.remaining(now)
            self._notify_now(
                f"🔔 텔레그램 알림을 켰습니다 [{watch.tag}]\n"
                f"{len(watch.keys)}편을 {watch.options.poll_interval_s:g}초마다 "
                f"다시 조회합니다. 감시 {window}.\n"
                f"{watch.title}"
            )
        self._write_booking(
            f"도는 감시 {len(running)}묶음에 텔레그램 알림을 알렸습니다."
        )

    def on_stop(self, selected_only: bool = False) -> None:
        """돌고 있는 감시를 멈춥니다. 고른 것만 멈출 수도 있습니다."""
        if selected_only:
            wanted = {t.journey.key() for t in self.selected_targets()}
            targets = [w for w in self.watches if w.running and (set(w.keys) & wanted)]
        else:
            targets = [w for w in self.watches if w.running]
        if not targets:
            messagebox.showinfo("자동예매", "멈출 감시가 없습니다")
            return
        for watch in targets:
            watch.session.stop()
            self._write_booking(
                f"[{watch.tag}] 중지를 요청했습니다. 이번 조회가 끝나면 멈춥니다."
            )

    def on_stop_selected(self) -> None:
        self.on_stop(selected_only=True)

    def restart_selected(self) -> None:
        """고른 줄을 멈추고 **지금 화면의 조건으로** 다시 겁니다.

        조건은 시작할 때 한 번 읽습니다. 도는 중에 조회 주기나 감시 시간을
        고쳐도 그 묶음은 옛 조건으로 계속 돕니다 — 화면과 실제가 어긋나는데
        화면이 그것을 말해 주지 않으면 사람이 속습니다.

        멈춤은 즉시 걸리지 않습니다(이번 조회가 끝나야 멈춥니다). 그래서
        멈춘 것을 확인한 뒤에 다시 겁니다.
        """
        picked = self.selected_targets()
        wanted = {target.journey.key() for target in picked}
        stopping = [w for w in self.watches if w.running and (set(w.keys) & wanted)]
        if not stopping:
            messagebox.showinfo("자동예매", "다시 걸 감시가 없습니다")
            return
        for watch in stopping:
            watch.session.stop()
            self._write_booking(f"[{watch.tag}] 조건을 바꿔 다시 걸려고 멈춥니다.")

        def when_stopped() -> None:
            if any(watch.running for watch in stopping):
                self.root.after(400, when_stopped)
                return
            # **아까 고른 것**으로 다시 겁니다. 여기서 선택을 다시 읽으면
            # 그 사이에 표가 다시 그려져 선택이 달라져 있을 수 있고, 그러면
            # 고르지도 않은 열차에 감시가 걸립니다.
            self._start_targets(picked)

        self.root.after(400, when_stopped)

    def _show_later(self, kind: str, title: str, body: str) -> None:
        """모달을 **큐를 비운 뒤에** 엽니다.

        :meth:`_drain` 안에서 바로 열면 모달이 제 이벤트 고리를 돌리는 동안
        다음 회차가 예약되지 않아, 사람이 [확인] 을 누를 때까지 화면 갱신이
        통째로 멈춥니다 — 도는 감시의 기록도 카운트다운도 함께 멈춥니다.
        """
        show = {"info": messagebox.showinfo, "warn": messagebox.showwarning}.get(
            kind, messagebox.showerror
        )
        self.root.after(0, lambda: show(title, body))

    def _booking_done(self, watch: Watch, result: BookingResult) -> None:
        levels = {
            Outcome.HELD: "good",
            Outcome.FAILED: "bad",
            Outcome.PREVIEW: "warn",
        }
        self._write_booking(
            f"[{watch.tag}] 종료 ({result.outcome.value}): {result.message}",
            levels.get(result.outcome, "info"),
        )
        self.sync_target_list()
        # 창은 잡았을 때와 실패했을 때만 띄웁니다. 여럿을 돌리는데 중지·시간
        # 끝마다 창이 뜨면 그것부터 치우느라 정작 볼 것을 못 봅니다.
        if result.outcome is Outcome.HELD:
            self._show_later("info", f"예약됨 [{watch.tag}]", result.message)
        elif result.outcome is Outcome.FAILED:
            self._show_later("error", f"자동예매 실패 [{watch.tag}]", result.message)

    # -- 동작: 텔레그램 ------------------------------------------------------

    def on_telegram_settings(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("텔레그램 알림 설정")
        window.transient(self.root)
        token = tk.StringVar(value=self.settings.telegram_token)
        chat_id = tk.StringVar(value=self.settings.telegram_chat_id)
        # 처음 쓰는 사람이 여기서 막힙니다. BotFather 답장만 보고는 어느 값을
        # 어디에 넣는지, 왜 [내 대화 ID 찾기] 가 빈손으로 오는지 알 수 없습니다.
        # 그래서 단계에 번호를 붙이고, 각 단계 옆에 그 단계의 단추를 둡니다.
        ttk.Label(
            window,
            text="텔레그램으로 알림을 받으려면 값이 둘 필요합니다 — 봇 토큰과 대화 ID.\n"
            "아래 순서대로 하면 둘 다 채워집니다.",
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 8))

        step1 = ttk.LabelFrame(window, text="1단계 — 봇 만들기 (텔레그램 앱에서)")
        step1.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=4)
        ttk.Label(
            step1,
            text="① 텔레그램 검색창에 @BotFather 를 치고 (파란 체크가 붙은 것) 대화를 엽니다.\n"
            "② /newbot 을 보냅니다.\n"
            "③ 봇 이름을 아무거나 보냅니다 (예: 코레일 알림).\n"
            "④ 봇 아이디를 보냅니다. 반드시 bot 으로 끝나야 합니다\n"
            "     (예: my_korail_alarm_bot). 이미 쓰는 이름이면 다시 물어봅니다.",
            justify="left",
        ).pack(anchor="w", padx=8, pady=6)

        step2 = ttk.LabelFrame(window, text="2단계 — 토큰 붙여넣기")
        step2.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=4)
        ttk.Label(
            step2,
            text='BotFather 답장에서 "Use this token to access the HTTP API:" 바로\n'
            "아랫줄을 통째로 복사해 아래 칸에 넣으세요.",
            justify="left",
        ).pack(anchor="w", padx=8, pady=(6, 2))
        ttk.Entry(step2, textvariable=token, width=52, show="*").pack(
            anchor="w", padx=8
        )
        # 예시는 텔레그램 공식 문서의 것을 씁니다. 진짜 토큰을 예시로 적어 두면
        # 그대로 붙여 넣는 사람이 생기고, 남의 봇 번호를 적어 둘 일도 아닙니다.
        ttk.Label(
            step2,
            text='모양: 123456789:ABCdefGHIjklMNOpqrSTUvwxYZ  (숫자 : 긴 문자열)\n'
            "이 토큰은 봇을 통째로 조종할 수 있습니다. 남에게 보이지 마세요.",
            foreground="#666666",
            justify="left",
        ).pack(anchor="w", padx=8, pady=(2, 4))
        ttk.Button(step2, text="토큰 확인", command=lambda: check_token()).pack(
            anchor="w", padx=8, pady=(0, 6)
        )

        step3 = ttk.LabelFrame(window, text="3단계 — 봇에게 먼저 말 걸기 (이걸 빼먹으면 안 됩니다)")
        step3.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=4)
        ttk.Label(
            step3,
            text="BotFather 답장 첫 줄의 t.me/… 링크를 눌러 내 봇과의 대화를 열고,\n"
            "[시작] 단추를 누르거나 /start 를 한 번 보냅니다.\n"
            "\n"
            "텔레그램은 사용자가 먼저 말을 건 적이 없는 봇에게 대화 ID 를 주지\n"
            "않습니다. 그래서 이 단계를 건너뛰면 아래 [내 대화 ID 찾기] 가 늘\n"
            "빈손으로 돌아옵니다.",
            justify="left",
        ).pack(anchor="w", padx=8, pady=6)

        step4 = ttk.LabelFrame(window, text="4단계 — 대화 ID 채우기")
        step4.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=4)
        ttk.Entry(step4, textvariable=chat_id, width=24).pack(anchor="w", padx=8, pady=(6, 2))
        ttk.Label(
            step4,
            text="모양: 123456789 — 숫자입니다(그룹이면 앞에 - 가 붙습니다).\n"
            "봇 이름(@my_korail_alarm_bot 같은 것)을 넣는 칸이 아닙니다. 텔레그램이\n"
            "@이름을 받는 것은 채널·슈퍼그룹뿐이고, 봇 자신은 대화 상대가 될 수\n"
            "없습니다.\n"
            "\n"
            "직접 알 필요 없습니다 — 3단계를 마쳤으면 아래 [내 대화 ID 찾기] 가\n"
            "채워 줍니다. 그 단추는 이 칸에 뭐가 적혀 있든 보지 않고 덮어씁니다\n"
            "(봇이 받은 마지막 메시지에서 읽어 옵니다). 그래서 뭘 쳐 넣었든\n"
            "누르는 순간 숫자로 바뀝니다.",
            foreground="#666666",
            justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 6))
        status = tk.StringVar(value="")
        ttk.Label(window, textvariable=status, foreground="#666666").grid(
            row=7, column=0, columnspan=2, sticky="w", padx=8, pady=6
        )

        def find_chat_id() -> None:
            def apply(found: ResolvedChat | None) -> None:
                if found is None:
                    status.set(
                        "못 찾았습니다 — 3단계를 하셨나요? 봇 대화에서 /start 를 "
                        "한 번 보낸 뒤 다시 누르세요."
                    )
                    return
                # 이 칸에 뭐가 적혀 있든 덮어씁니다. 그것이 이 단추의 일입니다.
                chat_id.set(found.chat_id)
                # 숫자 하나만 돌려주면 그 숫자가 무엇인지 알 수 없습니다.
                whose = f"({found.title} 님과의 대화)" if found.title else ""
                status.set(f"대화 ID {found.chat_id} 를 찾아 넣었습니다 {whose}".strip())

            def work() -> None:
                with TelegramNotifier(TelegramConfig(token=token.get().strip())) as bot:
                    found = bot.resolve_chat()
                self.events.put(lambda: apply(found))

            status.set("찾는 중…")
            self._in_thread(work, "telegram-updates")

        def check_token() -> None:
            """``getMe`` 로 토큰만 확인합니다. 아무것도 바꾸지 않습니다.

            토큰이 틀린 것과 대화 ID 가 없는 것은 증상이 같습니다("안 와요").
            갈라 주지 않으면 사람이 어디를 고쳐야 할지 알 수 없습니다.
            """
            def apply(name: str | None) -> None:
                if name:
                    status.set(f"토큰이 맞습니다 — 봇 @{name}. 이제 3단계로.")
                else:
                    status.set("토큰이 틀렸거나 연결이 안 됩니다. 2단계를 다시 보세요.")

            def work() -> None:
                with TelegramNotifier(TelegramConfig(token=token.get().strip())) as bot:
                    name = bot.bot_username()
                self.events.put(lambda: apply(name))

            status.set("토큰 확인 중…")
            self._in_thread(work, "telegram-getme")

        def bad_chat_id() -> bool:
            """숫자가 아니면 말해 줍니다. 그대로 보내면 조용히 실패합니다."""
            value = chat_id.get().strip()
            if not value or looks_like_chat_id(value):
                return False
            status.set(
                f'대화 ID 는 숫자입니다 — "{value}" 는 그 모양이 아닙니다. '
                "[내 대화 ID 찾기] 를 누르세요."
            )
            return True

        def send_test() -> None:
            if bad_chat_id():
                return
            config = TelegramConfig(token=token.get().strip(), chat_id=chat_id.get().strip())

            def work() -> None:
                with TelegramNotifier(config) as bot:
                    ok = bot.send("뉴레일 테스트 알림입니다.")
                self.events.put(
                    lambda: status.set("보냈습니다" if ok else "실패했습니다")
                )

            status.set("보내는 중…")
            self._in_thread(work, "telegram-test")

        def store() -> None:
            """설정 파일에 적습니다. 다음에 켤 때도 그대로 있습니다."""
            if bad_chat_id():
                return
            self.settings = replace(
                self.settings,
                telegram_token=token.get().strip(),
                telegram_chat_id=chat_id.get().strip(),
                notify_enabled=self.notify_enabled.get(),
            )
            path = settings_module.save(self.settings)
            # 저장한 값이 이번 실행에도 곧바로 쓰이도록, 일회용 값은 치웁니다.
            self._telegram_once = None
            self._write_log(
                f"텔레그램 설정을 저장했습니다: {path}" if path
                else "설정을 저장하지 못했습니다(권한을 확인하세요)."
            )
            window.destroy()
            # 이미 도는 묶음이 있으면 그쪽에도 알립니다. 뒤늦게 채운 사람은
            # 설정이 먹혔는지, 지금 무엇이 얼마나 남았는지를 알아야 합니다.
            self.announce_watches()

        def use_once() -> None:
            """이번 실행에만 씁니다. 파일에는 아무것도 쓰지 않습니다.

            남의 컴퓨터나 공용 컴퓨터에서 한 번만 쓰고 싶을 때를 위한 것입니다.
            토큰은 봇을 통째로 조종할 수 있으니 디스크에 남기지 않는 편이
            나을 때가 있습니다.
            """
            if bad_chat_id():
                return
            self._telegram_once = TelegramConfig(
                token=token.get().strip(),
                chat_id=chat_id.get().strip(),
            )
            self._write_log(
                "텔레그램 설정을 이번 실행에만 씁니다 — 파일에 저장하지 "
                "않았습니다. 프로그램을 끄면 사라집니다."
            )
            window.destroy()
            self.announce_watches()

        def forget() -> None:
            """저장된 값을 지웁니다. 이번 실행의 일회용 값도 같이 치웁니다."""
            self.settings = replace(
                self.settings,
                telegram_token="",
                telegram_chat_id="",
            )
            self._telegram_once = None
            token.set("")
            chat_id.set("")
            path = settings_module.save(self.settings)
            status.set("저장된 값을 지웠습니다.")
            self._write_log(
                f"텔레그램 설정을 지웠습니다: {path}" if path
                else "설정 파일을 고치지 못했습니다(권한을 확인하세요)."
            )

        buttons = ttk.Frame(window)
        buttons.grid(row=8, column=0, columnspan=2, sticky="w", padx=10, pady=8)
        ttk.Button(buttons, text="④ 내 대화 ID 찾기", command=find_chat_id).pack(side="left")
        ttk.Button(buttons, text="⑤ 테스트 전송", command=send_test).pack(side="left", padx=6)
        ttk.Button(buttons, text="⑥ 저장하고 쓰기", command=store).pack(side="left")
        ttk.Button(buttons, text="이번만 쓰기", command=use_once).pack(side="left", padx=6)
        ttk.Button(buttons, text="저장된 값 지우기", command=forget).pack(side="left")
        ttk.Label(
            window,
            text="잘 안 될 때:\n"
            "· [토큰 확인] 이 실패하면 → 2단계. 토큰을 잘못 복사한 것입니다\n"
            "   (앞뒤 공백, 줄바꿈, 한 글자 빠짐).\n"
            "· [토큰 확인] 은 되는데 대화 ID 를 못 찾으면 → 3단계를 안 한 것입니다.\n"
            "   봇 대화에서 /start 를 한 번 보내고 다시 누르세요.\n"
            "· 둘 다 채웠는데 테스트가 실패하면 → 봇 대화를 차단하지 않았는지 보세요.\n"
            "\n"
            "\n"
            "[⑥ 저장하고 쓰기] 는 이 컴퓨터의 설정 파일에 적습니다 — 다음에 켤 때도\n"
            "그대로 있습니다. [이번만 쓰기] 는 파일에 아무것도 쓰지 않고 이번 실행에만\n"
            "씁니다(프로그램을 끄면 사라집니다). 어느 쪽이든 토큰은 화면과 기록에\n"
            "남지 않습니다.",
            foreground="#666666",
            justify="left",
        ).grid(row=9, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 10))

    # -- 종료 ----------------------------------------------------------------

    def on_close(self) -> None:
        """끝내기 전에 **잃을 것이 있는지** 먼저 봅니다.

        예약 요청이 나가 있는 채로 닫으면 서버에는 예약이 생기고 이쪽에는
        PNR 도 결제 기한도 남지 않습니다. 잡아 둔 예약 목록도 파일이 아니라
        메모리에만 있습니다 — 닫는 순간 사라지고, 그 기한을 놓치면 표를
        잃습니다.
        """
        if self._reserving:
            if not messagebox.askyesno(
                "종료",
                "예약 요청이 아직 나가 있습니다. 지금 끝내면 서버에 예약이 "
                "생겨도 PNR 과 결제 기한을 여기서 볼 수 없습니다.\n\n"
                "정말 끝낼까요? (코레일 앱에서 예약 내역을 확인하세요)",
            ):
                return
        if self.any_running():
            if not messagebox.askyesno("종료", "자동예매가 돌고 있습니다. 정말 끝낼까요?"):
                return
            for watch in self.watches:
                watch.session.stop()
        unpaid = [held for held in self.holds if not is_expired(held.deadline, now_kst())]
        if unpaid and not messagebox.askyesno(
            "종료",
            f"결제 기한이 남은 예약 {len(unpaid)}건이 목록에 있습니다. 이 목록은 "
            "메모리에만 있어 끝내면 사라집니다 — PNR 을 적어 두셨나요?\n\n"
            + "\n".join(f"· {held.pnr}  {held.deadline_text}" for held in unpaid[:5])
            + "\n\n정말 끝낼까요?",
        ):
            return
        if self.client is not None:
            self.client.close()
        self.root.destroy()


def run() -> int:
    """창을 띄웁니다. ``app/main.py`` 가 부르는 곳입니다."""
    root = tk.Tk()
    app = BookerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0
