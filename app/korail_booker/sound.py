"""화면에서 나는 알림소리 — 창을 트레이에 숨겨 놔도 그대로 납니다.

Tkinter 를 import 하지 않습니다 — ``ui.py``/``tray.py`` 와 같은 이유로,
시험 환경에 tkinter 가 없어도 이 파일은 import 되고 시험할 수 있어야
합니다.

**딩동** 두 음을 울리는 소리 두 가지를 씁니다 — **예약 성공**(밝고 높은
두 음, 짧게)과 **자동 감시 종료**(낮고 차분한 두 음, 조금 느리게). 두
경우를 소리만 듣고 구분할 수 있어야 하므로 음높이도 간격도 다릅니다.
외부 파일을 받아 쓰지 않고 **이 파일이 직접 사인파로 합성**합니다
(:func:`_two_tone_wave_bytes`) — 인터넷도, 저작권 문제도, 추가 패키지도
없습니다.

재생은 플랫폼마다 다릅니다 — 전부 **표준 라이브러리이거나 OS 에 이미
있는 재생기**만 씁니다. 셋 다 **임시 WAV 파일**에 대고 겁니다(메모리
버퍼를 직접 넘기는 방식은 일부 Windows 환경에서 소리가 전혀 안 나는
증상이 실사용에서 보고되어, 더 널리 쓰이는 파일 재생 방식으로 바꿨습니다):

* **Windows**: 표준 라이브러리 ``winsound`` 로, 임시 파일을 비동기로
  겁니다(``SND_FILENAME | SND_ASYNC`` — 재생이 끝나기를 기다리지 않습니다.
  알림소리가 화면을 멈추면 안 됩니다).
* **macOS**: ``afplay``, **Linux**: ``paplay``(없으면 ``aplay``)를 같은
  임시 파일에 대해 비동기로 부릅니다.
* **어느 것도 없으면** 조용히 넘어갑니다 — 소리가 안 나는 것이 자동예매를
  멈추는 것보다 낫습니다. :func:`play_success_sound` 와
  :func:`play_watch_finished_sound` 는 **절대 예외를 던지지 않습니다.**
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
from collections.abc import Callable
from pathlib import Path


#: 전화·게임 알림음과 비슷한 표본화율. 사람 귀에 필요한 대역을 다 담고,
#: 굳이 CD 음질(44100)을 쓸 이유가 없는 짧은 신호음이라 가볍게 갑니다.
SAMPLE_RATE = 22050
#: 소리가 잦아드는 빠르기. 클수록 빨리 사그라듭니다.
DECAY_RATE = 4.0

#: 예약 성공 — 밝고 높은 두 음, 짧고 경쾌하게('딩동').
_SUCCESS_NOTES_HZ = (1318.51, 987.77)  # E6, B5
_SUCCESS_NOTE_S = 0.32
_SUCCESS_GAP_S = 0.11

#: 자동 감시 종료 — 낮고 차분한 두 음, 조금 느리게. 성공음과 음높이·
#: 간격이 달라 듣기만 해도 구분됩니다.
_FINISHED_NOTES_HZ = (783.99, 587.33)  # G5, D5
_FINISHED_NOTE_S = 0.42
_FINISHED_GAP_S = 0.16

#: 매번 새 파일을 만들면(미리듣기를 여러 번 눌렀을 때 등) 임시 폴더에 계속
#: 쌓이므로, 소리마다 한 번 만든 파일을 이 프로세스가 사는 동안 그대로
#: 다시 씁니다.
_cached_wav_paths: dict[str, Path] = {}


def _tone_samples(freq_hz: float, duration_s: float) -> list[float]:
    """사인파 한 음을 지수적으로 잦아들게. -1.0 ~ 1.0 사이 값들."""
    frame_count = int(SAMPLE_RATE * duration_s)
    samples = []
    for index in range(frame_count):
        t = index / SAMPLE_RATE
        envelope = math.exp(-DECAY_RATE * t)
        samples.append(envelope * math.sin(2 * math.pi * freq_hz * t))
    return samples


def _two_tone_wave_bytes(
    notes_hz: tuple[float, float], note_s: float, gap_s: float
) -> bytes:
    """"딩동" — 두 음을 순서대로, 뒤 음이 앞 음 꼬리에 살짝 겹치게 웁니다.

    모노 16비트 PCM WAV 바이트를 돌려줍니다.
    """
    first, second = notes_hz
    tail = _tone_samples(first, note_s)
    head = _tone_samples(second, note_s)
    start_second = int(SAMPLE_RATE * gap_s)
    total = start_second + len(head)
    mixed = [0.0] * total
    for i, value in enumerate(tail):
        mixed[i] += value
    for i, value in enumerate(head):
        mixed[start_second + i] += value
    samples = bytearray()
    for value in mixed:
        clamped = max(-1.0, min(1.0, value))
        samples += struct.pack("<h", int(clamped * 32767))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(samples))
    return buffer.getvalue()


def _success_wave_bytes() -> bytes:
    return _two_tone_wave_bytes(_SUCCESS_NOTES_HZ, _SUCCESS_NOTE_S, _SUCCESS_GAP_S)


def _watch_finished_wave_bytes() -> bytes:
    return _two_tone_wave_bytes(
        _FINISHED_NOTES_HZ, _FINISHED_NOTE_S, _FINISHED_GAP_S
    )


def _wav_file_path(kind: str, make_bytes: Callable[[], bytes]) -> Path | None:
    """이 소리를 재생기에 넘길 임시 WAV 파일. 실패하면 ``None``."""
    cached = _cached_wav_paths.get(kind)
    if cached is not None and cached.exists():
        return cached
    try:
        data = make_bytes()
        handle = tempfile.NamedTemporaryFile(
            prefix=f"newrail-{kind}-", suffix=".wav", delete=False
        )
        handle.write(data)
        handle.close()
    except OSError:
        return None
    path = Path(handle.name)
    _cached_wav_paths[kind] = path
    return path


def _play(kind: str, make_bytes: Callable[[], bytes]) -> None:
    """알림소리 하나를 냅니다. **절대 예외를 던지지 않습니다** — 소리가
    안 나는 것이 자동예매를 멈추는 것보다 낫습니다. 재생 자체도 비동기라
    화면을 멈추지 않습니다(끝나기를 기다리지 않습니다).
    """
    try:
        path = _wav_file_path(kind, make_bytes)
        if path is None:
            return
        if sys.platform == "win32":
            _play_windows(path)
        else:
            _play_external_player(path)
    except Exception:
        return


def play_success_sound() -> None:
    """예약 성공 알림소리(딩동, 밝고 높은 두 음)."""
    _play("success", _success_wave_bytes)


def play_watch_finished_sound() -> None:
    """자동 감시 종료 알림소리(딩동, 낮고 차분한 두 음) — 잡았든, 시간이
    끝났든, 사람이 멈췄든, 실패했든 감시 묶음 하나가 끝날 때마다 납니다.
    """
    _play("watch-finished", _watch_finished_wave_bytes)


def _play_windows(path: Path) -> None:
    import winsound  # 윈도우가 아니면 이 모듈 자체가 없습니다 — 그래서 여기서만.

    # pyright 는 이 저장소가 잡는 플랫폼(Linux)의 typeshed 스텁 기준으로
    # winsound 를 보므로, 윈도우 전용 이름은 정적으로 "없다" 고 봅니다 —
    # 실제로는 윈도우에서 이 함수 자체가 그 플랫폼에서만 불립니다.
    #
    # 메모리 버퍼(SND_MEMORY)를 직접 넘기던 예전 방식은 실사용에서 "소리가
    # 전혀 안 난다" 는 신고가 있었습니다. 파일 경로(SND_FILENAME)로 바꿨고,
    # SND_NODEFAULT 로 이 파일을 못 틀면 윈도우 기본 소리로 대신 울리지
    # 않고 조용히 넘어가게 했습니다 — 엉뚱한 소리보다는 무음이 낫습니다.
    winsound.PlaySound(  # type: ignore[attr-defined]
        str(path),
        winsound.SND_FILENAME  # type: ignore[attr-defined]
        | winsound.SND_ASYNC  # type: ignore[attr-defined]
        | winsound.SND_NODEFAULT,  # type: ignore[attr-defined]
    )


def _play_external_player(path: Path) -> None:
    player = shutil.which("afplay") if sys.platform == "darwin" else None
    if player is None:
        player = shutil.which("paplay") or shutil.which("aplay")
    if player is None:
        return
    subprocess.Popen(
        [player, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
