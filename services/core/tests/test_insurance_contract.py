"""보험계약 전용화(0220) — INSURANCE 타입/카테고리·증권번호·보장한도·납입주기·만기임박 통합."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.scm.contract_service import OutsourcingContractService
from erp.scm.models import OutsourcingContract

svc = OutsourcingContractService()


async def test_register_insurance_contract(session):
    """배상책임보험 등록 — INSURANCE 타입·증권번호·보장한도·납입주기 저장."""
    c = await svc.register(
        session, actor=1, vendor_name="한국손해보험", category="INSURANCE",
        contract_type="INSURANCE", billing_type="ONE_TIME", title="의료배상책임보험",
        monthly_amount=12_000_000, start_on=date(2026, 1, 1), end_on=date(2026, 12, 31),
        policy_no="MED-2026-0001", coverage_limit=1_000_000_000, premium_cycle="ANNUAL")
    assert c.contract_type == "INSURANCE" and c.category == "INSURANCE"
    row = await session.get(OutsourcingContract, c.id)
    assert row.policy_no == "MED-2026-0001" and float(row.coverage_limit) == 1_000_000_000
    assert row.premium_cycle == "ANNUAL"


async def test_bad_premium_cycle_rejected(session):
    with pytest.raises(ValidationError, match="납입주기"):
        await svc.register(
            session, actor=1, vendor_name="보험사", category="INSURANCE",
            contract_type="INSURANCE", billing_type="ONE_TIME", title="화재보험",
            monthly_amount=1_000_000, start_on=date(2026, 1, 1), end_on=date(2026, 12, 31),
            premium_cycle="WEEKLY")


async def test_insurance_surfaced_in_list_with_expiry(session):
    """보험계약이 계약 목록에 증권/보장한도와 함께 노출·만기 D-day 통합 관리."""
    await svc.register(
        session, actor=1, vendor_name="한국손보", category="INSURANCE",
        contract_type="INSURANCE", billing_type="ONE_TIME", title="의료기기보험",
        monthly_amount=3_000_000, start_on=date(2026, 1, 1), end_on=date(2026, 12, 31),
        policy_no="EQ-1", coverage_limit=500_000_000, premium_cycle="ANNUAL")
    out = await svc.list(session, today=date(2026, 11, 15))
    ins = next(i for i in out["items"] if i["contract_type"] == "INSURANCE")
    assert ins["policy_no"] == "EQ-1" and ins["coverage_limit"] == 500_000_000
    assert ins["premium_cycle"] == "ANNUAL"
    assert ins["d_day"] == (date(2026, 12, 31) - date(2026, 11, 15)).days
    assert ins["expiring_soon"] is True                    # 만기 60일 이내


async def test_non_insurance_fields_null(session):
    """일반 계약은 보험 필드 NULL."""
    c = await svc.register(session, actor=1, vendor_name="청정급식", category="MEAL",
                           title="식당위탁", monthly_amount=8_000_000,
                           start_on=date(2026, 1, 1), end_on=date(2026, 12, 31))
    row = await session.get(OutsourcingContract, c.id)
    assert row.policy_no is None and row.coverage_limit is None and row.premium_cycle is None
    # ONE_TIME 아니어도 회귀 없음(기존 위수탁 흐름)
    assert (await session.scalar(select(OutsourcingContract.contract_type).where(
        OutsourcingContract.id == c.id))) == "OUTSOURCING"
