"""환자안전사고 보고 (환자안전법 §14) — 접수·상태전이·통계·익명."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.regulatory.safety_service import PatientSafetyService

svc = PatientSafetyService()


async def test_report_and_lifecycle(session):
    inc = await svc.report(session, occurred_on=date(2026, 6, 10), incident_type="ADVERSE",
                           description="투약 오류 — 용량 착오", severity="MODERATE",
                           category="투약", department_id=3, reporter_id=5)
    assert inc.status == "REPORTED" and inc.reporter_id == 5
    await svc.transition(session, incident_id=inc.id, to_status="REVIEWING", actor=9)
    inc2 = await svc.transition(session, incident_id=inc.id, to_status="CLOSED", actor=9,
                                action="라벨 이중확인 절차 도입")
    assert inc2.status == "CLOSED" and inc2.action == "라벨 이중확인 절차 도입"
    assert inc2.reviewed_by == 9


async def test_invalid_transition_blocked(session):
    inc = await svc.report(session, occurred_on=date(2026, 6, 10), incident_type="NEAR_MISS",
                           description="근접오류")
    with pytest.raises(ValidationError, match="상태 전이"):
        await svc.transition(session, incident_id=inc.id, to_status="CLOSED", actor=1)  # REPORTED→CLOSED 불가


async def test_anonymous_report(session):
    inc = await svc.report(session, occurred_on=date(2026, 6, 10), incident_type="SENTINEL",
                           description="적신호사건", reporter_id=None)
    assert inc.reporter_id is None     # 자율보고 익명


async def test_stats(session):
    for t, sev in [("NEAR_MISS", "NONE"), ("ADVERSE", "MILD"), ("ADVERSE", "SEVERE")]:
        await svc.report(session, occurred_on=date(2026, 6, 5), incident_type=t,
                         description="x", severity=sev)
    s = await svc.stats(session, since=date(2026, 6, 1), until=date(2026, 6, 30))
    assert s["total"] == 3 and s["open"] == 3
    assert s["by_type"]["ADVERSE"] == 2 and s["by_type"]["NEAR_MISS"] == 1


# ---- 경위 노출 범위(v1.273 축소) ----

async def test_narrative_hidden_from_non_reviewers(session, client):
    """사고 경위·조치는 검토·종결 권한자(approve)에게만.

    v1.270에서 전 직원에게 열었던 것을 되돌린 회귀. `read`는 STAFF 포함 전 역할이 ALL인데
    경위는 자유서술이라(모델 주석도 "비식별 **권장**") 환자 상황·부서·시각이 그대로 실리고,
    경위+부서+시각이면 보고자도 특정 가능해 `reporter_id`만 가려서는 익명성이 지켜지지 않는다.
    """
    from erp.regulatory.models import PatientSafetyIncident
    from tests._http import auth_as

    session.add(PatientSafetyIncident(
        occurred_on=date(2026, 7, 1), incident_type="ADVERSE", severity="MODERATE",
        description="302호 환자 낙상 — 야간 순회 중 발견", status="REPORTED", reporter_id=7))
    await session.flush()

    staff = await auth_as(session, user_id=61, role="STAFF")
    r = await client.get("/api/v1/regulatory/safety/incidents", headers=staff)
    assert r.status_code == 200, r.text
    it = r.json()["items"][0]
    assert it["description"] is None and it["action"] is None
    assert it["narrative_visible"] is False
    assert "낙상" not in r.text                     # 본문 어디에도 새지 않는다
    assert it["severity"] == "MODERATE"             # 통계용 필드는 그대로

    mgr = await auth_as(session, user_id=62, role="MANAGER")
    it = (await client.get("/api/v1/regulatory/safety/incidents",
                           headers=mgr)).json()["items"][0]
    assert it["narrative_visible"] is True and "낙상" in it["description"]
    assert it["anonymous"] is False and "reporter_id" not in it   # 보고자 id는 누구에게도 안 준다
