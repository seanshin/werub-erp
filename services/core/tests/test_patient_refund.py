"""환불(과오납 반환) — 1급 거래 + 즉시 역전표(현금 반환)·과다환불 상한·멱등·소속검증."""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.config import get_settings
from erp.errors import ValidationError
from erp.finance.models import VoucherLine
from erp.patient.models import Patient, Payment, Reception, Refund
from erp.patient.service import RefundService

svc = RefundService()


async def _paid_patient(session, chart_no, amount, method="CASH"):
    """환자 + 접수 + 수납(PAID) 시드 — 환불 상한(순수납) 기준."""
    p = Patient(chart_no=chart_no, name="환자", birth_date=date(1990, 1, 1))
    session.add(p)
    await session.flush()
    rc = Reception(patient_id=p.id, dept_code="IM", received_on=date(2026, 6, 15), source="HIS")
    session.add(rc)
    await session.flush()
    session.add(Payment(reception_id=rc.id, amount=amount, method=method, status="PAID",
                        paid_on=date(2026, 6, 15)))
    await session.flush()
    return p, rc


async def _gl(session, code):
    d, c = (await session.execute(select(
        func.coalesce(func.sum(VoucherLine.amount).filter(VoucherLine.side == "DEBIT"), 0),
        func.coalesce(func.sum(VoucherLine.amount).filter(VoucherLine.side == "CREDIT"), 0))
        .where(VoucherLine.account_code == code))).one()
    return float(d) - float(c)


async def test_refund_posts_reversing_voucher_cash_basis(session):
    await _paid_patient(session, "C-1", 15_000)
    ref = await svc.refund(session, chart_no="C-1", amount=15_000, reason="과오납 반환",
                           refunded_on=date(2026, 6, 20), actor=1)
    await session.flush()
    assert ref.voucher_ref is not None
    # 현금주의(기본): DR 외래수익(4110) / CR 현금(1110)
    assert await _gl(session, "4110") == 15_000      # 수익 차감(차변)
    assert await _gl(session, "1110") == -15_000     # 현금 반환(대변)
    row = (await session.execute(select(Refund))).scalar_one()
    assert float(row.amount) == 15_000 and row.reason == "과오납 반환"


async def test_refund_card_credits_card_ar(session):
    """계산검토 H-4 — 카드 환불(취소)은 CR 카드미수(1132), 현금(1110) 불변(수납과 대칭)."""
    await _paid_patient(session, "C-CARD", 20_000, method="CARD")
    await svc.refund(session, chart_no="C-CARD", amount=20_000, reason="카드 취소",
                     refunded_on=date(2026, 6, 21), method="CARD", actor=1)
    await session.flush()
    assert await _gl(session, "1132") == -20_000     # 카드미수 감소(대변)
    assert await _gl(session, "1110") == 0           # 현금 시재 불변


async def test_refund_accrual_debits_patient_ar(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "patient_accrual_enabled", True, raising=False)
    await _paid_patient(session, "C-2", 10_000)
    await svc.refund(session, chart_no="C-2", amount=10_000, reason="환불",
                     refunded_on=date(2026, 6, 20), actor=1)
    await session.flush()
    assert await _gl(session, "1131") == 10_000      # 발생주의: 환자미수금 차변(과오납 음수 상계)
    assert await _gl(session, "1110") == -10_000


async def test_refund_guards(session):
    await _paid_patient(session, "C", 100)
    with pytest.raises(ValidationError):
        await svc.refund(session, chart_no="C", amount=0, reason="x", refunded_on=date(2026, 6, 1))
    with pytest.raises(ValidationError, match="사유"):
        await svc.refund(session, chart_no="C", amount=100, reason="", refunded_on=date(2026, 6, 1))


async def test_refund_rejects_unknown_chart(session):
    """환자 소속 검증 — 미존재 차트번호로 가공 환불전표 전기 차단."""
    with pytest.raises(ValidationError, match="존재하지 않는 환자"):
        await svc.refund(session, chart_no="NOPE", amount=1_000, reason="x",
                         refunded_on=date(2026, 6, 1))


