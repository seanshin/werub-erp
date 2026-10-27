"""위수탁계약 생애주기 — 승인 결재(직무분리)·발효·금액변경·전이 가드."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.models import Payable
from erp.common.models import ContractAmendment
from erp.scm.contract_service import OutsourcingContractService

svc = OutsourcingContractService()


async def _draft(session, **kw):
    defaults = dict(actor=1, vendor_name="청정급식", category="MEAL", title="식당 위탁",
                    monthly_amount=8_000_000, start_on=date(2026, 1, 1), end_on=date(2026, 12, 31),
                    require_approval=True)
    defaults.update(kw)
    return await svc.register(session, **defaults)


async def test_approval_flow_with_segregation(session):
    c = await _draft(session)
    assert c.status == "DRAFT"
    await svc.submit_for_approval(session, actor=1, contract_id=c.id)
    assert c.status == "PENDING_APPROVAL"
    with pytest.raises(ValidationError, match="직무분리"):     # 작성자(1) 본인 승인 차단
        await svc.approve(session, actor=1, contract_id=c.id)
    await svc.approve(session, actor=2, contract_id=c.id)
    assert c.status == "APPROVED" and c.approved_by == 2
    # 발효(발효일 도래)
    await svc.activate(session, actor=2, contract_id=c.id, on=date(2026, 1, 1))
    assert c.status == "ACTIVE"


async def test_activate_before_effective_blocked(session):
    c = await _draft(session, start_on=date(2026, 6, 1))
    await svc.submit_for_approval(session, actor=1, contract_id=c.id)
    await svc.approve(session, actor=2, contract_id=c.id)
    with pytest.raises(ValidationError, match="발효일"):
        await svc.activate(session, actor=2, contract_id=c.id, on=date(2026, 5, 1))


async def test_activate_requires_approved_not_draft(session):
    # 보안: DRAFT 계약 직접 발효 차단(결재선 우회 방지)
    c = await _draft(session)        # DRAFT(require_approval)
    with pytest.raises(ValidationError, match="승인된 계약만"):
        await svc.activate(session, actor=2, contract_id=c.id, on=date(2026, 6, 1))


async def test_illegal_transition_blocked(session):
    c = await svc.register(session, actor=1, vendor_name="X", category="MEAL", title="t",
                           monthly_amount=100, start_on=date(2026, 1, 1),
                           end_on=date(2026, 12, 31))     # ACTIVE
    with pytest.raises(ValidationError, match="전이"):
        await svc.submit_for_approval(session, actor=1, contract_id=c.id)  # ACTIVE→PENDING 불가


async def test_amend_amount_records_history_and_affects_accrual(session):
    c = await svc.register(session, actor=1, vendor_name="X", category="MEAL", title="t",
                           monthly_amount=8_000_000, start_on=date(2026, 1, 1),
                           end_on=date(2026, 12, 31))     # 기본 ACTIVE
    await svc.amend_amount(session, actor=1, contract_id=c.id, new_monthly=9_000_000,
                           effective_on=date(2026, 7, 1), reason="물가 인상")
    assert float(c.monthly_amount) == 9_000_000
    am = (await session.execute(select(ContractAmendment).where(
        ContractAmendment.contract_id == c.id))).scalar_one()
    assert am.change_type == "AMOUNT" and am.new_value == "9000000"
    out = await svc.accrue_month(session, actor=1, contract_id=c.id, yyyymm="2026-07")
    ap = await session.get(Payable, out["payable_id"])
    assert float(ap.amount) == 9_000_000      # 변경 단가 반영


async def test_lifecycle_batch_expiring_expired_renew(session):
    # 만기 임박(D-30) → EXPIRING
    soon = await svc.register(session, actor=1, vendor_name="A", category="MEAL", title="t",
                              monthly_amount=100, start_on=date(2026, 1, 1),
                              end_on=date(2026, 7, 20))
    # 만기 경과·auto_renew 아님 → EXPIRED
    gone = await svc.register(session, actor=1, vendor_name="B", category="MEAL", title="t",
                              monthly_amount=100, start_on=date(2025, 1, 1),
                              end_on=date(2026, 6, 1))
    # 만기 경과·auto_renew → 갱신(1년)
    rn = await svc.register(session, actor=1, vendor_name="C", category="MEAL", title="t",
                            monthly_amount=100, start_on=date(2025, 1, 1),
                            end_on=date(2026, 6, 1), auto_renew=True)
    res = await svc.run_lifecycle_transitions(session, today=date(2026, 6, 29))
    assert res == {"expiring": 1, "renewed": 1, "expired": 1}
    assert soon.status == "EXPIRING"
    assert gone.status == "EXPIRED"
    assert rn.status == "ACTIVE" and rn.end_on == date(2027, 6, 1)
    # 멱등 — 재실행 시 추가 전이 없음
    res2 = await svc.run_lifecycle_transitions(session, today=date(2026, 6, 29))
    assert res2 == {"expiring": 0, "renewed": 0, "expired": 0}


async def test_one_time_purchase_contract_excluded_from_monthly(session):
    # 일회성 구매계약(ONE_TIME) — 생애주기는 동일하되 월 accrue 제외
    c = await svc.register(session, actor=1, vendor_name="장비사", category="IT",
                           title="CT 장비 구매", monthly_amount=300_000_000,
                           start_on=date(2026, 1, 1), end_on=date(2026, 12, 31),
                           contract_type="PURCHASE", billing_type="ONE_TIME")
    assert c.contract_type == "PURCHASE" and c.billing_type == "ONE_TIME"
    with pytest.raises(ValidationError, match="일회성"):
        await svc.accrue_month(session, actor=1, contract_id=c.id, yyyymm="2026-03")
    # 월 정기 합계(monthly_total)에서도 제외
    out = await svc.list(session, today=date(2026, 1, 15))
    assert out["monthly_total"] == 0.0
    row = next(i for i in out["items"] if i["id"] == c.id)
    assert row["contract_type"] == "PURCHASE" and row["billing_type"] == "ONE_TIME"


async def test_service_contract_monthly_still_accrues(session):
    # 일반 용역계약(SERVICE·MONTHLY) — 월 accrue 정상
    c = await svc.register(session, actor=1, vendor_name="용역사", category="OTHER",
                           title="시설 유지보수", monthly_amount=2_000_000,
                           start_on=date(2026, 1, 1), end_on=date(2026, 12, 31),
                           contract_type="SERVICE", billing_type="MONTHLY")
    out = await svc.accrue_month(session, actor=1, contract_id=c.id, yyyymm="2026-03")
    assert out["duplicate"] is False and out["amount"] == 2_000_000


async def test_register_rejects_bad_category(session):
    with pytest.raises(ValidationError, match="분류"):
        await svc.register(session, actor=1, vendor_name="X", category="BOGUS", title="t",
                           monthly_amount=100, start_on=date(2026, 1, 1), end_on=date(2026, 12, 31))


async def test_accrue_month_rejects_bad_yyyymm(session):
    c = await svc.register(session, actor=1, vendor_name="X", category="MEAL", title="t",
                           monthly_amount=100, start_on=date(2026, 1, 1), end_on=date(2026, 12, 31))
    with pytest.raises(ValidationError, match="yyyymm"):
        await svc.accrue_month(session, actor=1, contract_id=c.id, yyyymm="202612")


async def test_lifecycle_renew_handles_leap_day(session):
    # 윤년 2/29 auto_renew — 비윤년 말일로 클램프(배치 크래시 방지)
    c = await svc.register(session, actor=1, vendor_name="X", category="MEAL", title="t",
                           monthly_amount=100, start_on=date(2027, 3, 1),
                           end_on=date(2028, 2, 29), auto_renew=True)   # 2028 윤년
    res = await svc.run_lifecycle_transitions(session, today=date(2028, 3, 1))
    assert res["renewed"] == 1
    assert c.end_on == date(2029, 2, 28) and c.status == "ACTIVE"   # 클램프


async def test_terminate_with_reason(session):
    c = await svc.register(session, actor=1, vendor_name="X", category="MEAL", title="t",
                           monthly_amount=100, start_on=date(2026, 1, 1), end_on=date(2026, 12, 31))
    await svc.terminate(session, actor=1, contract_id=c.id, reason="품질 미달")
    assert c.status == "TERMINATED" and c.terminate_reason == "품질 미달" and c.terminated_on
