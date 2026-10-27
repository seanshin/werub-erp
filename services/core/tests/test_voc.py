"""환자 민원·CS(VOC·Tier3 ①·마이그 0223) — 생애주기·가드·행스코프·통계·HTTP RBAC."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from erp.errors import ForbiddenError, ValidationError
from erp.patient.models import VocAction, VocCase
from erp.patient.voc_service import SLA_DAYS, VocService

svc = VocService()
# 서비스의 기한초과 판정은 실시계(`due_on < date.today()`)다 — 고정 날짜로 앵커하면
# 달력이 SLA_DAYS를 지나는 순간 대조군까지 기한초과가 돼 테스트가 스스로 거짓이 된다
# (2026-08-06부터 야간 PG 레인 적색·한 달간 미인지). 오늘 기준 상대 오프셋으로 고정한다.
ON = date.today()


async def _case(session, **kw):
    kw.setdefault("created_by", 1)
    kw.setdefault("title", "대기시간 불만")
    kw.setdefault("on", ON)
    return await svc.receive(session, **kw)


# ── 접수 ───────────────────────────────────────────────────
async def test_receive_numbering_sla_and_contact_encryption(session):
    c = await _case(session, contact="010-1234-5678", vtype="complaint", severity="high")
    assert c.case_no == f"민원-{ON.year}-0001" and c.status == "RECEIVED"
    assert c.due_on == ON + timedelta(days=SLA_DAYS)
    assert c.vtype == "COMPLAINT" and c.severity == "HIGH"     # 대문자 정규화
    assert c.contact_enc is not None and b"1234" not in c.contact_enc   # 암호문
    c2 = await _case(session, title="둘째")
    assert c2.case_no == f"민원-{ON.year}-0002"


async def test_receive_patient_link_skips_contact(session):
    """환자 연결 시 연락처 미저장(최소수집) — 미존재 환자는 거부."""
    with pytest.raises(ValidationError):
        await _case(session, patient_id=99999)
    from erp.patient.models import Patient
    session.add(Patient(id=5, chart_no="TEST-V1", name="김환자", birth_date=date(1990, 1, 1)))
    await session.flush()
    c = await _case(session, patient_id=5, contact="010-9999-0000")
    assert c.patient_id == 5 and c.contact_enc is None


async def test_receive_requires_title(session):
    with pytest.raises(ValidationError):
        await _case(session, title="  ")


# ── 생애주기 전이 ───────────────────────────────────────────
async def test_lifecycle_assign_answer_close_reopen(session):
    c = await _case(session)
    await svc.assign(session, case_id=c.id, actor=2, assignee_user_id=7, department_id=3)
    assert c.status == "IN_PROGRESS" and c.assignee_user_id == 7 and c.department_id == 3
    with pytest.raises(ValidationError):                      # 회신 전 종결 불가
        await svc.close(session, case_id=c.id, actor=2)
    await svc.answer(session, case_id=c.id, actor=2, answer_body="사과 및 프로세스 개선 안내")
    assert c.status == "ANSWERED" and c.answered_at is not None
    with pytest.raises(ValidationError):                      # 회신 상태에서 재회신 불가
        await svc.answer(session, case_id=c.id, actor=2, answer_body="x")
    await svc.close(session, case_id=c.id, actor=2, note="민원인 수용")
    assert c.status == "CLOSED" and c.closed_at is not None
    with pytest.raises(ValidationError):                      # 종결 후 배정 불가
        await svc.assign(session, case_id=c.id, actor=2, assignee_user_id=9)
    await svc.reopen(session, case_id=c.id, actor=2, note="민원인 재제기")
    assert c.status == "IN_PROGRESS" and c.reopen_count == 1 and c.closed_at is None
    acts = [a.action for a in (await session.scalars(
        select(VocAction).where(VocAction.case_id == c.id).order_by(VocAction.id))).all()]
    assert acts == ["ASSIGN", "ANSWER", "CLOSE", "REOPEN"]    # 전이 이력 완전


async def test_comment_and_guards(session):
    c = await _case(session)
    await svc.comment(session, case_id=c.id, actor=3, note="시설팀 확인 요청")
    with pytest.raises(ValidationError):
        await svc.comment(session, case_id=c.id, actor=3, note=" ")
    with pytest.raises(ValidationError):                      # 미종결 재오픈 불가
        await svc.reopen(session, case_id=c.id, actor=3, note="x")


# ── 행스코프·목록·통계 ─────────────────────────────────────
async def test_row_scope_self_and_dept(session):
    from erp.common.policy import Role, ScopeKind, require_scope
    from erp.common.router import CurrentUser

    c = await _case(session, created_by=10, department_id=3)
    staff_other = CurrentUser(user_id=11, role=Role.STAFF, dept_id=3, license_flag=True)
    scope_self = require_scope(staff_other, "patient.voc", "read")
    assert scope_self.kind == ScopeKind.SELF
    with pytest.raises(ForbiddenError):                       # 타인 접수분 SELF 차단
        await svc.assert_row_scope(session, c.id, scope=scope_self, user=staff_other)
    mgr_other = CurrentUser(user_id=12, role=Role.MANAGER, dept_id=8, license_flag=True)
    scope_dept = require_scope(mgr_other, "patient.voc", "admin")
    with pytest.raises(ForbiddenError):                       # 타 부서 DEPT 차단
        await svc.assert_row_scope(session, c.id, scope=scope_dept, user=mgr_other)
    mgr_same = CurrentUser(user_id=13, role=Role.MANAGER, dept_id=3, license_flag=True)
    await svc.assert_row_scope(session, c.id,
                               scope=require_scope(mgr_same, "patient.voc", "admin"),
                               user=mgr_same)                 # 본부서 통과


async def test_list_filters_and_overdue(session):
    late = await _case(session, on=ON - timedelta(days=40), category="FACILITY")
    await _case(session, vtype="PRAISE", category="STAFF")
    items = await svc.list_cases(session, overdue=True)
    assert [i["id"] for i in items] == [late.id] and items[0]["overdue"] is True
    assert len(await svc.list_cases(session, vtype="PRAISE")) == 1
    assert len(await svc.list_cases(session, created_by=99)) == 0


async def test_stats_counts_and_avg_days(session):
    c = await _case(session, on=ON, category="CARE")
    await svc.assign(session, case_id=c.id, actor=2, assignee_user_id=7)
    await svc.answer(session, case_id=c.id, actor=2, answer_body="안내")
    await svc.close(session, case_id=c.id, actor=2)
    await _case(session, on=ON - timedelta(days=30), vtype="GRIEVANCE")   # 미종결·기한초과
    out = await svc.stats(session, since=ON - timedelta(days=60), until=ON)
    assert out["total"] == 2 and out["open"] == 1 and out["overdue"] == 1
    assert out["by_vtype"]["GRIEVANCE"] == 1 and out["by_category"]["CARE"] == 1
    assert out["avg_close_days"] is not None


# ── HTTP·RBAC ──────────────────────────────────────────────
async def test_http_receive_list_detail_scope(session, client):
    """접수(전 직원)→본인 목록(SELF)→타인 상세 403·본인 상세 200+접속기록. 통계는 admin 전용."""
    from tests._http import auth_as, bearer

    h_staff = await auth_as(session, user_id=21, role="STAFF", dept_id=3)
    r = await client.post("/api/v1/patients/voc", headers=h_staff,
                          json={"title": "주차장 불편", "category": "FACILITY"})
    assert r.status_code == 201
    case_id = r.json()["id"]
    # 본인 목록(SELF) — 타인 것 미노출
    h_staff2 = await auth_as(session, user_id=22, role="STAFF", dept_id=3)
    assert (await client.get("/api/v1/patients/voc", headers=h_staff2)).json()["items"] == []
    r = await client.get(f"/api/v1/patients/voc/{case_id}", headers=h_staff2)
    assert r.status_code == 403                               # 타인 접수분 상세 차단
    r = await client.get(f"/api/v1/patients/voc/{case_id}", headers=h_staff)
    assert r.status_code == 200 and r.json()["case_no"].startswith("민원-")
    from erp.audit.models import AccessLog
    assert await session.scalar(select(AccessLog).where(
        AccessLog.entity == "patient.voc_cases")) is not None   # 상세 열람 접속기록
    # 통계 admin 전용 — STAFF 403·WORKSTATION 미통과
    assert (await client.get("/api/v1/patients/voc/stats?since=2026-07-01&until=2026-07-31",
                             headers=h_staff)).status_code == 403
    assert (await client.get("/api/v1/patients/voc",
                             headers=bearer(user_id=9, role="WORKSTATION"))
            ).status_code in (401, 403)


async def test_http_admin_flow_and_dept_boundary(session, client):
    """MANAGER(DEPT)는 본부서 귀속만 배정/회신 가능 — 타 부서 403."""
    from tests._http import auth_as

    h_staff = await auth_as(session, user_id=31, role="STAFF", dept_id=3)
    r = await client.post("/api/v1/patients/voc", headers=h_staff,
                          json={"title": "응대 불만", "category": "STAFF",
                                "department_id": 3})
    case_id = r.json()["id"]
    h_mgr8 = await auth_as(session, user_id=32, role="MANAGER", dept_id=8)
    r = await client.post(f"/api/v1/patients/voc/{case_id}/assign", headers=h_mgr8,
                          json={"assignee_user_id": 40})
    assert r.status_code == 403                               # 타 부서 부서장 차단
    h_mgr3 = await auth_as(session, user_id=33, role="MANAGER", dept_id=3)
    r = await client.post(f"/api/v1/patients/voc/{case_id}/assign", headers=h_mgr3,
                          json={"assignee_user_id": 40})
    assert r.status_code == 200 and r.json()["status"] == "IN_PROGRESS"
    r = await client.post(f"/api/v1/patients/voc/{case_id}/answer", headers=h_mgr3,
                          json={"answer_body": "교육 조치 완료 안내"})
    assert r.status_code == 200 and r.json()["status"] == "ANSWERED"
    r = await client.post(f"/api/v1/patients/voc/{case_id}/close", headers=h_mgr3, json={"note": "수용"})
    assert r.status_code == 200 and r.json()["status"] == "CLOSED"
    c = await session.get(VocCase, case_id)
    assert c.status == "CLOSED"