async def test_refund_caps_at_net_paid(session):
    """과다환불 상한 — 순수납(PAID) − 기환불 초과 불가(낸 적 없는 금액 유출 차단)."""
    await _paid_patient(session, "C-CAP", 100_000)
    # 초과 요청 거부
    with pytest.raises(ValidationError, match="순수납 잔액"):
        await svc.refund(session, chart_no="C-CAP", amount=150_000, reason="과다",
                         refunded_on=date(2026, 6, 20))
    # 한도 내 부분환불 허용 후, 잔여 초과분 거부(기환불 누계 반영)
    await svc.refund(session, chart_no="C-CAP", amount=60_000, reason="1차",
                     refunded_on=date(2026, 6, 20))
    with pytest.raises(ValidationError, match="순수납 잔액"):
        await svc.refund(session, chart_no="C-CAP", amount=50_000, reason="2차",
                         refunded_on=date(2026, 6, 21))   # 잔여 40k < 50k


async def test_refund_idempotent_on_duplicate(session):
    """멱등 — 동일 chart·접수·금액·일자·사유 재요청(더블클릭)은 기존 반환·현금 이중유출 없음."""
    _p, rc = await _paid_patient(session, "C-DUP", 30_000)
    a = await svc.refund(session, chart_no="C-DUP", amount=30_000, reason="반환",
                         refunded_on=date(2026, 6, 20), reception_id=rc.id)
    b = await svc.refund(session, chart_no="C-DUP", amount=30_000, reason="반환",
                         refunded_on=date(2026, 6, 20), reception_id=rc.id)
    assert a.id == b.id
    assert len((await session.scalars(select(Refund))).all()) == 1
    assert await _gl(session, "1110") == -30_000     # 현금 1회만 유출


async def test_refund_rejects_foreign_reception(session):
    """접수 소속 검증 — 타 환자 접수건 참조 환불 차단."""
    _p, _rc = await _paid_patient(session, "C-A", 50_000)
    _p2, rc_b = await _paid_patient(session, "C-B", 50_000)
    with pytest.raises(ValidationError, match="속하지 않"):
        await svc.refund(session, chart_no="C-A", amount=10_000, reason="x",
                         refunded_on=date(2026, 6, 20), reception_id=rc_b.id)


async def test_refund_blocked_on_closed_day(session):
    """마감 게이트 — pay와 대칭: 마감된 일자로 소급 환불 전기 차단(마감일 원장 오염 방지)."""
    from erp.patient.models import DailyClosing
    await _paid_patient(session, "C-CLS", 30_000)
    session.add(DailyClosing(closing_on=date(2026, 6, 15), seq=1, total_amount=30_000,
                             payment_count=1, closed_by=9))       # status 기본 CLOSED
    await session.flush()
    with pytest.raises(ValidationError, match="마감된 일자"):
        await svc.refund(session, chart_no="C-CLS", amount=10_000, reason="과오납",
                         refunded_on=date(2026, 6, 15))           # 마감일 소급 → 차단
    ref = await svc.refund(session, chart_no="C-CLS", amount=10_000, reason="과오납",
                           refunded_on=date(2026, 6, 16))         # 미마감 익일 → 정상
    assert ref.id is not None


async def test_closing_preview_and_history_show_refunds(session):
    """일계표·마감이력 환불 병기 — 마감보고(수납)와 현금 시재(수납−환불) 대사."""
    from erp.patient.service import ClosingService
    await _paid_patient(session, "C-PRV", 50_000)
    await svc.refund(session, chart_no="C-PRV", amount=20_000, reason="과오납",
                     refunded_on=date(2026, 6, 15))
    closing_svc = ClosingService()
    pv = await closing_svc.preview(session, date(2026, 6, 15))
    assert pv["total"] == 50_000 and pv["refund_total"] == 20_000
    assert pv["refund_count"] == 1 and pv["net_cash"] == 30_000
    await closing_svc.close_day(session, closing_on=date(2026, 6, 15), actor=2)
    hist = await closing_svc.history(session, yyyymm="2026-06")
    assert hist[0]["total_amount"] == 50_000 and hist[0]["refund_total"] == 20_000
