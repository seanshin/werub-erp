"""미래 시점 스위트 점검 — 달력이 지나가면 거짓이 되는 테스트를 **미리** 적발한다.

왜 있나: 고정 날짜로 앵커한 테스트가 실시계를 보는 서비스와 만나면, 통과하던 단언이
어느 날 조용히 거짓이 된다. 두 번 물렸다 —
  · 2026-08-01 `test_recurring_ops` (고정 now vs server_default created_at·F3 게이팅)
  · 2026-08-06 `test_voc` (고정 접수일 vs `due_on < date.today()`) — **한 달간 미인지**
둘 다 "돌 때는 초록이었다". 스위트는 돌 때만 진실이므로, 미래를 미리 돌려 본다.

쓰는 법(상시 CI 레인 아님 — 릴리즈 전이나 분기마다 수동):
    FUTURE_DAYS=180 PYTHONPATH=src:tests/tools pytest -q -p futureclock

⚠ 판정 규율 — 실패가 전부 결함은 아니다.
  이 플러그인은 **파이썬 시계만** 민다. DB의 `server_default=func.now()`는 실시계 그대로라
  둘을 비교하는 코드에는 인위적 스큐가 생긴다(예: `test_narcotics_recent_duplicate_rejected`
  — 중복기입 창을 `datetime.now() - created_at`으로 재니 항상 창 밖). 운영에서는 두 시계가
  같은 호스트라 발생하지 않는 실패이므로 **오탐으로 판정하고 고치지 않는다.**
  실패를 보면 먼저 물을 것: "이건 달력 때문인가, 아니면 내가 시계를 둘로 나눠서인가."

또 하나, 고쳐서는 안 되는 실패가 있다 — `test_current_year_rates_are_registered`.
당해연도 세율·소득세 파라미터 미등록은 **테스트 결함이 아니라 미등록 그 자체**다
(운영에서도 원천징수가 막힌다). 조치는 테스트가 아니라 `hr/statutory.py` 갱신이다.
"""
import os
from datetime import datetime, timedelta

from freezegun import freeze_time

# freezegun의 FakeDate가 걸리기 전에 date를 상속하는 서드파티 클래스를 먼저 임포트해 둔다
# (pydantic.v1.ConstrainedDate가 FakeDate를 베이스로 잡으면 metaclass 충돌로 수집이 죽는다).
import pydantic.v1  # noqa: F401

_days = int(os.environ.get("FUTURE_DAYS", "180"))
_target = datetime.now() + timedelta(days=_days)
# 모듈 상수(`ON = date.today()`)는 수집 시점에 평가되므로 임포트 즉시 건다.
# tick=True — 시간은 정상으로 흐른다(경과시간에 의존하는 테스트의 오탐 축소).
freeze_time(_target, tick=True).start()
print(f"\n[futureclock] 파이썬 시계 {_days}일 이동 → {_target:%Y-%m-%d}")
