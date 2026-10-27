"""VoucherService — 멱등·차대 균형·역분개 (FR-49/54, design §4.4)."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.models import Voucher, VoucherLine
from erp.finance.service import VoucherLineDTO, VoucherService

svc = VoucherService()
LINES = [VoucherLineDTO("1110", "DEBIT", 100_000), VoucherLineDTO("4110", "CREDIT", 100_000)]


async def test_create_balanced_voucher(session):
    v = await svc.create(session, source_module="patient", source_ref="t-1",
                         voucher_on=date(2026, 6, 7), lines=LINES)
    lines = (await session.execute(select(VoucherLine))).scalars().all()
    assert v.id and len(lines) == 2


async def test_list_vouchers_exposes_amount_and_lines(session):
    """전체검토 X0-3 — 승인자가 금액을 보고 결재하도록 목록에 총액·라인 상세 노출."""
    from erp.common.router import CurrentUser
    from erp.finance.router import list_vouchers, voucher_lines
    v = await svc.create(session, source_module="manual", source_ref="x0-3",
                         voucher_on=date(2026, 6, 7),
                         lines=[VoucherLineDTO("1110", "DEBIT", 250_000),
                                VoucherLineDTO("4110", "CREDIT", 250_000)])
    await session.flush()
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    lst = await list_vouchers(user=admin, session=session)
    row = next(it for it in lst["items"] if it["id"] == v.id)
    assert row["amount"] == 250_000                       # 차변 합 = 전표 총액
    detail = await voucher_lines(v.id, user=admin, session=session)
    accts = {(ln["account_code"], ln["side"], ln["amount"]) for ln in detail["items"]}
    assert ("1110", "DEBIT", 250_000) in accts and ("4110", "CREDIT", 250_000) in accts


async def test_idempotent_same_source_ref(session):
    """멱등 — 동일 출처 재호출 시 기존 전표 반환 (중복 메시지 안전)."""
    v1 = await svc.create(session, source_module="patient", source_ref="t-1",
                          voucher_on=date(2026, 6, 7), lines=LINES)
    v2 = await svc.create(session, source_module="patient", source_ref="t-1",
                          voucher_on=date(2026, 6, 7), lines=LINES)
    assert v1.id == v2.id
    assert len((await session.execute(select(Voucher))).scalars().all()) == 1


async def test_unbalanced_rejected(session):
    with pytest.raises(ValidationError):
        await svc.create(session, source_module="manual", source_ref="bad-1",
                         voucher_on=date(2026, 6, 7),
                         lines=[VoucherLineDTO("1110", "DEBIT", 100),
                                VoucherLineDTO("4110", "CREDIT", 90)])


async def test_reverse_creates_flipped_and_marks_original(session):
    """역분개 (FR-54) — 원 전표 보존 + 반대 분개."""
    v = await svc.create(session, source_module="patient", source_ref="t-2",
                         voucher_on=date(2026, 6, 7), lines=LINES)
    r = await svc.reverse(session, voucher_id=v.id, reason="마감 정정")
    assert v.status == "REVERSED" and v.reversed_by == r.id and r.reverses == v.id
    r_lines = (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == r.id))).scalars().all()
    assert {(ln.account_code, ln.side) for ln in r_lines} == {("1110", "CREDIT"),
                                                              ("4110", "DEBIT")}


async def test_double_reverse_rejected(session):
    v = await svc.create(session, source_module="patient", source_ref="t-3",
                         voucher_on=date(2026, 6, 7), lines=LINES)
    await svc.reverse(session, voucher_id=v.id, reason="1차")
    with pytest.raises(ValidationError):
        await svc.reverse(session, voucher_id=v.id, reason="2차")


async def test_reverse_endpoint_reason_required_and_audited(session):
    """P0-7/P0-8 — 역분개 EP 사유 필수 + 재무 변경 감사기록(AuditLog)."""
    from sqlalchemy import func

    from erp.audit.models import AuditLog
    from erp.common.router import CurrentUser
    from erp.finance.router import ReverseBody, reverse_voucher
    v = await svc.create(session, source_module="manual", source_ref="ep-rev",
                         voucher_on=date(2026, 6, 7), lines=LINES)
    await session.flush()
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    with pytest.raises(ValidationError):                       # 사유 공백 거부
        await reverse_voucher(voucher_id=v.id, body=ReverseBody(reason="  "),
                              user=admin, session=session)
    out = await reverse_voucher(voucher_id=v.id, body=ReverseBody(reason="마감 정정"),
                                user=admin, session=session)
    assert out["reverses"] == v.id
    n = await session.scalar(select(func.count()).select_from(AuditLog)
                             .where(AuditLog.action == "REVERSE"))
    assert n == 1                                              # 감사기록 1건


# ── M2 승인 워크플로 (maker-checker, deferred line posting) ───────────
async def _count_lines(session, voucher_id):
    from sqlalchemy import func
    return await session.scalar(select(func.count()).select_from(VoucherLine)
                                .where(VoucherLine.voucher_id == voucher_id))


async def test_draft_not_posted_until_approved(session):
    """초안/상신은 원장 라인 미생성 → 집계 자연 제외. 승인 시에만 전기."""
    v = await svc.create_draft(session, source_ref="m-1", voucher_on=date(2026, 5, 2),
                               lines=LINES, created_by=1)
    assert v.status == "DRAFT" and v.draft_lines and await _count_lines(session, v.id) == 0
    await svc.submit(session, voucher_id=v.id, actor=1)
    assert v.status == "PENDING" and await _count_lines(session, v.id) == 0   # 아직 미전기
    await svc.approve(session, voucher_id=v.id, actor=2)                       # 승인자≠작성자
    assert v.status == "POSTED" and v.approved_by == 2
    assert await _count_lines(session, v.id) == 2                              # 승인 시 전기


async def test_approve_sod_blocks_creator(session):
    """직무분리(SoD) — 작성자·상신자는 본인 전표 승인 불가."""
    v = await svc.create_draft(session, source_ref="m-2", voucher_on=date(2026, 5, 2),
                               lines=LINES, created_by=7)
    await svc.submit(session, voucher_id=v.id, actor=7)
    with pytest.raises(ValidationError):
        await svc.approve(session, voucher_id=v.id, actor=7)                   # 작성자 자기승인 차단
    assert v.status == "PENDING" and await _count_lines(session, v.id) == 0


async def test_reject_keeps_out_of_ledger(session):
    """반려 — PENDING→REJECTED, 사유 보존, 원장 미반영."""
    v = await svc.create_draft(session, source_ref="m-3", voucher_on=date(2026, 5, 2),
                               lines=LINES, created_by=1)
    await svc.submit(session, voucher_id=v.id, actor=1)
    await svc.reject(session, voucher_id=v.id, reason="계정 오류", actor=2)
    assert v.status == "REJECTED" and v.reject_reason == "계정 오류"
    assert await _count_lines(session, v.id) == 0


async def test_draft_unbalanced_rejected(session):
    """초안 단계에서 차대 불일치 선검증."""
    with pytest.raises(ValidationError):
        await svc.create_draft(session, source_ref="m-4", voucher_on=date(2026, 5, 2),
                               lines=[VoucherLineDTO("1110", "DEBIT", 100),
                                      VoucherLineDTO("4110", "CREDIT", 90)], created_by=1)


async def test_approve_requires_pending(session):
    """DRAFT(미상신)는 바로 승인 불가 — 상태기계 강제."""
    v = await svc.create_draft(session, source_ref="m-5", voucher_on=date(2026, 5, 2),
                               lines=LINES, created_by=1)
    with pytest.raises(ValidationError):
        await svc.approve(session, voucher_id=v.id, actor=2)


async def test_auto_voucher_still_immediate_posted(session):
    """자동전표(create) 경로는 승인 우회 — 즉시 POSTED(원천 통제)."""
    v = await svc.create(session, source_module="patient", source_ref="auto-1",
                         voucher_on=date(2026, 5, 2), lines=LINES)
    assert v.status == "POSTED" and await _count_lines(session, v.id) == 2


async def test_g9_line_dimensions_persist_through_paths(session):
    """G9 Phase0 — cost_center/project가 create·draft→approve·reverse 전 경로에서 보존."""
    dl = [VoucherLineDTO("1110", "DEBIT", 5000, cost_center="CC-IM", project="P-2026"),
          VoucherLineDTO("4110", "CREDIT", 5000, cost_center="CC-IM", project="P-2026")]
    # 1) create 직접 전기
    await svc.create(session, source_module="manual", source_ref="dim-1",
                     voucher_on=date(2026, 6, 7), lines=dl)
    ln = (await session.execute(select(VoucherLine).where(
        VoucherLine.account_code == "1110"))).scalars().first()
    assert ln.cost_center == "CC-IM" and ln.project == "P-2026"

    # 2) draft → approve (draft_lines JSON 경유)
    d = await svc.create_draft(session, source_ref="dim-draft", voucher_on=date(2026, 6, 8),
                               lines=dl, created_by=1)
    await svc.submit(session, voucher_id=d.id, actor=1)
    await svc.approve(session, voucher_id=d.id, actor=2)
    appr = (await session.execute(select(VoucherLine).where(
        VoucherLine.voucher_id == d.id, VoucherLine.account_code == "4110"))).scalars().first()
    assert appr.cost_center == "CC-IM" and appr.project == "P-2026"

    # 3) reverse — 역분개도 차원 보존
    orig = await svc.create(session, source_module="manual", source_ref="dim-rev",
                            voucher_on=date(2026, 6, 9), lines=dl)
    rev = await svc.reverse(session, voucher_id=orig.id, reason="정정", created_by=1)
    rln = (await session.execute(select(VoucherLine).where(
        VoucherLine.voucher_id == rev.id))).scalars().first()
    assert rln.cost_center == "CC-IM" and rln.project == "P-2026"


async def test_large_voucher_requires_admin_approval(session):
    """거액 전표 materiality 게이트 — 임계(5천만) 이상은 ADMIN/DIRECTOR만 승인."""
    big = [VoucherLineDTO("1110", "DEBIT", 60_000_000),
           VoucherLineDTO("4110", "CREDIT", 60_000_000)]
    d = await svc.create_draft(session, source_ref="big-1", voucher_on=date(2026, 6, 7),
                               lines=big, created_by=1)
    await svc.submit(session, voucher_id=d.id, actor=1)
    with pytest.raises(ValidationError, match="거액 전표"):
        await svc.approve(session, voucher_id=d.id, actor=2, actor_role="FINANCE_MANAGER")
    v = await svc.approve(session, voucher_id=d.id, actor=2, actor_role="ADMIN")
    assert v.status == "POSTED"


async def test_small_voucher_any_writer_approves(session):
    small = [VoucherLineDTO("1110", "DEBIT", 1000), VoucherLineDTO("4110", "CREDIT", 1000)]
    d = await svc.create_draft(session, source_ref="sm-1", voucher_on=date(2026, 6, 7),
                               lines=small, created_by=1)
    await svc.submit(session, voucher_id=d.id, actor=1)
    v = await svc.approve(session, voucher_id=d.id, actor=2, actor_role="FINANCE_MANAGER")
    assert v.status == "POSTED"


async def test_voucher_multistep_approval_optin(session):
    """opt-in 다단계 — VOUCHER 결재룰(FM→ADMIN) 있으면 단계 승인·완주 시 전기."""
    from erp.common.models import ApprovalRule
    from tests._http import make_user
    session.add(ApprovalRule(doc_type="VOUCHER", min_amount=0, max_amount=10 ** 12, active=True,
                steps=[{"type": "ROLE", "value": "FINANCE_MANAGER"}, {"type": "ROLE", "value": "ADMIN"}]))
    await make_user(session, user_id=1, role="STAFF")
    await make_user(session, user_id=10, role="FINANCE_MANAGER")
    await make_user(session, user_id=11, role="ADMIN")
    await session.flush()
    d = await svc.create_draft(session, source_ref="ms-1", voucher_on=date(2026, 6, 7),
                               lines=LINES, created_by=1)
    await svc.submit(session, voucher_id=d.id, actor=1)
    v = await svc.approve(session, voucher_id=d.id, actor=10, actor_role="FINANCE_MANAGER")
    assert v.status == "PENDING"                     # 1단계 승인 → 다음 단계 대기(미전기)
    assert (await session.execute(select(VoucherLine).where(
        VoucherLine.voucher_id == d.id))).scalars().first() is None
    v = await svc.approve(session, voucher_id=d.id, actor=11, actor_role="ADMIN")
    assert v.status == "POSTED"                       # 2단계 완주 → 전기
    assert len((await session.execute(select(VoucherLine).where(
        VoucherLine.voucher_id == d.id))).scalars().all()) == 2


async def test_voucher_no_rule_legacy_single_approve(session):
    """VOUCHER 룰 없음(기본) → 레거시 단일 승인(기존 동작 불변)."""
    d = await svc.create_draft(session, source_ref="leg-1", voucher_on=date(2026, 6, 7),
                               lines=LINES, created_by=1)
    await svc.submit(session, voucher_id=d.id, actor=1)
    v = await svc.approve(session, voucher_id=d.id, actor=2, actor_role="ADMIN")
    assert v.status == "POSTED"


async def test_voucher_multistep_reject(session):
    """다단계 진행 중 반려 — 단계 반려 기록 + 전표 REJECTED."""
    from erp.common.models import ApprovalRule
    from tests._http import make_user
    session.add(ApprovalRule(doc_type="VOUCHER", min_amount=0, max_amount=10 ** 12, active=True,
                steps=[{"type": "ROLE", "value": "FINANCE_MANAGER"}, {"type": "ROLE", "value": "ADMIN"}]))
    await make_user(session, user_id=1, role="STAFF")
    await make_user(session, user_id=10, role="FINANCE_MANAGER")
    await session.flush()
    d = await svc.create_draft(session, source_ref="msr-1", voucher_on=date(2026, 6, 7),
                               lines=LINES, created_by=1)
    await svc.submit(session, voucher_id=d.id, actor=1)
    v = await svc.reject(session, voucher_id=d.id, reason="반려사유", actor=10)
    assert v.status == "REJECTED"
