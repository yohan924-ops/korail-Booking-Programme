"""예약이 잡히면 나는 알림 소리 — 창을 트레이에 숨겨 놔도 그대로 납니다.

Tkinter 를 import 하지 않습니다 — ``ui.py``/``tray.py`` 와 같은 이유로,
시험 환경에 tkinter 가 없어도 이 파일은 import 되고 시험할 수 있어야
합니다.

소리 자체는 외부 파일을 받아 쓰지 않고 **이 파일이 직접 만듭니다** —
사인파 두 개(기본음 하나, 배음 하나)를 지수적으로 잦아들게 섞어 짧은
"맑은 종소리" 를 WAV 로 합성합니다(:func:`_bell_wave_bytes`). 그래서
인터넷에서 소리 파일을 받아 오지 않고, 저작권 문제도, 추가 패키지도
없습니다.

재생은 플랫폼마다 다릅니다 — 전부 **표준 라이브러리이거나 OS 에 이미
있는 재생기**만 씁니다:

* **Windows**: 표준 라이브러리 ``winsound`` 로, 메모리 위의 WAV 바이트를
  바로 겁니다(``SND_MEMORY | SND_ASYNC`` — 재생이 끝나기를 기다리지
  않습니다. 알림 소리가 화면을 멈추면 안 됩니다).
* **macOS**: ``afplay``, **Linux**: ``paplay``(없으면 ``aplay``)를 임시
  WAV 파일에 대해 비동기로 부릅니다.
* **어느 것도 없으면** 조용히 넘어갑니다 — 소리가 안 나는 것이 자동예매를
  멈추는 것보다 낫습니다. :func:`play_success_chime` 은 **절대 예외를
  던지지 않습니다.**
"""

from __future__ import annotations

import io
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path


#: 전화·게임 알림음과 비슷한 표본화율. 사람 귀에 필요한 대역을 다 담고,
#: 굳이 CD 음질(44100)을 쓸 이유가 없는 짧은 신호음이라 가볍게 갑니다.
SAMPLE_RATE = 22050
#: 종이 울리는 길이. 너무 길면 "빨리 확인하라" 는 알림 소리로는 늘어지고,
#: 너무 짧으면 다른 소리에 묻힙니다.
DURATION_S = 0.9
#: 기본음 — A6. "맑다" 는 인상은 낮은 음보다 이 대역에서 더 잘 삽니다.
FUNDAMENTAL_HZ = 1318.5
#: 배음. 실제 종의 배음비(대략 2.4배, 관악기의 정수배와 다릅니다)를
#: 흉내 내 "종소리" 느낌을 냅니다 — 배음 없이 사인파 하나만 틀면
#: 전자음처럼 들립니다.
OVERTONE_RATIO = 2.4
#: 소리가 잦아드는 빠르기. 클수록 빨리 사그라듭니다.
DECAY_RATE = 3.5

#: macOS/Linux 는 임시 파일에 대고 외부 재생기를 부릅니다. 매번 새 파일을
#: 만들면([미리듣기]를 여러 번 눌렀을 때 등) 임시 폴더에 계속 쌓이므로,
#: 한 번 만든 파일을 이 프로세스가 사는 동안 그대로 다시 씁니다.
_cached_wav_path: Path | None = None


def _bell_wave_bytes() -> bytes:
    """짧은 종소리 WAV 바이트. 모노 16비트 PCM입니다."""
    frame_count = int(SAMPLE_RATE * DURATION_S)
    samples = bytearray()
    for index in range(frame_count):
        t = index / SAMPLE_RATE
        envelope = math.exp(-DECAY_RATE * t)
        value = envelope * (
            0.7 * math.sin(2 * math.pi * FUNDAMENTAL_HZ * t)
            + 0.3 * math.sin(2 * math.pi * FUNDAMENTAL_HZ * OVERTONE_RATIO * t)
        )
        clamped = max(-1.0, min(1.0, value))
        samples += struct.pack("<h", int(clamped * 32767))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(samples))
    return buffer.getvalue()


def _wav_file_path() -> Path | None:
    """macOS/Linux 재생기에 넘길 임시 WAV 파일. 실패하면 ``None``."""
    global _cached_wav_path
    if _cached_wav_path is not None and _cached_wav_path.exists():
        return _cached_wav_path
    try:
        data = _bell_wave_bytes()
        handle = tempfile.NamedTemporaryFile(
            prefix="newrail-chime-", suffix=".wav", delete=False
        )
        handle.write(data)
        handle.close()
    except OSError:
        return None
    _cached_wav_path = Path(handle.name)
    return _cached_wav_path


def play_success_chime() -> None:
    """예약 성공 종소리를 냅니다. **절대 예외를 던지지 않습니다** — 소리가
    안 나는 것이 자동예매를 멈추는 것보다 낫습니다. 재생 자체도 비동기라
    화면을 멈추지 않습니다(끝나기를 기다리지 않습니다).
    """
    try:
        if sys.platform == "win32":
            _play_windows()
        else:
            _play_external_player()
    except Exception:
        return


def _play_windows() -> None:
    import winsound  # 윈도우가 아니면 이 모듈 자체가 없습니다 — 그래서 여기서만.

    # pyright 는 이 저장소가 잡는 플랫폼(Linux)의 typeshed 스텁 기준으로
    # winsound 를 보므로, 윈도우 전용 이름은 정적으로 "없다" 고 봅니다 —
    # 실제로는 윈도우에서 이 함수 자체가 그 플랫폼에서만 불립니다.
    winsound.PlaySound(  # type: ignore[attr-defined]
        _bell_wave_bytes(),
        winsound.SND_MEMORY | winsound.SND_ASYNC,  # type: ignore[attr-defined]
    )


def _play_external_player() -> None:
    player = shutil.which("afplay") if sys.platform == "darwin" else None
    if player is None:
        player = shutil.which("paplay") or shutil.which("aplay")
    if player is None:
        return
    path = _wav_file_path()
    if path is None:
        return
    subprocess.Popen(
        [player, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
