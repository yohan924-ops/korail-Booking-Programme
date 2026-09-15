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

import re
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


#: 대화 ID 는 정수입니다. 그룹은 앞에 ``-`` 가 붙습니다.
#:
#: ``@이름`` 은 이 칸의 모양이 **아닙니다.** 텔레그램이 ``@이름`` 을 받는 것은
#: 채널과 슈퍼그룹뿐이고, 봇 자신의 이름은 대화 상대가 될 수 없습니다.
CHAT_ID_RE = re.compile(r"-?[0-9]+")


def looks_like_chat_id(value: str) -> bool:
    return CHAT_ID_RE.fullmatch(value.strip()) is not None


def _chat_title(chat: dict[str, object]) -> str:
    """대화를 사람이 알아보는 이름으로. 없으면 빈 문자열."""
    for key in ("title", "username", "first_name"):
        value = chat.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@dataclass(frozen=True)
class ResolvedChat:
    """``getUpdates`` 에서 찾아낸 대화 하나."""

    chat_id: str
    title: str = ""


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
        #: 텔레그램이 마지막으로 알려 준 거절 사유. 없으면 빈 문자열입니다.
        #: 화면이 "실패했습니다" 뒤에 이것을 붙여 줍니다 — 그것이 없으면
        #: 사람은 무엇을 고쳐야 할지 알 수 없습니다.
        self.last_error = ""

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TelegramNotifier:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _url(self, method: str) -> str:
        # 앞뒤 공백을 뗍니다. 다른 모든 검사는 ``token.strip()`` 을 보는데
        # 주소만 원문을 쓰면, 설정 파일에 줄바꿈이 끼어든 토큰이 검사는
        # 통과하고 요청만 조용히 실패합니다.
        return f"{self._base_url}/bot{self.config.token.strip()}/{method}"

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, object] | None:
        """응답 본문을 사전으로. 사전이 아니면 ``None``.

        200 인데 본문이 배열이나 문자열인 경우가 있습니다(중간의 프록시가
        끼어들면 그렇습니다). ``.get`` 을 바로 부르면 AttributeError 가 새어
        나가 알림 경로 전체가 죽습니다.
        """
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

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
        except Exception:
            # httpx.InvalidURL 은 HTTPError 가 아닙니다. 토큰에 줄바꿈이
            # 하나 끼면 그것이 여기서 새어 나가 그날 밤 알림이 통째로
            # 사라졌습니다. 알림 실패가 예약을 죽이면 안 됩니다.
            return False
        if response.status_code != 200:
            return False
        payload = self._body(response)
        if payload is None:
            return False
        if not payload.get("ok"):
            # 텔레그램이 왜 거절했는지는 여기에만 옵니다. 버리면 사람은
            # "안 와요" 말고는 아무 단서도 못 얻습니다.
            self.last_error = str(payload.get("description") or "")[:200]
            return False
        return True

    def bot_username(self) -> str | None:
        """봇의 아이디(``@`` 없이). 토큰이 맞는지 확인하는 가장 싼 방법입니다.

        ``getMe`` 는 토큰만 있으면 되고 아무것도 바꾸지 않습니다. 이것이
        돌아오면 토큰은 맞는 것이고, 남은 문제는 대화 ID 뿐입니다 — 그 둘을
        갈라 주지 않으면 "왜 안 되는지" 를 사람이 짚을 수 없습니다.
        """
        if not self.config.token.strip():
            return None
        try:
            response = self._client.get(self._url("getMe"))
        except Exception:
            return None
        if response.status_code != 200:
            return None
        payload = self._body(response)
        if payload is None or not payload.get("ok"):
            if payload is not None:
                self.last_error = str(payload.get("description") or "")[:200]
            return None
        result = payload.get("result")
        if not isinstance(result, dict):
            return None
        name = result.get("username")
        return name if isinstance(name, str) and name else None

    def resolve_chat(self) -> ResolvedChat | None:
        """봇에게 마지막으로 말을 건 **대화**. 번호와 이름을 함께 돌려줍니다.

        사용자가 자기 chat id 를 알아낼 방법이 없어서 있는 기능입니다 — 봇에게
        아무 메시지나 한 번 보낸 뒤 이것을 부르면 됩니다.

        이름까지 읽는 이유: 돌아오는 것이 숫자 하나뿐이면 그 숫자가 무엇인지
        알 수 없습니다. "내 계정"이라고 말해 주어야 사람이 납득합니다.
        """
        if not self.config.token.strip():
            return None
        try:
            # ``offset=-1`` 이 **가장 마지막 것 하나**를 줍니다. ``limit`` 만
            # 주면 텔레그램은 밀린 것 중 **가장 오래된** 쪽부터 돌려주므로,
            # 밀린 메시지가 많으면 엉뚱한 옛 대화의 번호를 채워 넣습니다.
            response = self._client.get(
                self._url("getUpdates"), params={"offset": -1, "limit": 1}
            )
        except Exception:
            return None
        if response.status_code != 200:
            return None
        payload = self._body(response)
        if payload is None or not payload.get("ok"):
            if payload is not None:
                self.last_error = str(payload.get("description") or "")[:200]
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
                    return ResolvedChat(
                        chat_id=str(chat["id"]),
                        title=_chat_title(chat),
                    )
        return None

    def resolve_chat_id(self) -> str | None:
        """:meth:`resolve_chat` 의 번호만."""
        found = self.resolve_chat()
        return found.chat_id if found is not None else None

    def describe_failure(self, exc: Exception) -> str:
        """예외를 토큰 없는 한 줄로. 로그에 쓰는 유일한 통로입니다."""
        return mask_token(f"{type(exc).__name__}: {exc}", self.config.token)
