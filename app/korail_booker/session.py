"""클라이언트를 만들고 로그인합니다 — 화면이 쓰는 얇은 층.

로그인 입력은 **아이디·휴대폰번호·회원번호 아무거나** 됩니다. 무엇인지 고르는
것은 라이브러리의 ``infer_login_input_flag`` 가 값의 모양을 보고 합니다
(``session.py``), 그래서 화면에 종류를 고르는 칸이 필요 없습니다.

**비회원 예매는 없습니다.** 예약 라우트가 로그인 세션을 요구하고
(``client.reserve``), 예약 폼 자체가 회원 전용입니다
(``ReservationRequest.java:105-119``). 로그인 없이 되는 것은 조회까지입니다.
"""

from __future__ import annotations

import os
import threading
import time

from korail_mobile_api import (
    KorailAuthContinuationRequired,
    KorailAuthError,
    KorailClient,
    KorailConfig,
    KorailSession,
)
from korail_mobile_api.live import build_config_from_env


DEVICE_ID_ENV = "KORAIL_DYNAPATH_DEVICE_ID"
OS_VERSION_ENV = "KORAIL_DYNAPATH_OS_VERSION"
DEVICE_MODEL_ENV = "KORAIL_DYNAPATH_DEVICE_MODEL"

#: 요청 사이의 최소 간격. KORAIL 은 매크로성 트래픽에 IP 를 막습니다. 화면에는
#: 이 값을 내리는 칸이 없습니다.
MIN_REQUEST_INTERVAL_S = 1.5


class Pacer:
    """요청 사이의 최소 간격을 강제합니다. **스레드 여럿이 같이 씁니다.**

    이 프로그램은 클라이언트 하나를 조회 스레드와 감시 스레드 여럿이 나눠
    씁니다. 잠금 없이 재고 자면, 스레드 N 개가 같은 ``_last`` 를 읽고 동시에
    깨어나 요청 N 개를 한꺼번에 쏩니다 — 지키려던 1.5초 바닥이 그 순간
    사라집니다. 그 바닥은 KORAIL 이 매크로성 트래픽에 IP 를 막기 때문에
    있는 것입니다.

    잠금을 잡은 채로 잡니다. 그래야 다음 스레드가 **내가 보낸 시각 이후로**
    다시 재고, 간격이 정말 직렬로 지켜집니다.
    """

    def __init__(self, min_interval_s: float = MIN_REQUEST_INTERVAL_S) -> None:
        self.min_interval_s = min_interval_s
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._last is not None:
                remaining = self.min_interval_s - (now - self._last)
                if remaining > 0:
                    time.sleep(remaining)
            self._last = time.monotonic()


def install_pacing(client: KorailClient, pacer: Pacer | None = None) -> Pacer:
    """클라이언트가 스스로 부르는 요청까지 포함해 전부 늦춥니다."""
    installed = pacer or Pacer()
    inner = client.http._client
    hooks = dict(inner.event_hooks)
    hooks["request"] = [*hooks.get("request", []), lambda request: installed.wait()]
    inner.event_hooks = hooks
    return installed


def build_config() -> tuple[KorailConfig, str]:
    """기기 신원. 환경변수 셋이 다 있으면 그 값, 하나도 없으면 합성 값입니다."""
    values = {
        name: os.environ.get(name, "")
        for name in (DEVICE_ID_ENV, OS_VERSION_ENV, DEVICE_MODEL_ENV)
    }
    present = [name for name, value in values.items() if value]
    if len(present) == len(values):
        return build_config_from_env(), "환경변수의 실기기 값"
    if present:
        missing = ", ".join(name for name, value in values.items() if not value)
        raise ValueError(f"기기 값은 셋을 다 주거나 다 비워야 합니다. 빠진 것: {missing}")
    return KorailConfig(enable_dynapath=True), "합성 기기 값"


def build_client() -> tuple[KorailClient, str]:
    """페이싱까지 걸린 클라이언트 하나."""
    config, identity = build_config()
    client = KorailClient(config)
    install_pacing(client)
    return client, identity


def login(client: KorailClient, member_no: str, password: str) -> KorailSession:
    """아이디·휴대폰번호·회원번호 중 무엇으로든 로그인합니다.

    2단계 인증이 필요한 계정은 여기서 멈춥니다 — WebView 후속 인증은 이
    프로그램의 범위 밖입니다.
    """
    if not member_no.strip() or not password:
        raise KorailAuthError("아이디와 비밀번호를 모두 입력하세요")
    try:
        return client.login(member_no.strip(), password)
    except KorailAuthContinuationRequired as exc:
        raise KorailAuthError(
            "이 계정은 앱에서 추가 인증을 요구합니다. 코레일 앱으로 먼저 "
            "로그인해 인증을 마친 뒤 다시 시도하세요."
        ) from exc
