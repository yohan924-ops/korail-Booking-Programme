"""화면에 입력한 것 중 **비밀이 아닌 것**만 사용자 홈에 저장합니다.

**비밀번호는 저장하지 않습니다.** 이 파일에는 비밀번호를 담을 필드가 아예
없습니다 — 저장할 자리가 없으면 실수로 저장될 수도 없습니다. 텔레그램 봇
토큰은 저장합니다(그것 없이는 알림이 매번 다시 입력을 요구합니다). 그래서
파일은 소유자만 읽을 수 있게 ``0600`` 으로 씁니다.

저장 위치는 저장소 바깥입니다. Windows 는 ``%APPDATA%``, 나머지는
``$XDG_CONFIG_HOME`` 또는 ``~/.config`` 아래입니다.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


APP_DIR_NAME = "korail-booker"
SETTINGS_FILE_NAME = "settings.json"
#: 소유자만 읽고 쓰기. 토큰이 들어 있는 파일입니다.
SETTINGS_FILE_MODE = 0o600
#: 정수 칸의 상한. 승객 수도 분 수도 이보다 클 이유가 없고, 이보다 크면
#: 나중에 산술에서 터집니다.
MAX_INT_SETTING = 10_000_000


def settings_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"
    return root / APP_DIR_NAME


def settings_path() -> Path:
    return settings_dir() / SETTINGS_FILE_NAME


@dataclass
class Settings:
    """다시 켰을 때 그대로 있어 주면 좋은 것들. 비밀번호는 없습니다."""

    #: 로그인 아이디·휴대폰번호·회원번호 중 마지막에 쓴 것.
    login_id: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    notify_enabled: bool = True
    departure: str = "서울"
    arrival: str = "부산"
    depart_after: str = ""
    depart_before: str = ""
    round_trip: bool = False
    #: 오는 편은 자기 시간대를 씁니다. 가는 편과 같은 시간대를 쓰라는 법이 없습니다.
    return_depart_after: str = ""
    return_depart_before: str = ""
    train_names: list[str] = field(default_factory=list)
    seat_preference: str = "any"
    include_direct: bool = True
    include_transfer: bool = False
    transfer_mode: str = "server"
    transfer_stations: list[str] = field(default_factory=list)
    min_transfer_minutes: int = 0
    #: 위쪽을 열어 두면(0) 몇 시간씩 기다리는 조합까지 다 딸려옵니다. 30분이
    #: 기본입니다 — 화면의 기본값과 같아야 하므로 시험이 둘을 대조합니다.
    max_transfer_minutes: int = 30
    poll_interval_s: float = 30.0
    watch_minutes: int = 60
    allow_standby: bool = False
    adult: int = 1
    teenager: int = 0
    child: int = 0
    infant: int = 0
    senior: int = 0

    def masked(self) -> dict[str, Any]:
        """로그나 화면에 찍어도 되는 모양. 토큰을 가립니다."""
        data = asdict(self)
        if data.get("telegram_token"):
            data["telegram_token"] = "***"
        return data


def _coerce(raw: dict[str, Any]) -> Settings:
    """모르는 키는 버리고, 타입이 어긋난 값은 기본값으로 되돌립니다.

    손으로 고칠 수 있는 파일이라 무엇이든 들어 있을 수 있습니다. 잘못된 설정
    파일 하나로 프로그램이 시작조차 못 하는 것이 더 나쁩니다.
    """
    defaults = Settings()
    values: dict[str, Any] = {}
    for name, default in asdict(defaults).items():
        value = raw.get(name, default)
        if isinstance(default, bool):
            values[name] = bool(value) if isinstance(value, bool) else default
        elif isinstance(default, int) and not isinstance(default, bool):
            # 자릿수가 큰 정수는 그대로 두면 나중에 터집니다 — 감시 시간에
            # 10**400 이 들어오면 ``time.monotonic() + 분*60`` 이 OverflowError
            # 로 새어 나가 감시 시작이 통째로 실패합니다.
            values[name] = (
                value if type(value) is int and 0 <= value <= MAX_INT_SETTING
                else default
            )
        elif isinstance(default, float):
            # bool 은 int 의 하위형이라 그냥 통과합니다. NaN·Infinity 도
            # JSON 이 실어 나릅니다 — NaN 은 모든 ``<`` 비교를 거짓으로 만들어
            # 조회 주기 하한 검사를 통째로 무력화합니다(주기 0 초로 도는 감시).
            values[name] = (
                float(value)
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                else default
            )
        elif isinstance(default, list):
            values[name] = (
                [str(item) for item in value] if isinstance(value, list) else default
            )
        else:
            values[name] = value if isinstance(value, str) else default
    # 값이 정해져 있는 칸은 모르는 글자를 받지 않습니다. 예전에는 손으로 고친
    # transfer_mode 하나가 환승역 필터를 조용히 꺼 버렸습니다.
    if values.get("transfer_mode") not in ("server", "custom"):
        values["transfer_mode"] = defaults.transfer_mode
    if values.get("seat_preference") not in ("any", "general", "special"):
        values["seat_preference"] = defaults.seat_preference
    return Settings(**values)


def load(path: Path | None = None) -> Settings:
    """저장된 설정. 파일이 없거나 깨졌으면 기본값입니다."""
    target = path or settings_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return Settings()
        return _coerce(raw)
    except (OSError, ValueError, TypeError, OverflowError):
        # 손으로 고칠 수 있는 파일입니다. 무엇이 들어 있든 창은 떠야 합니다 —
        # 예전에는 자릿수가 큰 정수 하나가 OverflowError 로 새어 나와 프로그램이
        # 아예 시작하지 못했습니다.
        return Settings()


def save(settings: Settings, path: Path | None = None) -> Path | None:
    """설정을 씁니다. 실패해도 예외를 올리지 않고 ``None`` 을 돌려줍니다.

    저장 실패로 예매가 멈추면 안 됩니다 — 설정은 편의이고 예매가 본론입니다.
    """
    target = path or settings_path()
    handle = None
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(settings), ensure_ascii=False, indent=2)
        # 옆에 새로 쓰고 마지막에 갈아 끼웁니다. 제자리에 덮어쓰면 쓰다가
        # 멈춘 순간 **원래 있던 설정과 토큰까지 함께 잃습니다.**
        descriptor, name = tempfile.mkstemp(
            dir=str(target.parent), prefix=".settings-", suffix=".tmp"
        )
        temporary = Path(name)
        # 만드는 순간부터 소유자 전용입니다. 예전에는 umask 권한으로 만든 뒤에야
        # 좁혔는데, 그 사이에 토큰이 남에게 읽힐 수 있었습니다.
        os.chmod(temporary, SETTINGS_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        os.chmod(target, SETTINGS_FILE_MODE)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        return None
    return target
