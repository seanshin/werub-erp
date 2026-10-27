"""전역 차대균형 불변식 (P2-12) — 어떤 전기 경로도 불균형 전표를 만들 수 없음을 보증.

(1) 중앙 가드: VoucherService.create/create_draft가 차대 불일치를 거부(불변식 강제점).
(2) 도메인 스윕: 대표 전기 플로우(AR·외화·충당부채)를 실제 서비스로 돌린 뒤, 저장된 모든 전표를
    조회해 전표별 ΣDEBIT==ΣCREDIT을 확인. 서비스가 VoucherLine을 직접 만들어 가드를 우회해도 적발.
"""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.errors import ValidationError
from erp.finance.models import Account, CorporateAccount, Voucher, VoucherLine
from erp.finance.service import VoucherLineDTO, VoucherService

_ACCS = [("1110", "현금성자산", "ASSET"), ("1130", "미수금", "ASSET"),
         ("1115", "외화예금", "ASSET"), ("2110", "미지급금", "LIABILITY"),
         ("2130", "기타충당부채", "LIABILITY"), ("4110", "외래수익", "REVENUE"),
         ("5680", "충당부채전입액", "EXPENSE")]


async def _seed_accounts(session):
    for code, name, kind in _ACCS:
        session.add(Account(code=code, name=name, kind=kind))
    await session.flush()


async def test_central_guard_rejects_unbalanced(session):
    """VoucherService.create — 차대 불일치는 ValidationError(중앙 강제점)."""
    await _seed_accounts(session)
    vs = VoucherService()
    with pytest.raises(ValidationError, match="차변·대변"):
        await vs.create(session, source_module="test", source_ref="unbal-1",
                        voucher_on=date(2026, 6, 1),
                        lines=[VoucherLineDTO("1110", "DEBIT", 100),
                               VoucherLineDTO("4110", "CREDIT", 90)])  # 100≠90


async def test_central_guard_draft_rejects_unbalanced(session):
    """create_draft도 초안 단계에서 선검증(조기 오류)."""
    await _seed_accounts(session)
    vs = VoucherService()
    with pytest.raises(ValidationError, match="차변·대변"):
        await vs.create_draft(session, source_ref="unbal-draft", voucher_on=date(2026, 6, 1),
                              lines=[VoucherLineDTO("1110", "DEBIT", 100),
                                     VoucherLineDTO("4110", "CREDIT", 1)], created_by=1)


async def test_all_posted_vouchers_balanced_across_domains(session):
    """도메인 스윕 — 대표 전기 플로우 후 저장된 전 전표가 전표별 균형."""
    from erp.finance.disclosure_service import DisclosureService
    from erp.finance.forex_service import ForexTranslationService
    from erp.finance.settlement_service import SettlementService
    await _seed_accounts(session)

    # (1) AR — 미수금 인식 + 수금
    settle = SettlementService()
    r = await settle.record_receivable(session, party="TEST-CO", amount=1000,
                                       billed_on=date(2026, 6, 1), due_on=date(2026, 6, 30))
    await settle.collect(session, receivable_id=r.id, amount=400, on=date(2026, 6, 15))

    # (2) 외화 입금
    acct = CorporateAccount(alias="T USD", bank_name="T", account_no="T-1",
                            currency="USD", gl_account_code="1115")
    session.add(acct)
    await session.flush()
    await ForexTranslationService().record_movement(
        session, account_id=acct.id, direction="IN", fc_amount=100, rate=1350,
        on=date(2026, 6, 10), actor=1)

    # (3) 충당부채 인식
    disc = DisclosureService()
    c = await disc.add_contingency(session, period="2026", kind="LIABILITY",
                                   description="소송", estimate=30000, likelihood="높음")
    await disc.recognize_provision(session, contingency_id=c["id"], on_date=date(2026, 12, 31))
    await session.flush()

    # 스윕 — 전표별 ΣDEBIT vs ΣCREDIT
    vouchers = (await session.scalars(select(Voucher.id))).all()
    assert len(vouchers) >= 3           # 최소 3개 플로우가 전표 생성
    for vid in vouchers:
        d = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
            VoucherLine.voucher_id == vid, VoucherLine.side == "DEBIT"))
        c_ = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
            VoucherLine.voucher_id == vid, VoucherLine.side == "CREDIT"))
        assert float(d) == float(c_), f"전표 {vid} 불균형: DR {d} ≠ CR {c_}"

    # 전역 합계도 균형(이중 안전)
    total_d = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.side == "DEBIT"))
    total_c = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0)).where(
        VoucherLine.side == "CREDIT"))
    assert float(total_d) == float(total_c)
