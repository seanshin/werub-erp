"""인사평가(Tier3·0229) — 차수/항목·배정·자기평가·채점·확정(가중 총점·등급)·행권한·HTTP."""
from datetime import date

import pytest

from erp.common.policy import Role, require_scope
from erp.common.router import CurrentUser
from erp.errors import ForbiddenError, ValidationError
from erp.hr.evaluation_service import EvaluationService, _grade_for
from erp.hr.models import Employee

svc = EvaluationService()


async def _emp(session, no, dept=None):
    e = Employee(employee_no=no, name=f"직원{no}", hired_on=date(2025, 1, 1),
                 job_group="STAFF", employment_type="REGULAR", source="ERP", department_id=dept)
    session.add(e)
    await session.flush()
    return e


def test_grade_bands():
    assert _grade_for(95, None) == "S" and _grade_for(72, None) == "B" and _grade_for(40, None) == "D"


async def _cycle_with_criteria(session):
    c = await svc.create_cycle(session, actor=1, period="2026", title="2026 연간평가")
    a = await svc.add_criterion(session, actor=1, cycle_id=c.id, title="직무역량",
                                kind="COMPETENCY", weight=6, max_score=100)
    b = await svc.add_criterion(session, actor=1, cycle_id=c.id, title="목표달성",
                                kind="MBO", weight=4, max_score=100)
    return c, a, b


async def test_full_flow_weighted_total_and_grade(session):
    """차수→항목→개시→배정→자기평가→채점→확정. 가중 총점 검증."""
    emp = await _emp(session, "E1")
    c, cr_a, cr_b = await _cycle_with_criteria(session)
    await svc.open_cycle(session, actor=1, cycle_id=c.id)
    ev = await svc.assign(session, actor=1, cycle_id=c.id, employee_id=emp.id, evaluator_id=10)
    # 자기평가(본인만)
    with pytest.raises(ForbiddenError):
        await svc.submit_self(session, eval_id=ev.id, self_emp=999,
                              scores=[{"criterion_id": cr_a.id, "score": 90}])
    await svc.submit_self(session, eval_id=ev.id, self_emp=emp.id,
                          scores=[{"criterion_id": cr_a.id, "score": 90},
                                  {"criterion_id": cr_b.id, "score": 80}])
    assert ev.status == "SELF_DONE"
    # 평가자 채점 — 가중: (80/100*6 + 90/100*4)/10*100 = (4.8+3.6)/10*100 = 84.0
    scope_all = require_scope(CurrentUser(user_id=10, role=Role.ADMIN, dept_id=None,
                                          license_flag=True), "hr.evaluation", "read")
    await svc.submit_eval(session, eval_id=ev.id, actor=10, actor_scope=scope_all,
                          scores=[{"criterion_id": cr_a.id, "score": 80},
                                  {"criterion_id": cr_b.id, "score": 90}])
    assert ev.status == "EVAL_DONE" and float(ev.eval_total) == 84.0
    # 확정 — 자동 등급(84→A)
    await svc.confirm(session, eval_id=ev.id, actor=1)
    assert ev.status == "CONFIRMED" and ev.final_grade == "A"


async def test_criteria_only_in_draft_and_open_needs_criteria(session):
    c = await svc.create_cycle(session, actor=1, period="2027", title="x")
    with pytest.raises(ValidationError):                 # 항목 없이 개시 불가
        await svc.open_cycle(session, actor=1, cycle_id=c.id)
    await svc.add_criterion(session, actor=1, cycle_id=c.id, title="항목")
    await svc.open_cycle(session, actor=1, cycle_id=c.id)
    with pytest.raises(ValidationError):                 # OPEN 후 항목 추가 불가
        await svc.add_criterion(session, actor=1, cycle_id=c.id, title="추가")


