"""N단계 결재선 엔진 — 룰 매칭·단계 진행·승인자 적격·직무분리·반려·워크리스트."""
import pytest

from erp.common.approval_engine import ApprovalEngine
from erp.common.models import ApprovalRule, Department, User
from erp.errors import ValidationError

eng = ApprovalEngine()


async def _user(session, uid, role, dept_id=None):
    u = User(id=uid, email=f"u{uid}@x.example", role=role, department_id=dept_id, password_hash="x")
    session.add(u)
    await session.flush()
    return u


async def _rule(session, doc_type="PO", steps=None, min_amount=0, max_amount=None,
                category=None):
    r = ApprovalRule(doc_type=doc_type, category=category, min_amount=min_amount,
                     max_amount=max_amount, steps=steps or [{"type": "ROLE", "value": "MANAGER"}],
                     active=True, created_by=1)
    session.add(r)
    await session.flush()
    return r


async def test_no_rule_returns_none(session):
    inst = await eng.build(session, doc_type="PO", doc_id=1, amount=1000, created_by=1)
    assert inst is None       # 룰 없음 → 레거시 단일승인 폴백


async def test_threshold_band_selects_rule(session):
    await _rule(session, steps=[{"type": "ROLE", "value": "MANAGER"}],
                min_amount=0, max_amount=10_000_000)
    await _rule(session, steps=[{"type": "ROLE", "value": "MANAGER"},
                                {"type": "ROLE", "value": "DIRECTOR"}],
                min_amount=10_000_000, max_amount=None)
    small = await eng.find_rule(session, doc_type="PO", amount=5_000_000)
    big = await eng.find_rule(session, doc_type="PO", amount=20_000_000)
    assert len(small.steps) == 1 and len(big.steps) == 2


async def test_find_rule_deterministic_among_overlapping_bands(session):
    # 중첩 밴드(둘 다 매칭) — 좁은 밴드(높은 min_amount) 우선, 결정적
    await _rule(session, steps=[{"type": "ROLE", "value": "MANAGER"}],
                min_amount=0, max_amount=None)                       # 광역
    narrow = await _rule(session, steps=[{"type": "ROLE", "value": "DIRECTOR"}],
                         min_amount=10_000_000, max_amount=None)     # 고액 밴드
    picked = await eng.find_rule(session, doc_type="PO", amount=20_000_000)
    assert picked.id == narrow.id      # 높은 min_amount 우선(결정적)


async def test_two_step_chain_full_approval(session):
    await _user(session, 1, "STAFF")
    await _user(session, 2, "MANAGER")
    await _user(session, 3, "DIRECTOR")
    await _rule(session, steps=[{"type": "ROLE", "value": "MANAGER"},
                                {"type": "ROLE", "value": "DIRECTOR"}], min_amount=0)
    inst = await eng.build(session, doc_type="PO", doc_id=10, amount=5_000_000, created_by=1)
    assert inst.status == "PENDING" and inst.current_step == 1
    # 1단계: MANAGER 승인
    await eng.act(session, doc_type="PO", doc_id=10, actor=2, approve=True)
    assert inst.status == "PENDING" and inst.current_step == 2
    # 2단계: DIRECTOR 승인 → 완료
    out = await eng.act(session, doc_type="PO", doc_id=10, actor=3, approve=True)
    assert out.status == "APPROVED" and out.decided_at is not None


async def test_approval_step_notifications(session):
    """P1-3 — 상신 시 1단계 적격자·단계 진행 시 다음 단계 적격자에 '내 차례' 알림."""
    from sqlalchemy import select

    from erp.common.models import Notification
    await _user(session, 1, "STAFF")
    await _user(session, 2, "MANAGER")
    await _user(session, 3, "DIRECTOR")
    await _rule(session, steps=[{"type": "ROLE", "value": "MANAGER"},
                                {"type": "USER", "value": "3"}], min_amount=0)
    await eng.build(session, doc_type="PO", doc_id=77, amount=5_000_000, created_by=1)
    # 상신 → MANAGER(user2)에 알림
    n2 = (await session.scalars(select(Notification).where(
        Notification.user_id == 2, Notification.type == "APPROVAL_PENDING"))).all()
    assert len(n2) == 1 and "구매발주 #77" in n2[0].body
    # 1단계 승인 → 2단계 지정자(user3)에 알림
    await eng.act(session, doc_type="PO", doc_id=77, actor=2, approve=True)
    n3 = (await session.scalars(select(Notification).where(
        Notification.user_id == 3, Notification.type == "APPROVAL_PENDING"))).all()
    assert len(n3) == 1 and "2단계" in n3[0].body


async def test_wrong_role_blocked(session):
    await _user(session, 1, "STAFF")
    await _user(session, 2, "STAFF")
    await _rule(session, steps=[{"type": "ROLE", "value": "MANAGER"}])
    await eng.build(session, doc_type="PO", doc_id=11, amount=1000, created_by=1)
    with pytest.raises(ValidationError, match="권한이 없습니다"):
        await eng.act(session, doc_type="PO", doc_id=11, actor=2, approve=True)  # STAFF≠MANAGER


async def test_segregation_creator_cannot_approve(session):
    await _user(session, 2, "MANAGER")
    await _rule(session, steps=[{"type": "ROLE", "value": "MANAGER"}])
    await eng.build(session, doc_type="PO", doc_id=12, amount=1000, created_by=2)
    with pytest.raises(ValidationError, match="직무분리"):
        await eng.act(session, doc_type="PO", doc_id=12, actor=2, approve=True)


