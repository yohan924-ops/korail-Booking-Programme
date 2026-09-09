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
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


APP_DIR_NAME = "korail-booker"
SETTINGS_FILE_NAME = "settings.json"
#: 소유자만 읽고 쓰기. 토큰이 들어 있는 파일입니다.
SETTINGS_FILE_MODE = 0o600


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
    add_to_cart: bool = False
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
            values[name] = value if type(value) is int else default
        elif isinstance(default, float):
            values[name] = (
                float(value) if isinstance(value, (int, float)) else default
            )
        elif isinstance(default, list):
            values[name] = (
                [str(item) for item in value] if isinstance(value, list) else default
            )
        else:
            values[name] = value if isinstance(value, str) else default
    return Settings(**values)


def load(path: Path | None = None) -> Settings:
    """저장된 설정. 파일이 없거나 깨졌으면 기본값입니다."""
    target = path or settings_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    return _coerce(raw)


def save(settings: Settings, path: Path | None = None) -> Path | None:
    """설정을 씁니다. 실패해도 예외를 올리지 않고 ``None`` 을 돌려줍니다.

    저장 실패로 예매가 멈추면 안 됩니다 — 설정은 편의이고 예매가 본론입니다.
    """
    target = path or settings_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(settings), ensure_ascii=False, indent=2)
        target.write_text(payload, encoding="utf-8")
        # 토큰이 든 파일이라 권한을 좁힙니다. Windows 에서는 chmod 가 사실상
        # 무의미하지만 실패하지도 않습니다.
        os.chmod(target, SETTINGS_FILE_MODE)
    except OSError:
        return None
    return target