async def test_evaluator_row_guard_and_score_range(session):
    """지정 평가자/부서 아닌 자 채점 차단·점수 범위 검증."""
    e1 = await _emp(session, "D1", dept=3)
    c, cr_a, _ = await _cycle_with_criteria(session)
    await svc.open_cycle(session, actor=1, cycle_id=c.id)
    ev = await svc.assign(session, actor=1, cycle_id=c.id, employee_id=e1.id, evaluator_id=10)
    other = require_scope(CurrentUser(user_id=99, role=Role.MANAGER, dept_id=8,
                                      license_flag=True), "hr.evaluation", "read")
    with pytest.raises(ForbiddenError):                  # 타부서 부서장·비지정 평가자
        await svc.submit_eval(session, eval_id=ev.id, actor=99, actor_scope=other,
                              scores=[{"criterion_id": cr_a.id, "score": 50}])
    same = require_scope(CurrentUser(user_id=88, role=Role.MANAGER, dept_id=3,
                                     license_flag=True), "hr.evaluation", "read")
    with pytest.raises(ValidationError):                 # 점수 범위 초과(>max)
        await svc.submit_eval(session, eval_id=ev.id, actor=88, actor_scope=same,
                              scores=[{"criterion_id": cr_a.id, "score": 150}])


async def test_confirm_requires_eval_done(session):
    emp = await _emp(session, "C1")
    c, cr_a, _ = await _cycle_with_criteria(session)
    await svc.open_cycle(session, actor=1, cycle_id=c.id)
    ev = await svc.assign(session, actor=1, cycle_id=c.id, employee_id=emp.id)
    with pytest.raises(ValidationError):                 # 채점 전 확정 불가
        await svc.confirm(session, eval_id=ev.id, actor=1)


async def test_http_self_and_score_and_confirm(session, client):
    """HTTP — 자기평가(본인)→평가자 채점→확정. STAFF 타인 상세 403."""
    from erp.common.models import User
    from tests._http import auth_as, bearer, make_user
    admin = await auth_as(session, user_id=1, role="ADMIN")
    emp = await _emp(session, "H1", dept=3)
    # 자기평가자 계정(employee_id 연결)
    await make_user(session, user_id=50, role="STAFF")
    (await session.get(User, 50)).employee_id = emp.id
    await session.flush()
    r = await client.post("/api/v1/hr/eval/cycles", headers=admin,
                          json={"period": "2026H", "title": "반기", "cycle_type": "HALF"})
    cid = r.json()["id"]
    r = await client.post(f"/api/v1/hr/eval/cycles/{cid}/criteria", headers=admin,
                          json={"title": "역량", "weight": 1, "max_score": 100})
    crid = r.json()["id"]
    await client.post(f"/api/v1/hr/eval/cycles/{cid}/open", headers=admin)
    await client.post(f"/api/v1/hr/eval/cycles/{cid}/assign", headers=admin,
                      json={"assignments": [{"employee_id": emp.id, "evaluator_id": 1}]})
    evid = (await client.get(f"/api/v1/hr/eval/cycles/{cid}/evaluations",
                             headers=admin)).json()["items"][0]["id"]
    # 자기평가(본인 STAFF)
    r = await client.post(f"/api/v1/hr/eval/evaluations/{evid}/self",
                          headers=bearer(user_id=50, role="STAFF"),
                          json={"scores": [{"criterion_id": crid, "score": 88}]})
    assert r.status_code == 200 and r.json()["status"] == "SELF_DONE"
    # 평가자(admin) 채점→확정
    r = await client.post(f"/api/v1/hr/eval/evaluations/{evid}/score", headers=admin,
                          json={"scores": [{"criterion_id": crid, "score": 92}]})
    assert r.json()["eval_total"] == 92.0
    r = await client.post(f"/api/v1/hr/eval/evaluations/{evid}/confirm", headers=admin, json={})
    assert r.json()["final_grade"] == "S"
    # 타 직원(SELF) 상세 접근 차단
    await make_user(session, user_id=60, role="STAFF")
    r = await client.get(f"/api/v1/hr/eval/evaluations/{evid}",
                         headers=bearer(user_id=60, role="STAFF"))
    assert r.status_code == 403
