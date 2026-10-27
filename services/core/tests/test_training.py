"""원내 교육이수(Tier3·0228) — 과정·회차·이수 멱등·주기 만료·현황 매트릭스·대상 판정·HTTP."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.hr.models import Employee
from erp.hr.training_service import TrainingService, _add_months

svc = TrainingService()


async def _emp(session, no, job=None, dept=None):
    e = Employee(employee_no=no, name=f"직원{no}", hired_on=date(2025, 1, 1),
                 job_group="STAFF", employment_type="REGULAR", source="ERP",
                 job_category=job, department_id=dept)
    session.add(e)
    await session.flush()
    return e


def test_add_months_clamps_month_end():
    assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert _add_months(date(2026, 1, 15), 12) == date(2027, 1, 15)


async def test_course_session_completion_cycle(session):
    """과정(주기12M)→회차→이수: valid_until=이수+12M·(회차,직원) 멱등."""
    e = await _emp(session, "T1")
    c = await svc.create_course(session, actor=1, code="CPR", title="심폐소생술",
                                category="SAFETY", cycle_months=12)
    s = await svc.create_session(session, actor=1, course_id=c.id, held_on=date(2026, 3, 1),
                                 hours=2, instructor="김강사")
    rec = await svc.record_completion(session, actor=1, session_id=s.id, employee_id=e.id,
                                      score=95)
    assert rec.valid_until == date(2027, 3, 1) and float(rec.score) == 95
    again = await svc.record_completion(session, actor=1, session_id=s.id, employee_id=e.id)
    assert again.id == rec.id                            # 멱등


async def test_one_off_course_no_expiry(session):
    e = await _emp(session, "T2")
    c = await svc.create_course(session, actor=1, code="ORI", title="신규자오리엔테이션",
                                category="ONBOARDING", cycle_months=0)
    s = await svc.create_session(session, actor=1, course_id=c.id, held_on=date(2026, 2, 1))
    rec = await svc.record_completion(session, actor=1, session_id=s.id, employee_id=e.id)
    assert rec.valid_until is None                       # 일회성=만료 없음


async def test_status_matrix_states_and_target(session):
    """상태: NONE/OK/OVERDUE + 직종 대상 판정(N/A)."""
    nurse = await _emp(session, "N1", job="NURSE")
    await _emp(session, "R1", job="RADIOTECH")            # 대상 아님(N/A 확인용)
    # 간호 직무교육(간호사 대상만)
    c = await svc.create_course(session, actor=1, code="NURSEJOB", title="간호직무",
                                category="JOB", cycle_months=12, target="JOB_CATEGORY",
                                target_value="NURSE")
    s = await svc.create_session(session, actor=1, course_id=c.id, held_on=date(2025, 1, 1))
    # 간호사: 만료(2025-01+12M=2026-01 < as_of)
    await svc.record_completion(session, actor=1, session_id=s.id, employee_id=nurse.id)
    out = await svc.status(session, as_of=date(2026, 6, 1))
    cells = {i["employee_no"]: i["cells"]["NURSEJOB"]["state"] for i in out["items"]}
    assert cells["N1"] == "OVERDUE"                       # 만료
    assert cells["R1"] == "N/A"                           # 방사선사=대상 아님
    # 최신 회차 이수 시 OK
    s2 = await svc.create_session(session, actor=1, course_id=c.id, held_on=date(2026, 5, 1))
    await svc.record_completion(session, actor=1, session_id=s2.id, employee_id=nurse.id)
    out2 = await svc.status(session, as_of=date(2026, 6, 1))
    assert {i["employee_no"]: i["cells"]["NURSEJOB"]["state"]
            for i in out2["items"]}["N1"] == "OK"
    summ = {x["code"]: x for x in out2["summary"]}["NURSEJOB"]
    assert summ["required"] == 1 and summ["compliant"] == 1   # 대상=간호사 1명


async def test_create_course_validation(session):
    with pytest.raises(ValidationError):                  # 대상 한정인데 값 없음
        await svc.create_course(session, actor=1, code="X", title="x",
                                target="JOB_CATEGORY")
    await svc.create_course(session, actor=1, code="DUP", title="중복")
    with pytest.raises(ValidationError):
        await svc.create_course(session, actor=1, code="DUP", title="중복2")


async def test_http_bulk_and_status(session, client):
    from tests._http import auth_as, bearer, make_user
    headers = await auth_as(session, user_id=1, role="ADMIN")
    e1 = await _emp(session, "H1")
    e2 = await _emp(session, "H2")
    r = await client.post("/api/v1/hr/training/courses", headers=headers,
                          json={"code": "INF", "title": "감염관리", "category": "INFECTION",
                                "cycle_months": 12})
    cid = r.json()["id"]
    r = await client.post(f"/api/v1/hr/training/courses/{cid}/sessions", headers=headers,
                          json={"held_on": "2026-06-01", "hours": 1})
    sid = r.json()["id"]
    r = await client.post(f"/api/v1/hr/training/sessions/{sid}/completions/bulk", headers=headers,
                          json={"employee_ids": [e1.id, e2.id]})
    assert r.status_code == 200 and r.json()["added"] == 2
    # 재실행 멱등(added 0)
    r = await client.post(f"/api/v1/hr/training/sessions/{sid}/completions/bulk", headers=headers,
                          json={"employee_ids": [e1.id, e2.id]})
    assert r.json()["added"] == 0
    r = await client.get("/api/v1/hr/training/status", headers=headers)
    assert r.status_code == 200
    # STAFF는 write 불가
    await make_user(session, user_id=9, role="STAFF")
    r = await client.post("/api/v1/hr/training/courses", headers=bearer(user_id=9, role="STAFF"),
                          json={"code": "Z", "title": "z"})
    assert r.status_code == 403
