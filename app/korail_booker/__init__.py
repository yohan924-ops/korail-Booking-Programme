"""코레일 예매 GUI — 조회하고, 고르고, 자리가 열리면 한 번 잡습니다.

이 패키지는 배포되지 않습니다. ``src/korail_mobile_api`` 가 라이브러리이고
여기는 그것을 쓰는 프로그램입니다. 실행은 저장소에서 바로 합니다::

    python3 app/main.py

층이 나뉘어 있습니다. :mod:`~korail_booker.journeys` 는 계산만 하고,
:mod:`~korail_booker.search` 와 :mod:`~korail_booker.autobook` 은 클라이언트를
인자로 받고, Tkinter 를 import 하는 곳은 :mod:`~korail_booker.ui` 하나입니다.
그래서 화면 없이도 시험됩니다(``tests/test_korail_booker.py``).

무엇을 하지 않는지가 더 중요합니다.

* **결제하지 않습니다.** 결제 범주의 consent 를 만드는 코드가 없습니다.
* **비회원 예매를 하지 않습니다.** 라이브러리의 예약 경로가 회원 전용입니다.
* **잡으면 멈춥니다.** 재시도한 예약은 중복 예약입니다.
"""

from __future__ import annotations
