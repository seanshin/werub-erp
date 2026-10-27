"""감염관리 지표 (인증평가·P3-1) — 비율 산출·목표대비 상태·멱등 upsert·대시보드."""
import pytest

from erp.errors import ValidationError
from erp.regulatory.infection_service import InfectionService

svc = InfectionService()


async def test_rate_and_status_lower_better(session):
    # HAI 요로감염 밀도 = 3/1500×1000 = 2.0, 목표 1.5 초과 → ALERT(>1.1배)
    out = await svc.record(session, period="2026-06", indicator_type="HAI_UTI",
                           numerator=3, denominator=1500, multiplier=1000, target=1.5)
    assert out["rate"] == 2.0 and out["status"] == "ALERT"
    # 목표 이내(2/1500×1000=1.33 ≤ 1.5) → GOOD
    out2 = await svc.record(session, period="2026-07", indicator_type="HAI_UTI",
                            numerator=2, denominator=1500, multiplier=1000, target=1.5)
    assert out2["rate"] == 1.33 and out2["status"] == "GOOD"


async def test_status_higher_better_hand_hygiene(session):
    # 손위생 수행률 = 85/100×100 = 85%, 목표 90 → 미달이나 90%의 90%=81 이상 → WARN
    out = await svc.record(session, period="2026-06", indicator_type="HAND_HYGIENE",
                           numerator=85, denominator=100, multiplier=100, target=90)
    assert out["rate"] == 85.0 and out["status"] == "WARN"
    # 92% → 목표 달성 GOOD
    out2 = await svc.record(session, period="2026-07", indicator_type="HAND_HYGIENE",
                            numerator=92, denominator=100, multiplier=100, target=90)
    assert out2["status"] == "GOOD"
    # 70% → ALERT(목표의 90% 미만)
    out3 = await svc.record(session, period="2026-08", indicator_type="HAND_HYGIENE",
                            numerator=70, denominator=100, multiplier=100, target=90)
    assert out3["status"] == "ALERT"


async def test_upsert_idempotent_same_key(session):
    await svc.record(session, period="2026-06", indicator_type="MDRO", numerator=5,
                     denominator=1000, multiplier=1000)
    await svc.record(session, period="2026-06", indicator_type="MDRO", numerator=8,
                     denominator=1000, multiplier=1000)   # 같은 키 → 갱신
    rows = await svc.list_indicators(session, indicator_type="MDRO")
    assert len(rows) == 1 and rows[0]["numerator"] == 8.0


async def test_guards(session):
    with pytest.raises(ValidationError):
        await svc.record(session, period="2026-06", indicator_type="BAD", numerator=1,
                         denominator=1)
    with pytest.raises(ValidationError):
        await svc.record(session, period="2026-06", indicator_type="HAI_BSI", numerator=1,
                         denominator=0)     # 분모 0
    with pytest.raises(ValidationError):
        await svc.record(session, period="202606", indicator_type="HAI_BSI", numerator=1,
                         denominator=10)    # 잘못된 기간형식


async def test_dashboard_alerts(session):
    await svc.record(session, period="2026-06", indicator_type="HAI_BSI", numerator=4,
                     denominator=1000, multiplier=1000, target=1.0)   # 4.0 > 1.1 → ALERT
    await svc.record(session, period="2026-06", indicator_type="HAND_HYGIENE", numerator=95,
                     denominator=100, multiplier=100, target=90)      # GOOD
    dash = await svc.dashboard(session, period="2026-06")
    assert dash["alert_count"] == 1
    assert {i["indicator_type"] for i in dash["indicators"]} == {"HAI_BSI", "HAND_HYGIENE"}
    assert dash["alerts"][0]["indicator_type"] == "HAI_BSI"


async def test_dept_scoping_distinct_rows(session):
    # 같은 기간·유형이라도 부서별 별도 행(dept NULL=기관 전체)
    await svc.record(session, period="2026-06", indicator_type="HAI_SSI", numerator=2,
                     denominator=200, department_id=None)
    await svc.record(session, period="2026-06", indicator_type="HAI_SSI", numerator=1,
                     denominator=80, department_id=7)
    rows = await svc.list_indicators(session, indicator_type="HAI_SSI")
    assert len(rows) == 2
