"""AP 지급결의 배치 (P3-4) — 결의·SoD 승인·실행 전기·이체파일·중복포함 차단."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.disbursement_service import DisbursementService
from erp.finance.models import Payable, Voucher
from erp.finance.settlement_service import SettlementService

svc = DisbursementService()
settle = SettlementService()


async def _payable(session, party, amount):
    p = await settle.record_payable(session, party=party, amount=amount,
                                    incurred_on=date(2026, 6, 1), due_on=date(2026, 6, 30),
                                    description="테스트", expense_account="5110")
    return p


async def test_create_approve_execute_flow(session):
    p1 = await _payable(session, "A상사", 100000)
    p2 = await _payable(session, "B상사", 50000)
    batch = await svc.create_batch(session, payable_ids=[p1.id, p2.id],
                                   scheduled_on=date(2026, 7, 10), actor=1)
    assert batch.status == "DRAFT" and float(batch.total_amount) == 150000 and batch.line_count == 2
    assert batch.batch_no.startswith("DISB-202607-")
    # SoD — 작성자 본인 승인 불가
    with pytest.raises(ValidationError):
        await svc.approve(session, batch_id=batch.id, actor=1)
    b = await svc.approve(session, batch_id=batch.id, actor=2)
    assert b.status == "APPROVED"
    # 실행 — 지급 전기(DR 미지급/CR 현금) + 미지급금 PAID
    out = await svc.execute(session, batch_id=batch.id, on=date(2026, 7, 10), actor=2)
    assert out["status"] == "EXECUTED" and out["paid_total"] == 150000
    for pid in (p1.id, p2.id):
        p = await session.get(Payable, pid)
        assert p.status == "PAID"
    # 지급 전표 존재(ap-pay-*)
    vs = (await session.scalars(select(Voucher).where(
        Voucher.source_ref.like(f"ap-pay-{p1.id}-%")))).all()
    assert len(vs) == 1


async def test_duplicate_active_batch_blocked(session):
    p = await _payable(session, "C상사", 30000)
    await svc.create_batch(session, payable_ids=[p.id], scheduled_on=date(2026, 7, 10), actor=1)
    # 같은 미지급금을 다른 배치에 재포함 차단
    with pytest.raises(ValidationError):
        await svc.create_batch(session, payable_ids=[p.id], scheduled_on=date(2026, 7, 11), actor=1)


async def test_canceled_batch_releases_payable(session):
    p = await _payable(session, "D상사", 20000)
    b = await svc.create_batch(session, payable_ids=[p.id], scheduled_on=date(2026, 7, 10), actor=1)
    await svc.cancel(session, batch_id=b.id)
    # 취소 후 재포함 가능
    b2 = await svc.create_batch(session, payable_ids=[p.id], scheduled_on=date(2026, 7, 12), actor=1)
    assert b2.status == "DRAFT"


async def test_cannot_cancel_executed(session):
    p = await _payable(session, "E상사", 10000)
    b = await svc.create_batch(session, payable_ids=[p.id], scheduled_on=date(2026, 7, 10), actor=1)
    await svc.approve(session, batch_id=b.id, actor=2)
    await svc.execute(session, batch_id=b.id, on=date(2026, 7, 10), actor=2)
    with pytest.raises(ValidationError):
        await svc.cancel(session, batch_id=b.id)


async def test_export_csv(session):
    p = await _payable(session, "F,상사", 12345)   # 콤마 포함 이름 → 치환
    b = await svc.create_batch(session, payable_ids=[p.id], scheduled_on=date(2026, 7, 10),
                               accounts={p.id: "신한 110-123"}, actor=1)
    await svc.approve(session, batch_id=b.id, actor=2)   # 이체파일은 승인 배치만(2026-10-09 검토 E)
    csv = await svc.export_csv(session, batch_id=b.id)
    lines = csv.strip().split("\n")
    assert lines[0] == "거래처,금액,지급예정일,계좌,미지급ID"
    assert "F 상사,12345,2026-07-10,신한 110-123," in lines[1]


async def test_execute_requires_approved(session):
    p = await _payable(session, "G상사", 5000)
    b = await svc.create_batch(session, payable_ids=[p.id], scheduled_on=date(2026, 7, 10), actor=1)
    with pytest.raises(ValidationError):        # DRAFT 상태 실행 불가
        await svc.execute(session, batch_id=b.id, on=date(2026, 7, 10), actor=2)
