"""자금결재 기안문서(ERP 내부 결재·0213) — 생애주기·결재선 선택·회수(H-3)·본문봉인(M-1)·행스코프·첨부동결."""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.common.approval_line_service import ApprovalLineService
from erp.common.draft_service import DraftService
from erp.common.models import (
    ApprovalInstance, ApprovalLedger, ApprovalLineTemplate, Department, Notification, User,
)
from erp.common.router import CurrentUser
from erp.errors import ForbiddenError, ValidationError

svc = DraftService()
lines = ApprovalLineService()
ON = date(2026, 7, 21)


def _u(uid, role="STAFF", dept=None):
    return CurrentUser(user_id=uid, role=role, dept_id=dept, license_flag=True)


async def _seed_users(session, *ids_roles):
    for uid, role in ids_roles:
        session.add(User(id=uid, email=f"u{uid}@x.example", role=role, password_hash="x",
                         token_version=0, is_active=True))
    await session.flush()


async def _org_line(session, steps, doc_types=None):
    t = ApprovalLineTemplate(name="테스트결재선", scope="ORG",
                             doc_types=doc_types or ["GENERAL_DRAFT"], steps=steps, active=True)
    session.add(t)
    await session.flush()
    return t


async def test_lifecycle_two_step_approve(session):
    """기안→상신(템플릿 선택·doc_no 채번·본문봉인)→2단계 승인(USER)→APPROVED·공람 알림."""
    await _seed_users(session, (1, "STAFF"), (10, "MANAGER"), (20, "DIRECTOR"), (30, "STAFF"))
    tpl = await _org_line(session, [{"type": "USER", "value": "10"},
                                    {"type": "USER", "value": "20"}])
    d = await svc.create(session, user_id=1, dept_id=3, title="예산 외 지출 품의",
                         body="사유 본문", category="BUDGET")
    assert d.status == "DRAFT" and d.doc_no is None
    d = await svc.submit(session, draft_id=d.id, user_id=1, template_id=tpl.id,
                         viewer_ids=[30], on=ON)
    assert d.status == "PENDING" and d.doc_no == "기안-2026-0001"
    # 본문 봉인 — BUILD 원장 note에 sha256
    build = await session.scalar(select(ApprovalLedger).where(
        ApprovalLedger.doc_id == d.id, ApprovalLedger.action == "BUILD"))
    assert "sha256:" in (build.note or "")
    # 1단계(user10) 승인 → PENDING(2단계 대기)
    d = await svc.approve(session, draft_id=d.id, actor=10)
    assert d.status == "PENDING"
    # 2단계(user20) 승인 → APPROVED
    d = await svc.approve(session, draft_id=d.id, actor=20)
    assert d.status == "APPROVED"
    # 공람자(30)+기안자(1) 완료 알림
    notif = (await session.scalars(select(Notification).where(
        Notification.title.like("%결재완료%")))).all()
    assert {n.user_id for n in notif} == {1, 30}


async def test_submit_rejects_self_in_line_and_wrong_doctype(session):
    await _seed_users(session, (1, "STAFF"))
    self_line = await _org_line(session, [{"type": "USER", "value": "1"}])
    d = await svc.create(session, user_id=1, dept_id=3, title="t", body="b")
    with pytest.raises(ValidationError):        # 기안자 본인이 결재선에
        await svc.submit(session, draft_id=d.id, user_id=1, template_id=self_line.id)
    wrong = await _org_line(session, [{"type": "ROLE", "value": "DIRECTOR"}],
                            doc_types=["EXPENSE"])
    d2 = await svc.create(session, user_id=1, dept_id=3, title="t2", body="b")
    with pytest.raises(ValidationError):        # doc_type 불일치
        await svc.submit(session, draft_id=d2.id, user_id=1, template_id=wrong.id)


async def test_cancel_before_any_approval(session):
    """회수(H-3) — 미결재 시 CANCELED·인스턴스 종결. 결재 시작 후엔 회수 불가."""
    await _seed_users(session, (1, "STAFF"), (10, "MANAGER"), (20, "DIRECTOR"))
    tpl = await _org_line(session, [{"type": "USER", "value": "10"},
                                    {"type": "USER", "value": "20"}])
    d = await svc.create(session, user_id=1, dept_id=3, title="회수건", body="b")
    await svc.submit(session, draft_id=d.id, user_id=1, template_id=tpl.id, on=ON)
    d = await svc.cancel(session, draft_id=d.id, user_id=1)
    assert d.status == "CANCELED"
    inst = await session.scalar(select(ApprovalInstance).where(
        ApprovalInstance.doc_type == "GENERAL_DRAFT", ApprovalInstance.doc_id == d.id))
    assert inst.status == "CANCELED"            # 좀비 워크리스트 방지


async def test_cancel_blocked_after_approval_started(session):
    await _seed_users(session, (1, "STAFF"), (10, "MANAGER"), (20, "DIRECTOR"))
    tpl = await _org_line(session, [{"type": "USER", "value": "10"},
                                    {"type": "USER", "value": "20"}])
    d = await svc.create(session, user_id=1, dept_id=3, title="진행건", body="b")
    await svc.submit(session, draft_id=d.id, user_id=1, template_id=tpl.id, on=ON)
    await svc.approve(session, draft_id=d.id, actor=10)   # 1단계 결재됨
    with pytest.raises(ValidationError):
        await svc.cancel(session, draft_id=d.id, user_id=1)


