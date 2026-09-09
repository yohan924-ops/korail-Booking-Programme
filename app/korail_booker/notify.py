"""텔레그램 알림. 잡았을 때 사용자에게 닿는 유일한 통로입니다.

봇 토큰과 대화 ID 는 화면에서 입력받아 :mod:`korail_booker.settings` 가
사용자 홈에 저장합니다. 이 저장소에는 들어가지 않습니다.

**토큰은 어떤 경로로도 로그에 나가지 않습니다.** 텔레그램 API 는 토큰을 URL
경로에 싣기 때문에 httpx 의 예외 메시지에는 토큰이 통째로 들어 있습니다. 그래서
이 모듈은 예외를 밖으로 흘리지 않고, 남기는 문구는 전부
:func:`mask_token` 을 지납니다.

알림은 부수적입니다. 실패해도 :meth:`TelegramNotifier.send` 는 거짓을 돌려줄
뿐이고, 예약 흐름을 멈추지 않습니다.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT_S = 10.0
#: 텔레그램 한 메시지의 상한은 4096자입니다. 그보다 길면 잘라 보냅니다.
MAX_MESSAGE_CHARS = 4000


def mask_token(text: str, token: str) -> str:
    """문구에서 봇 토큰을 지웁니다. 값을 정확히 알고 지우는 방식입니다."""
    if not token:
        return text
    masked = text.replace(token, "***")
    head = token.split(":", 1)[0]
    # 토큰의 앞부분(봇 ID)만 남은 URL 조각도 지웁니다.
    return masked.replace(head, "***") if head and head != token else masked


@dataclass(frozen=True)
class TelegramConfig:
    """봇 하나와 받을 대화 하나."""

    token: str = ""
    chat_id: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.token.strip() and self.chat_id.strip())


class TelegramNotifier:
    """``sendMessage`` 하나와, 대화 ID 를 찾아 주는 ``getUpdates`` 하나."""

    def __init__(
        self,
        config: TelegramConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        base_url: str = TELEGRAM_API_BASE,
    ) -> None:
        self.config = config
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TelegramNotifier:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _url(self, method: str) -> str:
        return f"{self._base_url}/bot{self.config.token}/{method}"

    def send(self, text: str) -> bool:
        """메시지 한 통. 보냈으면 참.

        예외를 밖으로 내보내지 않습니다 — 알림이 실패했다고 예약을 멈추는 것은
        거꾸로입니다.
        """
        if not self.config.enabled:
            return False
        try:
            response = self._client.post(
                self._url("sendMessage"),
                data={
                    "chat_id": self.config.chat_id,
                    "text": text[:MAX_MESSAGE_CHARS],
                    "disable_web_page_preview": "true",
                },
            )
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        try:
            return bool(response.json().get("ok"))
        except ValueError:
            return False

    def resolve_chat_id(self) -> str | None:
        """봇에게 마지막으로 말을 건 대화의 ID.

        사용자가 자기 chat id 를 알아낼 방법이 없어서 있는 기능입니다 — 봇에게
        아무 메시지나 한 번 보낸 뒤 이것을 부르면 됩니다.
        """
        if not self.config.token.strip():
            return None
        try:
            response = self._client.get(self._url("getUpdates"), params={"limit": 10})
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not payload.get("ok"):
            return None
        updates = payload.get("result")
        if not isinstance(updates, list):
            return None
        for update in reversed(updates):
            if not isinstance(update, dict):
                continue
            for key in ("message", "edited_message", "channel_post"):
                message = update.get(key)
                if not isinstance(message, dict):
                    continue
                chat = message.get("chat")
                if isinstance(chat, dict) and chat.get("id") is not None:
                    return str(chat["id"])
        return None

    def describe_failure(self, exc: Exception) -> str:
        """예외를 토큰 없는 한 줄로. 로그에 쓰는 유일한 통로입니다."""
        return mask_token(f"{type(exc).__name__}: {exc}", self.config.token)
