"""재무 시뮬레이션 시나리오 CRUD — 사용자 소유 스코프·동일이름 upsert·드라이버 검증."""
import pytest
from pydantic import ValidationError

from erp.analytics.router import DriversModel
from erp.analytics.sim_scenario_service import SimScenarioService

svc = SimScenarioService()
DR = {"growth": 0.01, "varRatio": 0.3, "fixedDelta": 0, "taxRate": 0,
      "capex": 0, "loan": 0, "seasonal": True}


async def test_upsert_list_delete(session):
    a = await svc.upsert(session, user_id=1, name="공격적", drivers=DR)
    assert a["name"] == "공격적" and a["drivers"]["seasonal"] is True
    # 동일 이름 → upsert(중복 생성 X, 값 갱신)
    a2 = await svc.upsert(session, user_id=1, name="공격적", drivers={**DR, "growth": 0.05})
    assert a2["id"] == a["id"] and a2["drivers"]["growth"] == 0.05
    rows = await svc.list(session, user_id=1)
    assert len(rows) == 1
    # 타 사용자 격리
    await svc.upsert(session, user_id=2, name="보수적", drivers=DR)
    assert len(await svc.list(session, user_id=1)) == 1
    assert len(await svc.list(session, user_id=2)) == 1
    # 삭제 — 본인 것만
    assert await svc.delete(session, user_id=1, sid=a["id"]) is True
    assert await svc.delete(session, user_id=2, sid=a["id"]) is False  # 이미 없음/타인
    assert await svc.list(session, user_id=1) == []


async def test_empty_name_rejected(session):
    with pytest.raises(ValueError):
        await svc.upsert(session, user_id=1, name="  ", drivers=DR)


async def test_name_sanitized(session):
    """이름의 꺾쇠·제어문자 제거(보고서 직렬화 주입 방지)."""
    r = await svc.upsert(session, user_id=3, name="<script>공격</script>\t", drivers=DR)
    assert "<" not in r["name"] and ">" not in r["name"] and "\t" not in r["name"]
    assert "공격" in r["name"]


def test_drivers_model_validates():
    """드라이버 검증 — 정상 통과·미지 키 거부·범위 초과 거부(NaN/남용 차단)."""
    ok = DriversModel(growth=0.01, varRatio=0.3, fixedDelta=0, taxRate=0.1,
                      capex=0, loan=0, seasonal=True)
    assert ok.varRatio == 0.3
    with pytest.raises(ValidationError):                  # 미지 키
        DriversModel(growth=0, varRatio=0.3, fixedDelta=0, taxRate=0,
                     capex=0, loan=0, seasonal=False, evil="x")
    with pytest.raises(ValidationError):                  # 범위 초과(변동비율>1)
        DriversModel(growth=0, varRatio=5, fixedDelta=0, taxRate=0,
                     capex=0, loan=0, seasonal=False)
    with pytest.raises(ValidationError):                  # 타입 오류
        DriversModel(growth="abc", varRatio=0.3, fixedDelta=0, taxRate=0,
                     capex=0, loan=0, seasonal=False)