async def test_only_designated_user_can_approve(session):
    """지정 결재자만 결재 — 엔진 _eligible(H-1: STAFF도 지정되면 결재 가능·타인은 거부)."""
    await _seed_users(session, (1, "STAFF"), (5, "STAFF"), (10, "STAFF"))
    tpl = await _org_line(session, [{"type": "USER", "value": "10"}])
    d = await svc.create(session, user_id=1, dept_id=3, title="t", body="b")
    await svc.submit(session, draft_id=d.id, user_id=1, template_id=tpl.id, on=ON)
    with pytest.raises(ValidationError):        # 미지정 STAFF(5) 결재 거부
        await svc.approve(session, draft_id=d.id, actor=5)
    d = await svc.approve(session, draft_id=d.id, actor=10)   # 지정 STAFF(10) 승인 가능
    assert d.status == "APPROVED"


async def test_dept_head_step_notify_and_approve(session):
    await _seed_users(session, (1, "STAFF"), (50, "MANAGER"))
    session.add(Department(id=3, name="내과", head_user_id=50))
    await session.flush()
    tpl = await _org_line(session, [{"type": "DEPT_HEAD"}])
    d = await svc.create(session, user_id=1, dept_id=3, title="부서장결재", body="b")
    await svc.submit(session, draft_id=d.id, user_id=1, template_id=tpl.id, on=ON)
    # 부서장(50) 알림
    assert await session.scalar(select(func.count()).select_from(Notification).where(
        Notification.user_id == 50, Notification.title.like("%결재 요청%")))
    d = await svc.approve(session, draft_id=d.id, actor=50)
    assert d.status == "APPROVED"


async def test_reject_sets_status_and_notifies_drafter(session):
    await _seed_users(session, (1, "STAFF"), (10, "MANAGER"))
    tpl = await _org_line(session, [{"type": "USER", "value": "10"}])
    d = await svc.create(session, user_id=1, dept_id=3, title="반려건", body="b")
    await svc.submit(session, draft_id=d.id, user_id=1, template_id=tpl.id, on=ON)
    d = await svc.reject(session, draft_id=d.id, actor=10, note="근거 부족")
    assert d.status == "REJECTED"
    assert await session.scalar(select(func.count()).select_from(Notification).where(
        Notification.user_id == 1, Notification.title.like("%반려%")))


async def test_update_only_draft_and_owner(session):
    await _seed_users(session, (1, "STAFF"), (10, "MANAGER"))
    tpl = await _org_line(session, [{"type": "USER", "value": "10"}])
    d = await svc.create(session, user_id=1, dept_id=3, title="원본", body="b")
    with pytest.raises(ForbiddenError):         # 타인 수정 차단
        await svc.update(session, draft_id=d.id, user_id=2, title="변조")
    await svc.update(session, draft_id=d.id, user_id=1, title="수정본")
    assert d.title == "수정본"
    await svc.submit(session, draft_id=d.id, user_id=1, template_id=tpl.id, on=ON)
    with pytest.raises(ValidationError):        # 상신 후 수정 차단(M-3)
        await svc.update(session, draft_id=d.id, user_id=1, title="상신후변조")


async def test_row_scope_detail_involved_only(session):
    """SELF 스코프 — 무관자 상세 조회 403·기안자/공람자/지정결재자는 허용."""
    await _seed_users(session, (1, "STAFF"), (10, "STAFF"), (30, "STAFF"), (99, "STAFF"))
    tpl = await _org_line(session, [{"type": "USER", "value": "10"}])
    d = await svc.create(session, user_id=1, dept_id=3, title="t", body="b")
    await svc.submit(session, draft_id=d.id, user_id=1, template_id=tpl.id,
                     viewer_ids=[30], on=ON)
    await svc.get_detail(session, draft_id=d.id, user=_u(1))     # 기안자
    await svc.get_detail(session, draft_id=d.id, user=_u(10))    # 지정 결재자
    await svc.get_detail(session, draft_id=d.id, user=_u(30))    # 공람자
    with pytest.raises(ForbiddenError):                          # 무관자
        await svc.get_detail(session, draft_id=d.id, user=_u(99))


async def test_content_hash_seals_attachments(session):
    """본문 봉인 해시가 첨부 sha256을 포함 — 상신 시점 증빙 집합 봉인(M-1)."""
    from erp.common.models import Attachment
    await _seed_users(session, (1, "STAFF"))
    d = await svc.create(session, user_id=1, dept_id=3, title="첨부건", body="b")
    session.add(Attachment(entity_type="draft", entity_id=d.id, filename="a.pdf",
                           content_type="application/pdf", size_bytes=1, sha256="a" * 64,
                           uploaded_by=1))
    await session.flush()
    h1 = await svc._content_hash(session, d)
    session.add(Attachment(entity_type="draft", entity_id=d.id, filename="b.pdf",
                           content_type="application/pdf", size_bytes=1, sha256="b" * 64,
                           uploaded_by=1))
    await session.flush()
    assert await svc._content_hash(session, d) != h1     # 첨부 변화가 봉인 해시에 반영
