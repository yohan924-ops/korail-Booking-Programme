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
import queue
import threading
import time
import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date
from tkinter import messagebox, ttk

from korail_mobile_api import (
    KorailApiError,
    KorailClient,
    KorailPassengerCounts,
    KorailSeatClass,
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
    Target,
)
from .journeys import (
    Journey,
    JourneySource,
    SeatPreference,
    format_clock,
    format_duration,
    unbookable_detail,
    unbookable_reason,
)
from .logfmt import format_entry
from .notify import TelegramConfig, TelegramNotifier
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
    "departure": ("출발", 65),
    "arrival": ("도착", 65),
    "duration": ("소요", 85),
    "transfer": ("환승", 120),
    # 좌석 문구에는 "매진" 만 오는 것이 아니라 운임과 적립 안내까지 담겨
    # 옵니다. 좁으면 글자가 잘립니다.
    "general": ("일반실", 175),
    "special": ("특실", 175),
    "extras": ("그 밖", 100),
}

WEEKDAY_NAMES = ("월", "화", "수", "목", "금", "토", "일")
#: 달력 바탕. 뒤쪽 창(대개 #f0f0f0)보다 **살짝** 어둡습니다. 많이
#: 어두우면 글씨가 안 읽히고, 같으면 어디까지가 달력인지 안 보입니다.
CALENDAR_BG = "#e2e2e2"
#: 달력 테두리. 바탕보다 확실히 진해야 경계가 섭니다.
CALENDAR_BORDER = "#7a7a7a"
#: 칸 하나가 최소 높이 말고도 먹는 몫 — 손잡이와 위아래 여백.
PANE_CHROME = 13
#: 스스로 굴러가는 위젯. 이 위에서는 휠을 그쪽에 양보합니다.
SELF_SCROLLING = frozenset({"Text", "Treeview", "Listbox"})
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
    if raw < time.strftime("%Y%m%d"):
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

    def set_completions(self, names: Sequence[str]) -> None:
        self._completions = tuple(names)
        self.configure(values=list(self._completions))

    def _on_key_release(self, event: tk.Event) -> None:
        if not self._completions or event.keysym in _NAVIGATION_KEYS:
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
        self._today = date.today()
        self._shown = self._today.replace(day=1)
        self._header = tk.StringVar()
        self._title = tk.StringVar(value="가는 날")
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

    def open_for(self, title: str, current: date, *, over: tk.Misc, x: int, y: int) -> None:
        """조회 묶음 위에 겹쳐 띄웁니다.

        ``grid`` 로 한 줄을 차지하면 열릴 때마다 아래의 결과 표가 밀려 내려가고,
        창 밖으로 나가기까지 합니다. ``place`` 는 배치를 건드리지 않습니다 —
        팝업 창이 아니라 같은 창 안에 겹치는 것입니다.
        """
        self._title.set(title)
        self._shown = current.replace(day=1)
        self._draw()
        self.place(in_=over, x=x, y=y)
        self.lift()

    def hide(self) -> None:
        self.place_forget()

    def _shift(self, months: int) -> None:
        month = self._shown.month + months
        year = self._shown.year + (month - 1) // 12
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
                if current < self._today:
                    button.state(["disabled"])
                button.grid(row=row, column=column, padx=1, pady=1)


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
        self.session: BookingSession | None = None
        self.events: queue.Queue[Callable[[], None]] = queue.Queue()
        self._credentials: tuple[str, str] | None = None
        #: 환승역 목록이 어느 구간 것인지. 같은 구간이면 다시 묻지 않습니다.
        self._transfer_route: tuple[str, str] | None = None
        #: 전국 역 이름. 자동완성과 환승역 추가가 이것을 씁니다.
        self.station_names: tuple[str, ...] = ()
        #: 붙인 칸들 — (담은 PanedWindow, 묶음, 지정된 최소 높이 또는 None).
        self._panes: list[tuple[tk.PanedWindow, ttk.Widget, int | None]] = []
        #: 각 칸의 최소 높이. 본문 높이를 여기서 더해 냅니다.
        self._pane_minimums: list[int] = []
        #: 달력이 지금 어느 칸을 고치는 중인지.
        self._calendar_for_return = False
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
            *self.passenger_vars.values(),
        )
        self.root.after(120, self._drain)
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
        self.root.title("코레일 예매 도우미")
        self.root.geometry("1240x1000")
        # 스크롤이 있으므로 최소 크기를 크게 잡을 이유가 없습니다. 작은
        # 노트북에서도 창이 화면 밖으로 나가지 않아야 합니다.
        self.root.minsize(900, 480)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        canvas = tk.Canvas(self.root, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.root, orient="vertical", command=canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scroll.set)
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
            height = max(self._body_height(), visible)
            canvas.itemconfigure(window, width=width, height=height)
            canvas.configure(scrollregion=(0, 0, width, height))

        canvas.bind("<Configure>", lambda event: fit(event.width, event.height))
        # 휠은 창 어디서 굴려도 듣습니다. 다만 스스로 굴러가는 위젯 위에서는
        # 그쪽에 양보합니다 — 표를 굴리려는데 창이 굴러가면 못 씁니다.
        canvas.bind_all("<MouseWheel>", self._on_wheel)
        canvas.bind_all("<Button-4>", self._on_wheel)
        canvas.bind_all("<Button-5>", self._on_wheel)

        self._build_login(body)
        self._build_query(body)
        self._build_results(body)
        self._build_targets(body)
        self._build_booking(body)
        self._build_log(body)
        # 묶음을 다 붙인 뒤라야 최소 높이를 잴 수 있고, 그 합을 알아야 스크롤
        # 영역을 정할 수 있다 — 창이 그보다 작으면 굴려서 본다.
        self._settle_panes()
        fit(canvas.winfo_width(), canvas.winfo_height())
        # 조건을 고치고 Enter — 조회 단추를 찾아 누르지 않아도 됩니다.
        self.root.bind("<Return>", lambda _event: self.on_search())

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

    def _on_wheel(self, event: tk.Event) -> None:
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
        self.canvas.yview_scroll(step, "units")

    def _build_login(self, parent: tk.PanedWindow) -> None:
        frame = ttk.LabelFrame(parent, text="1. 로그인 (아이디·휴대폰번호·회원번호)")
        self._add_pane(parent, frame, stretch="never")
        self.login_id = tk.StringVar()
        self.login_pw = tk.StringVar()
        self.login_state = tk.StringVar(value="로그인하지 않았습니다 — 조회만 됩니다")
        ttk.Label(frame, text="아이디").grid(row=0, column=0, padx=4, pady=6)
        ttk.Entry(frame, textvariable=self.login_id, width=18).grid(row=0, column=1)
        ttk.Label(frame, text="비밀번호").grid(row=0, column=2, padx=4)
        ttk.Entry(frame, textvariable=self.login_pw, show="*", width=18).grid(
            row=0, column=3
        )
        self.login_button = ttk.Button(frame, text="로그인", command=self.on_login)
        self.login_button.grid(row=0, column=4, padx=8)
        ttk.Label(frame, textvariable=self.login_state).grid(
            row=0, column=5, padx=8, sticky="w"
        )
        ttk.Label(
            frame,
            text="비밀번호는 저장하지 않습니다. 비회원 예매는 지원하지 않습니다.",
            foreground="#666666",
        ).grid(row=1, column=0, columnspan=6, sticky="w", padx=4, pady=(0, 6))

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
        ttk.Label(route, text="승객").pack(side="left")
        for label, key in (
            ("어른", "adult"),
            ("청소년", "teenager"),
            ("어린이", "child"),
            ("유아", "infant"),
            ("경로", "senior"),
        ):
            ttk.Label(route, text=label).pack(side="left", padx=(8, 1))
            ttk.Entry(route, textvariable=self.passenger_vars[key], width=3).pack(
                side="left"
            )
        self.station_state = tk.StringVar(value="역 목록을 불러오는 중…")
        ttk.Label(route, textvariable=self.station_state, foreground="#666666").pack(
            side="left", padx=(14, 4)
        )
        ttk.Button(route, text="새로고침", width=8, command=self.on_load_stations).pack(
            side="left"
        )

        # 가는 편과 오는 편은 같은 모양의 줄입니다 — 날짜와 시간대를 각각.
        outbound = self._section(frame, 1, "가는 편")
        self._leg_fields(outbound, self.date, self.after_time, self.before_time, False)
        ttk.Checkbutton(
            outbound,
            text="왕복",
            variable=self.round_trip,
            command=self._round_trip_toggled,
        ).pack(side="left", padx=(16, 0))

        inbound = self._section(frame, 2, "오는 편")
        self.return_widgets = self._leg_fields(
            inbound, self.return_date, self.return_after_time, self.return_before_time, True
        )

        kinds = self._section(frame, 3, "열차 종류")
        for kind in TRAIN_KINDS:
            ttk.Checkbutton(
                kinds,
                text=kind,
                variable=self.train_kind_vars[kind],
                command=self.sync_train_kinds,
            ).pack(side="left", padx=(0, 6))
        ttk.Button(kinds, text="모두 지우기", command=self.clear_train_kinds).pack(
            side="left", padx=(6, 6)
        )
        ttk.Label(kinds, textvariable=self.train_kind_label, foreground="#1f6feb").pack(
            side="left"
        )

        seats = self._section(frame, 4, "좌석")
        ttk.Combobox(
            seats,
            textvariable=self.seat_choice,
            values=[label for label, _ in SEAT_CHOICES],
            width=6,
            state="readonly",
        ).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(
            seats, text="직통", variable=self.include_direct, command=self.mark_stale
        ).pack(side="left")
        ttk.Checkbutton(
            seats,
            text="환승",
            variable=self.include_transfer,
            command=self._transfer_toggled,
        ).pack(side="left", padx=(6, 0))

        # 환승 조건은 환승을 켰을 때만 만질 수 있습니다. 꺼져 있으면 아무 효과도
        # 없는 칸이라 켜 두면 헷갈리기만 합니다.
        self.transfer_frame = ttk.LabelFrame(
            frame, text="환승 조건 (직통 열차에는 영향을 주지 않습니다)"
        )
        # "w" 입니다("ew" 가 아니라) — 옆자리를 달력에게 내주려면 자기 폭만
        # 차지해야 합니다.
        self.transfer_frame.grid(row=5, column=0, sticky="w", padx=4, pady=(4, 6))
        self.query_frame = frame
        self.calendar = CalendarPanel(frame, self._calendar_picked)
        self._build_search_button(frame)
        left = ttk.Frame(self.transfer_frame)
        left.grid(row=0, column=0, sticky="nw", padx=4, pady=4)
        self.server_radio = ttk.Radiobutton(
            left,
            text="서버 추천 환승 (검증됨)",
            variable=self.transfer_mode,
            value=TRANSFER_SERVER,
            command=self._transfer_toggled,
        )
        self.server_radio.pack(anchor="w")
        self.custom_radio = ttk.Radiobutton(
            left,
            text="환승역 직접 지정 (서버 수용 미검증)",
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
        ttk.Label(self.transfer_time_row, text="분 이하 (0 = 제한 없음)").pack(
            side="left"
        )

        right = ttk.Frame(self.transfer_frame)
        right.grid(row=0, column=1, sticky="nw", padx=12, pady=4)
        ttk.Label(right, text="환승역 (Ctrl+클릭으로 여러 개)").pack(anchor="w")
        # 이 목록이 무엇이고 지금 무슨 구실을 하는지는 모드마다 다릅니다.
        # 화면이 그것을 말하지 않으면 고른 역이 필터인지 조회 대상인지 알 수
        # 없습니다.
        ttk.Label(right, textvariable=self.transfer_role, foreground="#1f6feb").pack(
            anchor="w"
        )
        picker = ttk.Frame(right)
        picker.pack(anchor="w")
        self.transfer_list = tk.Listbox(
            picker, selectmode="extended", height=4, width=24, exportselection=False
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
            adder, textvariable=self.transfer_query, width=14
        )
        self.transfer_entry.pack(side="left")
        self.transfer_entry.bind("<Return>", lambda _event: self.add_transfer_station())
        self.transfer_add_button = ttk.Button(
            adder, text="추가", width=5, command=self.add_transfer_station
        )
        self.transfer_add_button.pack(side="left", padx=4)
        self.transfer_load_button = ttk.Button(
            adder,
            text="이 구간 후보 다시 불러오기",
            command=self.on_load_transfer_stations,
        )
        self.transfer_load_button.pack(side="left", padx=4)
        ttk.Label(
            right,
            text="목록은 코레일이 이 구간에 대해 답한 환승역(qry.chtnStn.do)입니다. "
            "직접 지정 모드에서는 여기 없는 역도 위 칸에서 찾아 [추가] 하면 됩니다.",
            foreground="#666666",
            wraplength=380,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

    def _section(self, frame: ttk.LabelFrame, row: int, title: str) -> ttk.Frame:
        """이름 붙은 한 줄. 이름은 왼쪽에 고정 폭으로 세워 눈이 따라가게 합니다."""
        line = ttk.Frame(frame)
        line.grid(row=row, column=0, sticky="w", padx=4, pady=3)
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
        after.pack(side="left", padx=2)
        ttk.Label(parent, text="~").pack(side="left")
        before = ttk.Combobox(
            parent, textvariable=before_var, values=CLOCK_CHOICES, width=7,
            state="readonly",
        )
        before.pack(side="left", padx=2)
        return (entry, button, after, before)

    def _build_search_button(self, frame: ttk.LabelFrame) -> None:
        """조회 단추는 조건 **아래**에 크게 둡니다.

        조건을 고치고 나서 누르는 것이라, 조건 줄 사이에 끼어 있으면 눈이
        찾지 못합니다. 환승역을 바꾼 뒤 다시 누르는 일이 잦습니다.
        """
        bar = ttk.Frame(frame)
        bar.grid(row=6, column=0, sticky="ew", padx=4, pady=(0, 8))
        style = ttk.Style(self.root)
        style.configure("Search.TButton", font=("", 11, "bold"), padding=(24, 8))
        self.search_button = ttk.Button(
            bar, text="조회", style="Search.TButton", command=self.on_search
        )
        self.search_button.pack(side="left")
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
            current = date.today()
        self.calendar.open_for(
            "오는 날" if for_return else "가는 날",
            current,
            over=self.query_frame,
            # 날짜 칸 바로 아래입니다. 조회 조건 위에 겹칩니다.
            x=330 if not for_return else 560,
            y=36,
        )

    def _calendar_picked(self, picked: date) -> None:
        variable = self.return_date if self._calendar_for_return else self.date
        variable.set(picked.isoformat())

    def _round_trip_toggled(self) -> None:
        self.sync_round_trip_panes()
        enabled = self.round_trip.get()
        entry, button, after, before = self.return_widgets
        entry.configure(state="normal" if enabled else "disabled")
        button.configure(state="normal" if enabled else "disabled")
        # 콤보는 켜도 "normal" 이 아니라 "readonly" 입니다 — 고르는 칸이지
        # 쳐 넣는 칸이 아닙니다.
        for combo in (after, before):
            combo.configure(state="readonly" if enabled else "disabled")
        self.mark_stale()

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
        tree.column("#0", width=28, stretch=False)
        for name, (title, width) in TREE_COLUMNS.items():
            tree.heading(name, text=title)
            tree.column(name, width=width, anchor="center", stretch=False)
        tree.tag_configure("custom", foreground="#a15c00")
        tree.tag_configure("leg", foreground="#555555")
        # 자동예매가 노리는 것은 매진입니다. 한눈에 갈리게 색을 답니다.
        tree.tag_configure("open", foreground="#1a7f37")
        tree.tag_configure("soldout", foreground="#b42318")
        tree.tag_configure("unbookable", foreground="#8a8a8a")
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
        # 표는 줄여도 됩니다 — 보이는 줄 수만 줄어듭니다. 머리글과 상태 줄이
        # 남는 높이입니다.
        self._add_pane(parent, frame, minsize=110, stretch="always")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        self.outbound_title = ttk.Label(frame, text="가는 편", foreground="#1f6feb")
        self.outbound_title.grid(row=0, column=0, sticky="w", padx=6)
        self.outbound_title.grid_remove()
        self.inbound_title = ttk.Label(frame, text="오는 편", foreground="#1f6feb")
        self.inbound_title.grid(row=0, column=1, sticky="w", padx=6)
        self.inbound_title.grid_remove()

        outbound_pane = ttk.Frame(frame)
        outbound_pane.grid(row=1, column=0, sticky="nsew")
        self.tree = self._make_tree(outbound_pane)
        # 오는 편 표는 왕복일 때만 폅니다. 편도면 가는 편이 폭을 다 씁니다.
        self.inbound_pane = ttk.Frame(frame)
        self.inbound_pane.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        self.return_tree = self._make_tree(self.inbound_pane)
        self.inbound_pane.grid_remove()

        # 조건을 바꿔도 표는 그대로 남습니다. 그 표가 지금 조건의 결과인지
        # 아닌지를 말해 주지 않으면 "바꿨는데 아무 일도 안 일어난다" 가 됩니다.
        self.results_status = tk.StringVar(value="조건을 정하고 [조회] 를 누르세요.")
        self.results_label = ttk.Label(frame, textvariable=self.results_status)
        self.results_label.grid(row=2, column=0, columnspan=2, sticky="w", padx=4)

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

    def _build_targets(self, parent: tk.PanedWindow) -> None:
        frame = ttk.LabelFrame(parent, text="4. 예매 대상 (여기 담긴 것만 노립니다)")
        # 재서 씁니다. 96 으로 적어 뒀다가 [담기]·[빼기]·[비우기] 가 잘렸습니다.
        self._add_pane(parent, frame, stretch="always")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.target_list = tk.Listbox(
            frame, height=4, selectmode="extended", exportselection=False
        )
        self.target_list.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.target_list.yview)
        self.target_list.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns", pady=4)
        buttons = ttk.Frame(frame)
        buttons.grid(row=0, column=2, sticky="n", padx=6, pady=4)
        ttk.Button(buttons, text="↑ 담기", width=10, command=self.add_targets).pack()
        ttk.Button(buttons, text="빼기", width=10, command=self.remove_targets).pack(
            pady=(4, 0)
        )
        ttk.Button(buttons, text="비우기", width=10, command=self.clear_targets).pack(
            pady=(4, 0)
        )
        ttk.Label(
            frame,
            text="위 목록에서 고르고 [담기]. 왕복이면 가는 편·오는 편을 각각 "
            "담으세요 — 방향마다 한 건씩 잡고 멈춥니다.",
            foreground="#666666",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 6))

    def _build_booking(self, parent: tk.PanedWindow) -> None:
        frame = ttk.LabelFrame(parent, text="5. 자동예매 (만석이면 취소표를 계속 노립니다)")
        self._add_pane(parent, frame, stretch="never")
        self.poll_interval = tk.StringVar(value=f"{DEFAULT_POLL_INTERVAL_S:g}")
        self.watch_minutes = tk.StringVar(value="60")
        self.allow_standby = tk.BooleanVar(value=False)
        self.notify_enabled = tk.BooleanVar(value=True)
        row = ttk.Frame(frame)
        row.grid(row=0, column=0, sticky="w", padx=4, pady=6)
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
        row2.grid(row=1, column=0, sticky="w", padx=4, pady=(0, 6))
        ttk.Button(row2, text="텔레그램 설정", command=self.on_telegram_settings).pack(
            side="left", padx=12
        )
        self.start_button = ttk.Button(row2, text="자동예매 시작", command=self.on_start)
        self.start_button.pack(side="left", padx=4)
        self.stop_button = ttk.Button(
            row2, text="중지", command=self.on_stop, state="disabled"
        )
        self.stop_button.pack(side="left")
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
            text="결제는 하지 않습니다. 잡은 뒤 코레일 앱에서 기한 안에 결제하세요.",
            foreground="#666666",
        ).grid(row=2, column=0, sticky="w", padx=4, pady=(0, 6))

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
        self.train_kind_label.set(
            "전체 (아무것도 고르지 않음)" if not picked else f"{len(picked)}종 선택"
        )

    def clear_train_kinds(self) -> None:
        for var in self.train_kind_vars.values():
            var.set(False)
        self.sync_train_kinds()

    # -- 환승 조건 -----------------------------------------------------------

    def _transfer_toggled(self) -> None:
        """환승 체크나 모드가 바뀌었을 때. 상태를 맞추고 결과를 낡음으로."""
        self.sync_transfer_state()
        self.mark_stale()

    def sync_transfer_state(self) -> None:
        """환승 조건은 환승을 켰을 때만 만질 수 있습니다.

        고른 환승역의 구실도 여기서 갱신합니다 — 모드에 따라 뜻이 다릅니다.
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
        state = "normal" if self.include_transfer.get() else "disabled"
        for widget in (
            self.server_radio,
            self.custom_radio,
            self.min_transfer_entry,
            self.max_transfer_entry,
            self.transfer_load_button,
        ):
            widget.configure(state=state)
        # 역을 손으로 넣는 것은 직접 지정 모드에서만 뜻이 있습니다 — 서버 추천
        # 모드에서 없는 역을 넣으면 결과를 0편으로 만드는 필터가 될 뿐입니다.
        adding = (
            "normal"
            if self.include_transfer.get()
            and self.transfer_mode.get() == TRANSFER_CUSTOM
            else "disabled"
        )
        self.transfer_entry.configure(state=adding)
        self.transfer_add_button.configure(state=adding)
        self.transfer_list.configure(state=state)

    def selected_transfer_stations(self) -> tuple[str, ...]:
        picked = tuple(
            self.transfer_list.get(index) for index in self.transfer_list.curselection()
        )
        return tuple(name.strip() for name in picked if name.strip())

    def _fill_transfer_stations(
        self,
        names: list[str],
        *,
        select_all: bool = True,
    ) -> None:
        """목록을 채웁니다. **기본은 전부 선택** 입니다.

        고른 것이 하나도 없는 상태는 두 모드에서 뜻이 갈립니다 — 서버 추천에서는
        "전부 보기", 직접 지정에서는 "조회할 역이 없음". 전부 선택해 두면 화면에
        보이는 것과 실제로 쓰이는 것이 같아집니다.
        """
        keep = set(self.selected_transfer_stations())
        self.transfer_list.configure(state="normal")
        self.transfer_list.delete(0, "end")
        for name in names:
            self.transfer_list.insert("end", name)
        for index, name in enumerate(names):
            if select_all or name in keep:
                self.transfer_list.selection_set(index)
        self.sync_transfer_state()

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
        existing = list(self.transfer_list.get(0, "end"))
        if name not in existing:
            existing.append(name)
        self._fill_transfer_stations(existing, select_all=False)
        for index, item in enumerate(self.transfer_list.get(0, "end")):
            if item == name:
                self.transfer_list.selection_set(index)
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
        self.transfer_load_button.configure(state="normal")
        self._fill_transfer_stations(names)
        self._write_log(
            f"{self.departure.get()}→{self.arrival.get()} 환승역 {len(names)}개를 "
            "불러왔습니다."
            if names
            else "이 구간에는 서버가 알려 주는 환승역이 없습니다."
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
            return_depart_after=parse_clock_field(
                self.return_after_time.get(), label="오는 편 시작 시각"
            ),
            return_depart_before=parse_clock_field(
                self.return_before_time.get(), label="오는 편 끝 시각"
            ),
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
        messagebox.showerror("오류", detail)

    def _reset_buttons(self) -> None:
        self.login_button.configure(state="normal")
        self.search_button.configure(state="normal")
        self.transfer_load_button.configure(
            state="normal" if self.include_transfer.get() else "disabled"
        )
        if self.session is None or not self.session.running:
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")

    # -- 클라이언트 ----------------------------------------------------------

    def _ensure_client(self) -> KorailClient:
        if self.client is None:
            client, identity = build_client()
            self.client = client
            self.identity = identity
            self.log(f"클라이언트를 만들었습니다 (기기 신원: {identity})")
        return self.client

    # -- 동작: 로그인 --------------------------------------------------------

    def on_login(self) -> None:
        member_no = self.login_id.get().strip()
        password = self.login_pw.get()
        if not member_no or not password:
            messagebox.showwarning("로그인", "아이디와 비밀번호를 입력하세요")
            return
        self.login_button.configure(state="disabled")
        self.login_state.set("로그인 중…")

        def work() -> None:
            try:
                client = self._ensure_client()
                do_login(client, member_no, password)
            except (KorailApiError, ValueError) as exc:
                # 문구를 지금 붙잡습니다. except 블록을 벗어나면 파이썬이
                # 예외 이름을 지우므로, 나중에 도는 람다 안에서는 못 읽습니다.
                message = str(exc)
                self.events.put(lambda: self._login_failed(message))
                return
            self._credentials = (member_no, password)
            self.events.put(self._login_succeeded)

        self._in_thread(work, "korail-login")

    def _login_succeeded(self) -> None:
        self.logged_in = True
        self.login_state.set("로그인됨")
        self.login_button.configure(state="normal")
        self._write_log("로그인했습니다.", "good")
        self.settings = replace(self.settings, login_id=self.login_id.get().strip())
        settings_module.save(self.settings)

    def _login_failed(self, message: str) -> None:
        self.logged_in = False
        self.login_state.set("로그인 실패")
        self.login_button.configure(state="normal")
        self._write_log(f"로그인 실패: {message}", "bad")
        messagebox.showerror("로그인 실패", message)

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
        return return_request(
            outbound,
            date=parse_date_field(self.return_date.get()),
            depart_after=parse_clock_field(
                self.return_after_time.get(), label="오는 편 시작 시각"
            ),
            depart_before=parse_clock_field(
                self.return_before_time.get(), label="오는 편 끝 시각"
            ),
        )

    def on_search(self) -> None:
        try:
            request = self.build_request()
        except (ValueError, TypeError) as exc:
            messagebox.showwarning("조회 조건", str(exc))
            return
        self._remember(request)
        self.search_button.configure(state="disabled")
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
                return

        def work() -> None:
            found: list[Target] = []
            try:
                client = self._ensure_client()
                for label, leg in legs:
                    if label:
                        self.log(f"── {label}: {leg.departure}→{leg.arrival} {leg.date}")
                    self._refresh_transfer_stations(client, leg)
                    found.extend(
                        Target(journey=journey, request=leg, label=label)
                        for journey in search_journeys(client, leg, log=self.log)
                    )
            except (KorailApiError, ValueError) as exc:
                message = str(exc)
                self.events.put(lambda: self._search_failed(message))
                return
            self.events.put(lambda: self._show_journeys(found))

        self._in_thread(work, "korail-search")

    def _note_if_today(self, request: SearchRequest) -> None:
        """오늘 조회면 서버가 지금 이후 열차만 준다는 것을 적어 둡니다.

        시작 시각을 아침으로 두고 오후에 조회하면 "왜 이 열차가 없지" 가
        됩니다. 이미 떠난 열차는 서버가 주지 않습니다.
        """
        if request.date != time.strftime("%Y%m%d"):
            return
        now = time.strftime("%H%M%S")
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
        self.events.put(lambda: self._transfer_stations_loaded(names))

    def _search_failed(self, message: str) -> None:
        self.search_button.configure(state="normal")
        self.results_status.set("조회에 실패했습니다. 기록을 확인하세요.")
        self.results_label.configure(foreground="#b42318")
        self._write_log(f"조회 실패: {message}", "bad")
        messagebox.showerror("조회 실패", message)

    def _show_journeys(self, results: list[Target]) -> None:
        self.search_button.configure(state="normal")
        self.results = results
        self.journeys = [target.journey for target in results]
        self.sync_round_trip_panes()
        self.item_journeys.clear()
        for tree in (self.tree, self.return_tree):
            tree.delete(*tree.get_children())
        for index, target in enumerate(results):
            tree = self.return_tree if target.label == "오는 편" else self.tree
            self._insert_row(tree, index, target)
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
        for leg_index, leg in enumerate(journey.legs):
            # 구간 줄에도 그 구간의 좌석 상태를 적습니다. 부모 줄은 두 구간을
            # 합쳐 하나로 말하므로(한 구간만 매진이어도 '매진'), 어느 쪽이
            # 막혔는지는 여기서만 보입니다. 한 구간짜리 여정으로 만들어
            # 같은 계산을 그대로 씁니다 — 규칙을 두 번 쓰지 않습니다.
            alone = Journey(legs=(leg,), source=journey.source)
            tree.insert(
                item,
                "end",
                values=(
                    f"{leg_index + 1}구간",
                    f"{(leg.train_class_name or '').strip()} {leg.train_no}",
                    format_clock(leg.departure_time),
                    format_clock(leg.arrival_time),
                    format_duration(journey.leg_minutes(leg_index)),
                    f"{leg.departure_station_name}→{leg.arrival_station_name}",
                    alone.seat_text(KorailSeatClass.GENERAL),
                    alone.seat_text(KorailSeatClass.SPECIAL),
                    " · ".join(alone.extras()) or "-",
                ),
                tags=("leg",),
            )
        tree.item(item, open=True)

    def _row_values(self, target: Target) -> tuple[str, ...]:
        journey = target.journey
        if journey.is_transfer:
            kind = (
                "환승"
                if journey.source is JourneySource.SERVER_TRANSFER
                else "환승(직접)"
            )
            station = journey.transfer_station_name or "환승역 다름"
            transfer = f"{station} {format_duration(journey.transfer_minutes)}"
        else:
            kind = "직통"
            transfer = "-"
        names = " ".join(dict.fromkeys(name for name in journey.train_names() if name))
        trains = "+".join(journey.train_numbers())
        return (
            f"{target.label[:2]}·{kind}" if target.label else kind,
            f"{names} {trains}".strip(),
            format_clock(journey.departure_clock),
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
        if journey.source is JourneySource.CUSTOM_TRANSFER:
            return ("custom",)
        if journey.bookable_seat_class(SeatPreference.ANY) is not None:
            return ("open",)
        general = journey.seat_state(KorailSeatClass.GENERAL)
        special = journey.seat_state(KorailSeatClass.SPECIAL)
        return ("soldout",) if general.sold_out or special.sold_out else ()

    # -- 동작: 자동예매 ------------------------------------------------------

    def selected_results(self) -> list[Target]:
        """두 표에서 고른 것들. 구간 행을 골랐으면 그 여정을 씁니다."""
        chosen: list[Target] = []
        for tree in (self.tree, self.return_tree):
            for item in tree.selection():
                index = self.item_journeys.get((str(tree), item))
                if index is None:
                    index = self.item_journeys.get((str(tree), tree.parent(item)))
                if index is not None and self.results[index] not in chosen:
                    chosen.append(self.results[index])
        return chosen

    def add_targets(self) -> None:
        picked = self.selected_results()
        if not picked:
            messagebox.showwarning("예매 대상", "위 목록에서 열차를 고르고 [담기] 를 누르세요")
            return
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
            if any(
                existing.journey.key() == target.journey.key()
                and existing.direction == target.direction
                for existing in self.targets
            ):
                continue
            self.targets.append(target)
            self.target_list.insert("end", target.describe())
            added += 1
        self._write_log(
            f"예매 대상에 {added}편을 담았습니다 (모두 {len(self.targets)}편)."
            if added
            else "이미 담긴 열차입니다."
        )

    def remove_targets(self) -> None:
        for index in sorted(self.target_list.curselection(), reverse=True):
            self.target_list.delete(index)
            del self.targets[index]
        self._write_log(f"예매 대상 {len(self.targets)}편 남았습니다.")

    def clear_targets(self) -> None:
        self.target_list.delete(0, "end")
        self.targets.clear()
        self._write_log("예매 대상을 비웠습니다.")

    def build_options(self) -> BookingOptions:
        interval = self.poll_interval.get().strip()
        try:
            interval_s = float(interval)
        except ValueError as exc:
            raise ValueError("조회 주기는 숫자여야 합니다") from exc
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

    def on_start(self) -> None:
        if self.session is not None and self.session.running:
            messagebox.showinfo("자동예매", "이미 돌고 있습니다")
            return
        targets = list(self.targets)
        if not targets:
            messagebox.showwarning(
                "자동예매", "먼저 [담기] 로 예매 대상에 열차를 넣으세요"
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
        if not self._confirm_live(targets):
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
        booker = AutoBooker(
            self._ensure_client(),
            targets,
            options,
            log=self.log_booking,
            notify=self._make_notifier(),
            relogin=self.relogin if self._credentials else None,
        )
        self.session = BookingSession(booker)
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        directions = len({target.direction for target in targets})
        self._write_booking(
            f"자동예매 시작 — {len(targets)}편 감시, 방향 {directions}개"
        )
        self.session.start(on_done=lambda result: self.events.put(
            lambda: self._booking_done(result)
        ))

    def _confirm_live(self, targets: list[Target]) -> bool:
        lines = "\n".join(f"· {target.describe()}" for target in targets[:5])
        return messagebox.askyesno(
            "실제 예약을 만듭니다",
            "아래 열차를 지켜보다가 **방향마다 한 건씩** 진짜 예약(결제 전 홀드)을 "
            "만듭니다.\n\n"
            f"{lines}\n\n"
            "결제는 하지 않습니다. 잡은 뒤에는 코레일 앱에서 기한 안에 결제하거나 "
            "취소해야 합니다. 계속할까요?",
        )

    def _make_notifier(self) -> Callable[[str], None] | None:
        if not self.notify_enabled.get():
            return None
        config = TelegramConfig(
            token=self.settings.telegram_token,
            chat_id=self.settings.telegram_chat_id,
        )
        if not config.enabled:
            self._write_booking("텔레그램 설정이 없어 알림은 보내지 않습니다.", "warn")
            return None

        def send(message: str) -> None:
            with TelegramNotifier(config) as notifier:
                if not notifier.send(message):
                    self.log_booking("텔레그램 전송에 실패했습니다.")

        return send

    def on_stop(self) -> None:
        if self.session is not None:
            self.session.stop()
            self._write_booking("중지를 요청했습니다. 이번 조회가 끝나면 멈춥니다.")

    def _booking_done(self, result: BookingResult) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        levels = {
            Outcome.HELD: "good",
            Outcome.FAILED: "bad",
            Outcome.PREVIEW: "warn",
        }
        self._write_booking(
            f"자동예매 종료 ({result.outcome.value}): {result.message}",
            levels.get(result.outcome, "info"),
        )
        if result.outcome is Outcome.HELD:
            messagebox.showinfo("예약됨", result.message)
        elif result.outcome is Outcome.FAILED:
            messagebox.showerror("자동예매 실패", result.message)
        elif result.outcome is Outcome.PREVIEW:
            # 미리보기는 "잡을 수 있었다" 로 끝납니다. 기록 한 줄로만 알리면
            # 잡힌 줄 알고 코레일 장바구니를 열어 보게 됩니다.
            messagebox.showinfo(
                "미리보기 — 아무것도 보내지 않았습니다",
                f"{result.message}\n\n"
                "이번 실행은 미리보기였습니다. 예약도 장바구니도 만들어지지 "
                "않았고, 코레일에는 아무 요청도 나가지 않았습니다.\n\n"
                "실제로 잡으려면 [실제 예약(홀드) 만들기] 를 켜고 다시 "
                "[자동예매 시작] 을 누르세요.",
            )

    # -- 동작: 텔레그램 ------------------------------------------------------

    def on_telegram_settings(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("텔레그램 알림 설정")
        window.transient(self.root)
        token = tk.StringVar(value=self.settings.telegram_token)
        chat_id = tk.StringVar(value=self.settings.telegram_chat_id)
        ttk.Label(window, text="봇 토큰 (@BotFather 에서 발급)").grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 2)
        )
        ttk.Entry(window, textvariable=token, width=48, show="*").grid(
            row=1, column=0, columnspan=2, padx=8
        )
        ttk.Label(window, text="대화 ID (봇에게 아무 메시지나 보낸 뒤 찾기)").grid(
            row=2, column=0, sticky="w", padx=8, pady=(8, 2)
        )
        ttk.Entry(window, textvariable=chat_id, width=24).grid(
            row=3, column=0, sticky="w", padx=8
        )
        status = tk.StringVar(value="")
        ttk.Label(window, textvariable=status, foreground="#666666").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=8, pady=6
        )

        def find_chat_id() -> None:
            def apply(found: str | None) -> None:
                if found:
                    chat_id.set(found)
                    status.set(f"대화 ID {found} 를 찾았습니다")
                else:
                    status.set("찾지 못했습니다. 봇에게 먼저 말을 걸어 보세요")

            def work() -> None:
                with TelegramNotifier(TelegramConfig(token=token.get().strip())) as bot:
                    found = bot.resolve_chat_id()
                self.events.put(lambda: apply(found))

            status.set("찾는 중…")
            self._in_thread(work, "telegram-updates")

        def send_test() -> None:
            config = TelegramConfig(token=token.get().strip(), chat_id=chat_id.get().strip())

            def work() -> None:
                with TelegramNotifier(config) as bot:
                    ok = bot.send("코레일 예매 도우미 테스트 알림입니다.")
                self.events.put(
                    lambda: status.set("보냈습니다" if ok else "실패했습니다")
                )

            status.set("보내는 중…")
            self._in_thread(work, "telegram-test")

        def store() -> None:
            self.settings = replace(
                self.settings,
                telegram_token=token.get().strip(),
                telegram_chat_id=chat_id.get().strip(),
                notify_enabled=self.notify_enabled.get(),
            )
            path = settings_module.save(self.settings)
            self._write_log(
                f"텔레그램 설정을 저장했습니다: {path}" if path
                else "설정을 저장하지 못했습니다(권한을 확인하세요)."
            )
            window.destroy()

        buttons = ttk.Frame(window)
        buttons.grid(row=5, column=0, columnspan=2, sticky="w", padx=8, pady=8)
        ttk.Button(buttons, text="내 대화 ID 찾기", command=find_chat_id).pack(side="left")
        ttk.Button(buttons, text="테스트 전송", command=send_test).pack(side="left", padx=6)
        ttk.Button(buttons, text="저장", command=store).pack(side="left")
        ttk.Label(
            window,
            text="토큰은 이 컴퓨터의 설정 파일에만 저장되며 화면과 기록에는 남지 않습니다.",
            foreground="#666666",
        ).grid(row=6, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))

    # -- 종료 ----------------------------------------------------------------

    def on_close(self) -> None:
        if self.session is not None and self.session.running:
            if not messagebox.askyesno("종료", "자동예매가 돌고 있습니다. 정말 끝낼까요?"):
                return
            self.session.stop()
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
