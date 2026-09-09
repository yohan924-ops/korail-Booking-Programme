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

import queue
import threading
import time
import tkinter as tk
from collections.abc import Callable
from dataclasses import replace
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
)
from .journeys import (
    Journey,
    JourneySource,
    SeatPreference,
    format_clock,
    format_duration,
)
from .notify import TelegramConfig, TelegramNotifier
from .search import (
    TRANSFER_CUSTOM,
    TRANSFER_SERVER,
    SearchRequest,
    search_journeys,
    transfer_station_candidates,
)
from .session import build_client
from .session import login as do_login


#: 열차 종별. 거르는 방식이 **부분일치**라 ``"KTX"`` 하나로 ``KTX-산천`` 과
#: ``KTX-이음`` 까지 함께 잡힙니다. 산천만 보려면 그 이름을 직접 고르면 됩니다.
#: 목록에 없는 종별은 칸에 직접 쳐 넣을 수 있습니다(콤보가 읽기 전용이 아님).
TRAIN_KINDS = (
    "전체",
    "KTX",
    "KTX-산천",
    "KTX-이음",
    "ITX",
    "ITX-새마을",
    "ITX-마음",
    "새마을",
    "무궁화",
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


class BookerApp:
    """창 하나에 로그인·조회·자동예매가 다 들어간 화면."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = settings_module.load()
        self.client: KorailClient | None = None
        self.identity = ""
        self.logged_in = False
        self.journeys: list[Journey] = []
        self.item_journeys: dict[str, int] = {}
        self.session: BookingSession | None = None
        self.events: queue.Queue[Callable[[], None]] = queue.Queue()
        self._credentials: tuple[str, str] | None = None
        self._build()
        self._restore()
        self.root.after(120, self._drain)

    # -- 화면 만들기 ---------------------------------------------------------

    def _build(self) -> None:
        self.root.title("코레일 예매 도우미")
        self.root.geometry("1180x800")
        self.root.minsize(980, 620)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=3)
        self.root.rowconfigure(4, weight=2)
        self._build_login()
        self._build_query()
        self._build_results()
        self._build_booking()
        self._build_log()

    def _build_login(self) -> None:
        frame = ttk.LabelFrame(self.root, text="1. 로그인 (아이디·휴대폰번호·회원번호)")
        frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
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

    def _build_query(self) -> None:
        frame = ttk.LabelFrame(self.root, text="2. 열차 조회")
        frame.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        self.departure = tk.StringVar()
        self.arrival = tk.StringVar()
        self.date = tk.StringVar(value=time.strftime("%Y-%m-%d"))
        self.after_time = tk.StringVar()
        self.before_time = tk.StringVar()
        self.train_kind = tk.StringVar(value="전체")
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

        row = ttk.Frame(frame)
        row.grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Label(row, text="출발").pack(side="left")
        self.departure_box = ttk.Combobox(row, textvariable=self.departure, width=10)
        self.departure_box.pack(side="left", padx=(2, 8))
        ttk.Label(row, text="도착").pack(side="left")
        self.arrival_box = ttk.Combobox(row, textvariable=self.arrival, width=10)
        self.arrival_box.pack(side="left", padx=(2, 8))
        ttk.Label(row, text="날짜").pack(side="left")
        ttk.Entry(row, textvariable=self.date, width=12).pack(side="left", padx=(2, 8))
        # 시각은 고르는 것입니다. 손으로 치면 형식을 틀리기 쉽고, 틀린 값은
        # 조회 전에 경고창으로만 돌아옵니다.
        ttk.Label(row, text="시간").pack(side="left")
        ttk.Combobox(
            row,
            textvariable=self.after_time,
            values=CLOCK_CHOICES,
            width=7,
            state="readonly",
        ).pack(side="left", padx=2)
        ttk.Label(row, text="~").pack(side="left")
        ttk.Combobox(
            row,
            textvariable=self.before_time,
            values=CLOCK_CHOICES,
            width=7,
            state="readonly",
        ).pack(side="left", padx=2)
        ttk.Button(row, text="역 목록 불러오기", command=self.on_load_stations).pack(
            side="left", padx=8
        )

        row2 = ttk.Frame(frame)
        row2.grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Label(row2, text="열차 종류").pack(side="left")
        # 읽기 전용이 아닙니다 — 목록에 없는 종별을 직접 칠 수 있어야 합니다.
        # 거르는 방식이 부분일치라 "KTX" 는 KTX-산천·KTX-이음까지 함께 잡습니다.
        ttk.Combobox(
            row2,
            textvariable=self.train_kind,
            values=TRAIN_KINDS,
            width=12,
        ).pack(side="left", padx=(2, 8))
        ttk.Label(row2, text="좌석").pack(side="left")
        ttk.Combobox(
            row2,
            textvariable=self.seat_choice,
            values=[label for label, _ in SEAT_CHOICES],
            width=6,
            state="readonly",
        ).pack(side="left", padx=(2, 12))
        ttk.Checkbutton(row2, text="직통", variable=self.include_direct).pack(
            side="left"
        )
        ttk.Checkbutton(
            row2,
            text="환승",
            variable=self.include_transfer,
            command=self.sync_transfer_state,
        ).pack(side="left", padx=(4, 12))
        ttk.Label(row2, text="승객").pack(side="left")
        for label, key in (
            ("어른", "adult"),
            ("청소년", "teenager"),
            ("어린이", "child"),
            ("유아", "infant"),
            ("경로", "senior"),
        ):
            ttk.Label(row2, text=label).pack(side="left", padx=(6, 1))
            ttk.Entry(row2, textvariable=self.passenger_vars[key], width=3).pack(
                side="left"
            )
        self.search_button = ttk.Button(row2, text="조회", command=self.on_search)
        self.search_button.pack(side="left", padx=16)

        # 환승 조건은 환승을 켰을 때만 만질 수 있습니다. 꺼져 있으면 아무 효과도
        # 없는 칸이라 켜 두면 헷갈리기만 합니다.
        self.transfer_frame = ttk.LabelFrame(
            frame, text="환승 조건 (직통 열차에는 영향을 주지 않습니다)"
        )
        self.transfer_frame.grid(row=2, column=0, sticky="ew", padx=4, pady=(2, 6))
        left = ttk.Frame(self.transfer_frame)
        left.grid(row=0, column=0, sticky="nw", padx=4, pady=4)
        self.server_radio = ttk.Radiobutton(
            left,
            text="서버 추천 환승 (검증됨)",
            variable=self.transfer_mode,
            value=TRANSFER_SERVER,
            command=self.sync_transfer_state,
        )
        self.server_radio.pack(anchor="w")
        self.custom_radio = ttk.Radiobutton(
            left,
            text="환승역 직접 지정 (서버 수용 미검증)",
            variable=self.transfer_mode,
            value=TRANSFER_CUSTOM,
            command=self.sync_transfer_state,
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
        self.transfer_list.pack(side="left")
        list_scroll = ttk.Scrollbar(
            picker, orient="vertical", command=self.transfer_list.yview
        )
        self.transfer_list.configure(yscrollcommand=list_scroll.set)
        list_scroll.pack(side="left", fill="y")
        self.transfer_load_button = ttk.Button(
            right,
            text="이 구간의 환승역 불러오기",
            command=self.on_load_transfer_stations,
        )
        self.transfer_load_button.pack(anchor="w", pady=(4, 0))
        ttk.Label(
            right,
            text="목록은 코레일이 이 구간에 대해 답한 환승역입니다"
            "(qry.chtnStn.do). 전국 역 목록이 아닙니다.",
            foreground="#666666",
            wraplength=320,
            justify="left",
        ).pack(anchor="w")

    def _build_results(self) -> None:
        frame = ttk.LabelFrame(self.root, text="3. 열차 (여러 개 고르면 먼저 열리는 것을 잡습니다)")
        frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=4)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        columns = ("kind", "train", "departure", "arrival", "duration", "transfer",
                   "general", "special")
        self.tree = ttk.Treeview(
            frame, columns=columns, show="tree headings", selectmode="extended"
        )
        headings = {
            "kind": ("구분", 90),
            "train": ("열차", 110),
            "departure": ("출발", 70),
            "arrival": ("도착", 70),
            "duration": ("소요", 90),
            "transfer": ("환승", 130),
            "general": ("일반실", 110),
            "special": ("특실", 110),
        }
        self.tree.column("#0", width=30, stretch=False)
        for name, (title, width) in headings.items():
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, anchor="center")
        self.tree.tag_configure("custom", foreground="#a15c00")
        self.tree.tag_configure("leg", foreground="#555555")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")

    def _build_booking(self) -> None:
        frame = ttk.LabelFrame(self.root, text="4. 자동예매 (만석이면 취소표를 계속 노립니다)")
        frame.grid(row=3, column=0, sticky="ew", padx=8, pady=4)
        self.poll_interval = tk.StringVar(value=f"{DEFAULT_POLL_INTERVAL_S:g}")
        self.watch_minutes = tk.StringVar(value="60")
        self.allow_standby = tk.BooleanVar(value=False)
        self.add_to_cart = tk.BooleanVar(value=False)
        self.live_mode = tk.BooleanVar(value=False)
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
        ttk.Checkbutton(row, text="잡으면 장바구니에도", variable=self.add_to_cart).pack(
            side="left", padx=8
        )
        ttk.Checkbutton(row, text="텔레그램 알림", variable=self.notify_enabled).pack(
            side="left"
        )
        row2 = ttk.Frame(frame)
        row2.grid(row=1, column=0, sticky="w", padx=4, pady=(0, 6))
        ttk.Checkbutton(
            row2,
            text="실제 예약(홀드) 만들기 — 끄면 미리보기만",
            variable=self.live_mode,
        ).pack(side="left")
        ttk.Button(row2, text="텔레그램 설정", command=self.on_telegram_settings).pack(
            side="left", padx=12
        )
        self.start_button = ttk.Button(row2, text="자동예매 시작", command=self.on_start)
        self.start_button.pack(side="left", padx=4)
        self.stop_button = ttk.Button(
            row2, text="중지", command=self.on_stop, state="disabled"
        )
        self.stop_button.pack(side="left")
        ttk.Label(
            row2,
            text="결제는 하지 않습니다. 잡은 뒤 코레일 앱에서 기한 안에 결제하세요.",
            foreground="#666666",
        ).pack(side="left", padx=12)

    def _build_log(self) -> None:
        frame = ttk.LabelFrame(self.root, text="기록")
        frame.grid(row=4, column=0, sticky="nsew", padx=8, pady=(4, 8))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(frame, height=10, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")

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
        self.train_kind.set(stored.train_name or "전체")
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
        self.add_to_cart.set(stored.add_to_cart)
        self.notify_enabled.set(stored.notify_enabled)
        for key, var in self.passenger_vars.items():
            var.set(str(getattr(stored, key)))
        self.sync_transfer_state()

    # -- 환승 조건 -----------------------------------------------------------

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
        select_all: bool = False,
    ) -> None:
        keep = set(self.selected_transfer_stations())
        self.transfer_list.configure(state="normal")
        self.transfer_list.delete(0, "end")
        for name in names:
            self.transfer_list.insert("end", name)
        for index, name in enumerate(names):
            if select_all or name in keep:
                self.transfer_list.selection_set(index)
        self.sync_transfer_state()

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
            train_name=self.train_kind.get(),
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

    def _write_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

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
        self._write_log("로그인했습니다.")
        self.settings = replace(self.settings, login_id=self.login_id.get().strip())
        settings_module.save(self.settings)

    def _login_failed(self, message: str) -> None:
        self.logged_in = False
        self.login_state.set("로그인 실패")
        self.login_button.configure(state="normal")
        self._write_log(f"로그인 실패: {message}")
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
                self.log(f"역 목록을 불러오지 못했습니다: {exc}")
                return
            names = sorted({station.name for station in stations if station.name})
            self.events.put(lambda: self._fill_stations(names))

        self._in_thread(work, "korail-stations")

    def _fill_stations(self, names: list[str]) -> None:
        self.departure_box.configure(values=names)
        self.arrival_box.configure(values=names)
        self._write_log(f"역 {len(names)}개를 불러왔습니다.")

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
        kind = self.train_kind.get().strip()
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
            train_name="" if kind in ("", "전체") else kind,
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

    def on_search(self) -> None:
        try:
            request = self.build_request()
        except (ValueError, TypeError) as exc:
            messagebox.showwarning("조회 조건", str(exc))
            return
        self._remember(request)
        self.search_button.configure(state="disabled")
        self.log(f"조회: {request.departure}→{request.arrival} {request.date}")

        def work() -> None:
            try:
                client = self._ensure_client()
                journeys = search_journeys(client, request, log=self.log)
            except (KorailApiError, ValueError) as exc:
                message = str(exc)
                self.events.put(lambda: self._search_failed(message))
                return
            self.events.put(lambda: self._show_journeys(journeys))

        self._in_thread(work, "korail-search")

    def _search_failed(self, message: str) -> None:
        self.search_button.configure(state="normal")
        self._write_log(f"조회 실패: {message}")
        messagebox.showerror("조회 실패", message)

    def _show_journeys(self, journeys: list[Journey]) -> None:
        self.search_button.configure(state="normal")
        self.journeys = journeys
        self.item_journeys.clear()
        self.tree.delete(*self.tree.get_children())
        for index, journey in enumerate(journeys):
            item = self.tree.insert("", "end", values=self._row_values(journey),
                                    tags=self._row_tags(journey))
            self.item_journeys[item] = index
            if journey.is_transfer:
                for leg_index, leg in enumerate(journey.legs):
                    self.tree.insert(
                        item,
                        "end",
                        values=(
                            f"{leg_index + 1}구간",
                            f"{(leg.train_class_name or '').strip()} {leg.train_no}",
                            format_clock(leg.departure_time),
                            format_clock(leg.arrival_time),
                            format_duration(journey.leg_minutes(leg_index)),
                            f"{leg.departure_station_name}→{leg.arrival_station_name}",
                            "",
                            "",
                        ),
                        tags=("leg",),
                    )
                self.tree.item(item, open=True)
        self._write_log(f"열차 {len(journeys)}편을 찾았습니다.")
        if not journeys:
            messagebox.showinfo(
                "조회 결과 없음",
                "조건에 맞는 열차가 없습니다.\n\n"
                "아래 기록 창에 서버가 뭐라고 답했는지 찍혀 있습니다. "
                "역 이름(예: '서울', '동대구')과 날짜를 먼저 확인해 보세요.",
            )

    def _row_values(self, journey: Journey) -> tuple[str, ...]:
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
        general = journey.seat_state(KorailSeatClass.GENERAL)
        special = journey.seat_state(KorailSeatClass.SPECIAL)
        return (
            kind,
            f"{names} {trains}".strip(),
            format_clock(journey.departure_clock),
            format_clock(journey.arrival_clock),
            format_duration(journey.total_minutes),
            transfer,
            general.label,
            special.label,
        )

    def _row_tags(self, journey: Journey) -> tuple[str, ...]:
        return ("custom",) if journey.source is JourneySource.CUSTOM_TRANSFER else ()

    # -- 동작: 자동예매 ------------------------------------------------------

    def selected_journeys(self) -> list[Journey]:
        chosen: list[Journey] = []
        for item in self.tree.selection():
            index = self.item_journeys.get(item)
            if index is None:  # 구간 행을 골랐으면 부모 여정을 씁니다.
                index = self.item_journeys.get(self.tree.parent(item))
            if index is not None and self.journeys[index] not in chosen:
                chosen.append(self.journeys[index])
        return chosen

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
            add_to_cart=self.add_to_cart.get(),
            live=self.live_mode.get(),
        )

    def on_start(self) -> None:
        if self.session is not None and self.session.running:
            messagebox.showinfo("자동예매", "이미 돌고 있습니다")
            return
        targets = self.selected_journeys()
        if not targets:
            messagebox.showwarning("자동예매", "목록에서 열차를 하나 이상 고르세요")
            return
        try:
            request = self.build_request()
            options = self.build_options()
        except (ValueError, TypeError) as exc:
            messagebox.showwarning("자동예매", str(exc))
            return
        if options.live and not self.logged_in:
            messagebox.showwarning("자동예매", "실제 예약을 하려면 먼저 로그인하세요")
            return
        if options.live and not self._confirm_live(targets):
            return
        custom = [j for j in targets if j.source is JourneySource.CUSTOM_TRANSFER]
        if custom and not messagebox.askyesno(
            "확인",
            "직접 지정한 환승 조합이 들어 있습니다. 서버가 이런 조합을 받아들이는지"
            " 확인된 바 없습니다. 그래도 시도할까요?",
        ):
            return
        booker = AutoBooker(
            self._ensure_client(),
            request,
            targets,
            options,
            log=self.log,
            notify=self._make_notifier(),
            relogin=self.relogin if self._credentials else None,
        )
        self.session = BookingSession(booker)
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        mode = "실제 예약" if options.live else "미리보기(아무것도 보내지 않음)"
        self.log(f"자동예매 시작 — {len(targets)}편 감시, {mode}")
        self.session.start(on_done=lambda result: self.events.put(
            lambda: self._booking_done(result)
        ))

    def _confirm_live(self, targets: list[Journey]) -> bool:
        lines = "\n".join(f"· {journey.summary()}" for journey in targets[:5])
        return messagebox.askyesno(
            "실제 예약을 만듭니다",
            "아래 열차 중 먼저 열리는 것 하나에 진짜 예약(결제 전 홀드)을 만듭니다.\n\n"
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
            self.log("텔레그램 설정이 없어 알림은 보내지 않습니다.")
            return None

        def send(message: str) -> None:
            with TelegramNotifier(config) as notifier:
                if not notifier.send(message):
                    self.log("텔레그램 전송에 실패했습니다.")

        return send

    def on_stop(self) -> None:
        if self.session is not None:
            self.session.stop()
            self.log("중지를 요청했습니다. 이번 조회가 끝나면 멈춥니다.")

    def _booking_done(self, result: BookingResult) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self._write_log(f"자동예매 종료 ({result.outcome.value}): {result.message}")
        if result.outcome is Outcome.HELD:
            messagebox.showinfo("예약됨", result.message)
        elif result.outcome is Outcome.FAILED:
            messagebox.showerror("자동예매 실패", result.message)

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