async def test_second_act_after_decided_rejected(session):
    """전체검토 HIGH — 확정된 결재의 재행위는 반려(행잠금 후처리 이중적용 방지 계약).

    행잠금(with_for_update)의 동시성 보장은 SQLite에서 검증 불가(FOR UPDATE no-op)지만,
    잠금이 재평가하는 status=='PENDING' 술어의 종단 계약(확정 후 재호출 차단)을 고정."""
    await _user(session, 1, "STAFF")
    await _user(session, 2, "MANAGER")
    await _rule(session, steps=[{"type": "ROLE", "value": "MANAGER"}])
    await eng.build(session, doc_type="PO", doc_id=14, amount=1000, created_by=1)
    out = await eng.act(session, doc_type="PO", doc_id=14, actor=2, approve=True)
    assert out.status == "APPROVED"
    with pytest.raises(ValidationError, match="진행 중인 결재가 없습니다"):
        await eng.act(session, doc_type="PO", doc_id=14, actor=2, approve=True)   # 이중 최종적용 차단


async def test_reject_terminates_chain(session):
    await _user(session, 1, "STAFF")
    await _user(session, 2, "MANAGER")
    await _rule(session, steps=[{"type": "ROLE", "value": "MANAGER"},
                                {"type": "ROLE", "value": "DIRECTOR"}])
    await eng.build(session, doc_type="PO", doc_id=13, amount=1000, created_by=1)
    out = await eng.act(session, doc_type="PO", doc_id=13, actor=2, approve=False, note="예산 초과")
    assert out.status == "REJECTED"


async def test_dept_head_step(session):
    await _user(session, 1, "STAFF")
    head = await _user(session, 5, "MANAGER")
    dept = Department(id=3, name="총무", head_user_id=head.id)
    session.add(dept)
    await session.flush()
    await _rule(session, steps=[{"type": "DEPT_HEAD"}])
    await eng.build(session, doc_type="PO", doc_id=14, amount=1000, created_by=1, department_id=3)
    out = await eng.act(session, doc_type="PO", doc_id=14, actor=5, approve=True)
    assert out.status == "APPROVED"


async def test_executive_override_skips_remaining_steps(session):
    await _user(session, 1, "STAFF")
    await _user(session, 9, "DIRECTOR")
    await _rule(session, steps=[{"type": "ROLE", "value": "MANAGER"},
                                {"type": "ROLE", "value": "DIRECTOR"}])
    await eng.build(session, doc_type="PO", doc_id=20, amount=99_000_000, created_by=1)
    out = await eng.act(session, doc_type="PO", doc_id=20, actor=9, approve=True,
                        override=True, note="긴급 집행")
    assert out.status == "APPROVED"
    st = await eng.get(session, doc_type="PO", doc_id=20)
    assert st["steps"][0]["status"] == "APPROVED"      # 전권 행사 단계
    assert st["steps"][1]["status"] == "SKIPPED"       # 잔여 단계 생략


async def test_override_requires_director(session):
    await _user(session, 1, "STAFF")
    await _user(session, 2, "MANAGER")
    await _rule(session, steps=[{"type": "ROLE", "value": "DIRECTOR"}])
    await eng.build(session, doc_type="PO", doc_id=21, amount=1000, created_by=1)
    with pytest.raises(ValidationError, match="전권 결재 권한"):
        await eng.act(session, doc_type="PO", doc_id=21, actor=2, approve=True, override=True)


async def test_override_keeps_segregation(session):
    await _user(session, 9, "DIRECTOR")
    await _rule(session, steps=[{"type": "ROLE", "value": "DIRECTOR"}])
    await eng.build(session, doc_type="PO", doc_id=22, amount=1000, created_by=9)  # 작성자=원장
    with pytest.raises(ValidationError, match="직무분리"):
        await eng.act(session, doc_type="PO", doc_id=22, actor=9, approve=True, override=True)


async def test_build_dept_head_requires_department(session):
    # 부서장 단계 룰인데 문서 귀속 부서가 없으면 build 거부(영원히 미해결 방지)
    await _rule(session, steps=[{"type": "DEPT_HEAD"}])
    with pytest.raises(ValidationError, match="귀속 부서"):
        await eng.build(session, doc_type="PO", doc_id=30, amount=1000, created_by=1)
    # 부서 지정 시 정상 생성
    inst = await eng.build(session, doc_type="PO", doc_id=31, amount=1000, created_by=1,
                           department_id=7)
    assert inst is not None and inst.status == "PENDING"


async def test_worklist_lists_eligible_pending(session):
    await _user(session, 1, "STAFF")
    await _user(session, 2, "MANAGER")
    await _rule(session, steps=[{"type": "ROLE", "value": "MANAGER"}])
    await eng.build(session, doc_type="PO", doc_id=15, amount=1000, created_by=1)
    wl = await eng.pending_for(session, actor=2, role="MANAGER", dept_id=None)
    assert any(i["doc_id"] == 15 for i in wl)
    wl_staff = await eng.pending_for(session, actor=1, role="STAFF", dept_id=None)
    assert wl_staff == []     # 작성자 본인 + 비적격
