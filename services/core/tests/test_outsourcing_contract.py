"""위수탁 계약 — 등록·갱신·해지·만기임박·월 위탁료 미지급 발생(멱등)."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.models import Payable
from erp.scm.contract_service import OutsourcingContractService

svc = OutsourcingContractService()


async def _contract(session, **kw):
    defaults = dict(vendor_name="청정급식", category="MEAL", title="직원식당 위탁운영",
                    monthly_amount=8_000_000, start_on=date(2026, 1, 1),
                    end_on=date(2026, 12, 31))
    defaults.update(kw)
    return await svc.register(session, actor=1, **defaults)


async def test_register_validation_and_list(session):
    c = await _contract(session)
    await _contract(session, vendor_name="메디세탁", category="LAUNDRY",
                    title="세탁 위탁", monthly_amount=2_000_000,
                    end_on=date(2026, 7, 31))                      # 만기 임박(60일 내)
    with pytest.raises(ValidationError, match="종료일"):
        await _contract(session, end_on=date(2025, 12, 31))

    out = await svc.list(session, today=date(2026, 6, 11))
    assert out["monthly_total"] == 10_000_000
    by = {b["category"]: b["monthly"] for b in out["by_category"]}
    assert by["MEAL"] == 8_000_000 and by["LAUNDRY"] == 2_000_000
    soon = {i["vendor_name"] for i in out["expiring_soon"]}
    assert soon == {"메디세탁"}                                    # 50일 후 만료만 임박
    assert c.id is not None


async def test_renew_terminate_and_expired_state(session):
    c = await _contract(session, end_on=date(2026, 5, 31))
    out = await svc.list(session, today=date(2026, 6, 11))
    assert out["items"][0]["status"] == "EXPIRED"                  # 만료 표시 + 월비용 제외
    assert out["monthly_total"] == 0

    await svc.renew(session, actor=1, contract_id=c.id, new_end_on=date(2027, 5, 31))
    out2 = await svc.list(session, today=date(2026, 6, 11))
    assert out2["items"][0]["status"] == "ACTIVE"
    with pytest.raises(ValidationError, match="이후"):
        await svc.renew(session, actor=1, contract_id=c.id, new_end_on=date(2027, 1, 1))

    await svc.terminate(session, actor=1, contract_id=c.id)
    with pytest.raises(ValidationError, match="이미 해지"):
        await svc.terminate(session, actor=1, contract_id=c.id)


async def test_renew_from_terminated_blocked(session):
    """심층 재검토 HIGH — 해지된 계약은 갱신으로 부활 불가(상태머신 우회·과금 재개 차단)."""
    c = await _contract(session, end_on=date(2026, 12, 31))
    await svc.terminate(session, actor=1, contract_id=c.id)
    assert c.status == "TERMINATED"
    with pytest.raises(ValidationError, match="갱신 가능 상태"):
        await svc.renew(session, actor=1, contract_id=c.id, new_end_on=date(2027, 12, 31))


async def test_terminate_draft_bypasses_blocked(session):
    """심층 재검토 — 미발효(DRAFT) 계약은 해지 우회 불가(상태머신 커널 경유·반려 경로 사용)."""
    c = await _contract(session, require_approval=True)   # DRAFT
    assert c.status == "DRAFT"
    with pytest.raises(ValidationError):
        await svc.terminate(session, actor=1, contract_id=c.id)


async def test_accrue_month_idempotent_payable(session):
    c = await _contract(session)
    out = await svc.accrue_month(session, actor=1, contract_id=c.id, yyyymm="2026-06")
    assert out["duplicate"] is False and out["due_on"] == date(2026, 7, 10)

    dup = await svc.accrue_month(session, actor=1, contract_id=c.id, yyyymm="2026-06")
    assert dup["duplicate"] is True and dup["payable_id"] == out["payable_id"]

    ap = await session.get(Payable, out["payable_id"])
    assert float(ap.amount) == 8_000_000 and ap.party == "청정급식"
    assert (await session.scalar(select(Payable.source_ref).where(
        Payable.id == ap.id))) == f"contract:{c.id}:2026-06"

    await svc.terminate(session, actor=1, contract_id=c.id)
    c.terminated_on = date(2026, 6, 30)            # 해지일 고정(실시계 의존 제거)
    await session.flush()
    # 해지일 이후 달은 발생 불가 — 해지 전 유효했던 달(늦은 월마감)은 허용(3차 검토 E)
    with pytest.raises(ValidationError):
        await svc.accrue_month(session, actor=1, contract_id=c.id, yyyymm="2026-07")
