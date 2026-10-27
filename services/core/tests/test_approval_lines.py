"""결재선 요청·관리 (기안문서 선행 기반·0212) — 템플릿·개인결재선·요청 워크플로·엔진 build_from_steps."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.common.approval_engine import ApprovalEngine, validate_steps
from erp.common.approval_line_service import ApprovalLineService
from erp.common.models import ApprovalInstance, ApprovalLineTemplate, ApprovalStep
from erp.errors import ForbiddenError, ValidationError

svc = ApprovalLineService()
engine = ApprovalEngine()
ON = date(2026, 7, 21)
STEPS = [{"type": "DEPT_HEAD"}, {"type": "ROLE", "value": "DIRECTOR"}]


# ── 단계 검증 ──────────────────────────────────────────────
def test_validate_steps_rules():
    assert validate_steps([{"type": "role", "value": "MANAGER"}]) == [
        {"type": "ROLE", "value": "MANAGER"}]
    with pytest.raises(ValidationError):
        validate_steps([])                                   # 빈 단계
    with pytest.raises(ValidationError):
        validate_steps([{"type": "ROLE"}])                   # ROLE value 없음
    with pytest.raises(ValidationError):
        validate_steps([{"type": "ROLE", "value": "KING"}])  # 유효하지 않은 역할
    with pytest.raises(ValidationError):
        validate_steps([{"type": "USER", "value": "abc"}])   # USER 비정수
    with pytest.raises(ValidationError):
        validate_steps([{"type": "USER", "value": "5"},
                        {"type": "USER", "value": "5"}])     # 중복 결재자
    with pytest.raises(ValidationError):
        validate_steps([{"type": "ROLE", "value": "MANAGER"}] * 9)  # 9단계(>8)


# ── 개인 결재선 ────────────────────────────────────────────
async def test_personal_template_crud_owner_only(session):
    t = await svc.create_personal(session, user_id=1, name="내 품의선",
                                  steps=[{"type": "ROLE", "value": "MANAGER"}])
    assert t.scope == "PERSONAL" and t.owner_user_id == 1 and t.doc_types == ["ANY"]
    # 타인 수정/삭제 차단
    with pytest.raises(ForbiddenError):
        await svc.update_personal(session, template_id=t.id, user_id=2, name="x")
    with pytest.raises(ForbiddenError):
        await svc.delete_personal(session, template_id=t.id, user_id=2)
    await svc.update_personal(session, template_id=t.id, user_id=1,
                              steps=[{"type": "ROLE", "value": "DIRECTOR"}])
    assert t.steps == [{"type": "ROLE", "value": "DIRECTOR"}]
    await svc.delete_personal(session, template_id=t.id, user_id=1)
    assert await session.get(ApprovalLineTemplate, t.id) is None


async def test_list_templates_scope_and_doctype(session):
    # ORG 공용(GENERAL_DRAFT) + 본인 개인(ANY) + 타인 개인(비노출)
    session.add_all([
        ApprovalLineTemplate(name="공용품의", scope="ORG", doc_types=["GENERAL_DRAFT"],
                             steps=[{"type": "ROLE", "value": "DIRECTOR"}], active=True),
        ApprovalLineTemplate(name="비활성공용", scope="ORG", doc_types=["ANY"],
                             steps=[{"type": "ROLE", "value": "ADMIN"}], active=False),
        ApprovalLineTemplate(name="타인개인", scope="PERSONAL", owner_user_id=99,
                             doc_types=["ANY"], steps=[{"type": "ROLE", "value": "MANAGER"}],
                             active=True)])
    await svc.create_personal(session, user_id=1, name="내개인",
                              steps=[{"type": "ROLE", "value": "MANAGER"}])
    names = {t["name"] for t in await svc.list_templates(session, user_id=1,
                                                         doc_type="GENERAL_DRAFT")}
    assert names == {"공용품의", "내개인"}         # 비활성·타인개인·doctype불일치 제외


# ── 요청 워크플로 ──────────────────────────────────────────
async def test_request_new_provisions_org_template(session):
    req = await svc.submit_request(
        session, requested_by=5, request_type="NEW", reason="지출품의 표준선 필요",
        proposed_name="지출품의 결재선", proposed_steps=STEPS, doc_types=["GENERAL_DRAFT"],
        department_id=3, on=ON)
    assert req.req_no == "결재선요청-2026-0001" and req.status == "REQUESTED"
    out = await svc.review_request(session, request_id=req.id, approve=True, reviewer=1)
    assert out["status"] == "APPROVED" and out["template_id"]
    t = await session.get(ApprovalLineTemplate, out["template_id"])
    assert t.scope == "ORG" and t.name == "지출품의 결재선" and t.source_request_id == req.id
    assert t.steps == [{"type": "DEPT_HEAD", "value": None},
                       {"type": "ROLE", "value": "DIRECTOR"}]
    # 재검토 차단(멱등)
    with pytest.raises(ValidationError):
        await svc.review_request(session, request_id=req.id, approve=False, reviewer=1)


async def test_request_reject_no_template(session):
    req = await svc.submit_request(session, requested_by=5, request_type="NEW",
                                   reason="x", proposed_name="a", proposed_steps=STEPS, on=ON)
    out = await svc.review_request(session, request_id=req.id, approve=False, reviewer=1,
                                   note="중복")
    assert out["status"] == "REJECTED" and out["template_id"] is None
    assert (await session.scalar(select(ApprovalLineTemplate))) is None


async def test_request_modify_and_retire(session):
    base = ApprovalLineTemplate(name="구선", scope="ORG", doc_types=["ANY"],
                                steps=[{"type": "ROLE", "value": "MANAGER"}], active=True)
    session.add(base)
    await session.flush()
    # MODIFY — steps 교체
    rq = await svc.submit_request(session, requested_by=5, request_type="MODIFY",
                                  reason="단계 추가", target_template_id=base.id,
                                  proposed_steps=STEPS, doc_types=["GENERAL_DRAFT"], on=ON)
    await svc.review_request(session, request_id=rq.id, approve=True, reviewer=1)
    assert base.steps[0]["type"] == "DEPT_HEAD" and base.doc_types == ["GENERAL_DRAFT"]
    # RETIRE — 비활성
    rq2 = await svc.submit_request(session, requested_by=5, request_type="RETIRE",
                                   reason="폐기", target_template_id=base.id, on=ON)
    await svc.review_request(session, request_id=rq2.id, approve=True, reviewer=1)
    assert base.active is False


async def test_request_validation(session):
    with pytest.raises(ValidationError):            # 사유 없음
        await svc.submit_request(session, requested_by=5, request_type="NEW", reason=" ",
                                 proposed_name="a", proposed_steps=STEPS, on=ON)
    with pytest.raises(ValidationError):            # NEW인데 steps 없음
        await svc.submit_request(session, requested_by=5, request_type="NEW", reason="x",
                                 proposed_name="a", on=ON)
    with pytest.raises(ValidationError):            # MODIFY 대상 없음
        await svc.submit_request(session, requested_by=5, request_type="MODIFY", reason="x",
                                 proposed_steps=STEPS, on=ON)


async def test_cancel_request_owner_only(session):
    req = await svc.submit_request(session, requested_by=5, request_type="NEW", reason="x",
                                   proposed_name="a", proposed_steps=STEPS, on=ON)
    with pytest.raises(ForbiddenError):
        await svc.cancel_request(session, request_id=req.id, user_id=9)
    await svc.cancel_request(session, request_id=req.id, user_id=5)
    assert req.status == "CANCELED"


# ── 결재자 디렉터리 (HTTP·FE 결재선 관리 화면 지원) ─────────
async def test_directory_active_users_with_employee_name(session, client):
    """활성 사용자만·직원 연결 시 실명·미연결 익명 폴백. 최소필요(v1.203) — role·이메일 미노출."""
    from erp.common.models import User
    from erp.hr.models import Employee
    from tests._http import auth_as, bearer

    headers = await auth_as(session, user_id=1, role="STAFF")
    session.add(Employee(id=70, employee_no="E070", name="김결재", department_id=3,
                         hired_on=date(2025, 1, 1)))
    session.add_all([
        User(id=2, email="named@x.example", role="MANAGER", employee_id=70,
             password_hash="x", token_version=0, is_active=True),
        User(id=3, email="inactive@x.example", role="STAFF",
             password_hash="x", token_version=0, is_active=False)])
    await session.flush()
    r = await client.get("/api/v1/approvals/lines/directory", headers=headers)
    assert r.status_code == 200
    items = {it["id"]: it for it in r.json()["items"]}
    assert 3 not in items                                   # 비활성 제외
    assert items[2]["name"] == "김결재"                     # 직원 연결 → 실명
    assert items[1]["name"] == "사용자1"                    # 미연결 → 익명 폴백(이메일 힌트 금지)
    assert "role" not in items[2] and "email" not in items[2]   # 최소필요 — 역할 분포 미노출
    # WORKSTATION kiosk는 read 미부여(User 행 아님·토큰만으론 미통과) — 200 아니어야
    r = await client.get("/api/v1/approvals/lines/directory",
                         headers=bearer(user_id=9, role="WORKSTATION"))
    assert r.status_code in (401, 403)
    # 접속기록(v1.204) — 로스터 조회가 access_log에 남는다
    from erp.audit.models import AccessLog
    row = await session.scalar(select(AccessLog).where(
        AccessLog.entity == "common.approver_directory"))
    assert row is not None and row.actor == 1


# ── 엔진 build_from_steps (템플릿 소비 기반) ────────────────
async def test_build_from_steps_snapshot(session):
    """템플릿 steps로 인스턴스 생성 — DEPT_HEAD 부서 필수·멱등·단계 복사."""
    from erp.common.models import Department, User
    session.add(Department(id=3, name="내과", head_user_id=50))
    session.add(User(id=1, email="a@x.example", role="ADMIN", password_hash="x",
                     token_version=0, is_active=True))
    # 부서장(user 50) 실존 필요 — 엔진이 결재 알림을 FK로 적재(PG는 FK 강제·sqlite 잠복 갭)
    session.add(User(id=50, email="head3@x.example", role="MANAGER", password_hash="x",
                     token_version=0, is_active=True))
    await session.flush()
    inst = await engine.build_from_steps(session, doc_type="GENERAL_DRAFT", doc_id=7,
                                         steps=STEPS, created_by=1, department_id=3)
    assert inst.status == "PENDING"
    steps = (await session.scalars(select(ApprovalStep).where(
        ApprovalStep.instance_id == inst.id).order_by(ApprovalStep.step_no))).all()
    assert [(s.approver_type, s.approver_value) for s in steps] == [
        ("DEPT_HEAD", None), ("ROLE", "DIRECTOR")]
    # 멱등 — 재호출 시 동일 인스턴스
    again = await engine.build_from_steps(session, doc_type="GENERAL_DRAFT", doc_id=7,
                                          steps=STEPS, created_by=1, department_id=3)
    assert again.id == inst.id
    n = await session.scalar(select(__import__("sqlalchemy").func.count())
                             .select_from(ApprovalInstance)
                             .where(ApprovalInstance.doc_id == 7,
                                    ApprovalInstance.doc_type == "GENERAL_DRAFT"))
    assert n == 1


async def test_build_from_steps_dept_head_needs_dept(session):
    with pytest.raises(ValidationError):
        await engine.build_from_steps(session, doc_type="GENERAL_DRAFT", doc_id=8,
                                      steps=[{"type": "DEPT_HEAD"}], created_by=1)
