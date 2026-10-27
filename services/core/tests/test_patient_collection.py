"""환자 미수금 회수 워크플로 — 독촉·분납·개별 대손상각 (마이그 0209·갭 감사 원무 ①)."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.models import Receivable, Voucher, VoucherLine
from erp.patient.collection_service import PatientCollectionService

svc = PatientCollectionService()
TODAY = date(2026, 7, 20)


async def _rec(session, *, amount=100_000, collected=0, due=date(2026, 5, 31),
               chart="C-1001", module="patient"):
    r = Receivable(party=chart, description="환자 본인부담 2026-05", amount=amount,
                   collected_amount=collected, billed_on=due, due_on=due,
                   status="PARTIAL" if collected else "OUTSTANDING",
                   source_module=module, source_ref=f"patient-ar:{chart}:2026-05")
    session.add(r)
    await session.flush()
    return r


async def test_overdue_list_with_dunning_and_plan_flags(session):
    r = await _rec(session, amount=100_000, collected=30_000)
    await _rec(session, chart="C-2", due=date(2026, 7, 25))          # 미도래 — 제외
    await svc.record_dunning(session, receivable_id=r.id, method="SMS", sent_on=TODAY)
    out = await svc.overdue_list(session, as_of=TODAY)
    assert out["count"] == 1
    item = out["items"][0]
    assert item["outstanding"] == 70_000 and item["days_overdue"] == 50
    assert item["dunning"]["level"] == 1 and item["has_active_plan"] is False


async def test_dunning_level_increments(session):
    r = await _rec(session)
    n1 = await svc.record_dunning(session, receivable_id=r.id, sent_on=TODAY)
    n2 = await svc.record_dunning(session, receivable_id=r.id, method="MAIL", sent_on=TODAY)
    assert (n1.level, n2.level) == (1, 2)
    hist = await svc.dunning_history(session, receivable_id=r.id)
    assert [h["level"] for h in hist] == [1, 2]


async def test_dunning_rejects_non_patient_and_closed(session):
    other = await _rec(session, module="claims")
    with pytest.raises(ValidationError):
        await svc.record_dunning(session, receivable_id=other.id)
    done = await _rec(session, chart="C-3", collected=100_000)
    done.status = "COLLECTED"
    with pytest.raises(ValidationError):
        await svc.record_dunning(session, receivable_id=done.id)


async def test_installment_plan_single_active(session):
    r = await _rec(session, amount=90_000)
    p = await svc.create_plan(session, receivable_id=r.id, months=3, start_month="2026-08")
    assert float(p.monthly_amount) == 30_000 and p.status == "ACTIVE"
    with pytest.raises(ValidationError):        # ACTIVE 중복 차단
        await svc.create_plan(session, receivable_id=r.id, months=6, start_month="2026-09")
    await svc.update_plan_status(session, plan_id=p.id, status="CANCELED")
    p2 = await svc.create_plan(session, receivable_id=r.id, months=2, start_month="2026-09")
    assert p2.status == "ACTIVE"                # 종결 후 재수립 가능
    with pytest.raises(ValidationError):        # 종결 상태 재전이 불가
        await svc.update_plan_status(session, plan_id=p.id, status="COMPLETED")


async def test_write_off_uses_allowance_first(session):
    """상각 회계 — 충당금(1190) 잔액 우선 상계·부족분 5690·CR 1131."""
    from erp.finance.service import VoucherLineDTO, VoucherService
    # 충당금 40,000 선적립(5690/1190)
    await VoucherService().create(
        session, source_module="test", source_ref="allow-1", voucher_on=date(2026, 6, 30),
        lines=[VoucherLineDTO("5690", "DEBIT", 40_000),
               VoucherLineDTO("1190", "CREDIT", 40_000)])
    r = await _rec(session, amount=100_000, collected=10_000)   # 미수 90,000
    out = await svc.write_off(session, receivable_id=r.id, reason="소멸시효 완성", on=TODAY)
    assert out["written_off"] == 90_000
    assert out["allowance_used"] == 40_000 and out["bad_debt_expense"] == 50_000
    assert r.status == "WRITTEN_OFF" and r.written_off_on == TODAY
    lines = (await session.execute(
        select(VoucherLine).join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.source_ref == f"wo:{r.id}"))).scalars().all()
    got = {(ln.account_code, ln.side, float(ln.amount)) for ln in lines}
    assert got == {("1190", "DEBIT", 40_000), ("5690", "DEBIT", 50_000),
                   ("1131", "CREDIT", 90_000)}
    # 멱등 — 재호출 시 이중 전기 없음
    again = await svc.write_off(session, receivable_id=r.id, reason="재시도", on=TODAY)
    assert again.get("already") is True


async def test_write_off_no_allowance_full_expense(session):
    """충당금 0이면 전액 5690 대손상각비."""
    r = await _rec(session, amount=50_000)
    out = await svc.write_off(session, receivable_id=r.id, reason="파산", on=TODAY)
    assert out["allowance_used"] == 0 and out["bad_debt_expense"] == 50_000


async def test_written_off_excluded_from_aging(session):
    """상각 채권은 aging(현재상태) 제외 — 미수 잔액 왜곡 방지."""
    from erp.finance.settlement_service import SettlementService
    r = await _rec(session, amount=80_000)
    await _rec(session, chart="C-9", amount=20_000)
    await svc.write_off(session, receivable_id=r.id, reason="소멸시효", on=TODAY)
    aging = await SettlementService().aging(session, as_of=TODAY, kind="AR",
                                           source_module="patient")
    assert aging["total"] == 20_000              # 상각분 80,000 제외


async def test_accrue_skips_written_off(session, monkeypatch):
    """상각 후 accrue가 죽은 채권을 부활·재전기하지 않음(동결)."""
    from erp.config import get_settings
    from erp.patient.receivable_service import PatientReceivableService
    monkeypatch.setattr(get_settings(), "patient_accrual_enabled", True, raising=False)
    r = await _rec(session, chart="C-1001", amount=100_000)
    await svc.write_off(session, receivable_id=r.id, reason="소멸시효", on=TODAY)
    out = await PatientReceivableService().accrue(session, chart_no="C-1001",
                                                  care_month="2026-05")
    assert out.get("skipped") == "written_off"
    assert r.status == "WRITTEN_OFF"             # 상태 불변
