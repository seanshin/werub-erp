"""진료의뢰·회송 (진료협력·P3-3) — 등록·유형/기간 필터·회송률 통계·종결·검증."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.patient.referral_service import ReferralService

svc = ReferralService()


async def test_create_and_list_filter(session):
    await svc.create(session, referral_type="REFER_OUT", partner_institution="A대학병원",
                     referred_on=date(2026, 7, 1), chart_no="C1", has_fee=True)
    await svc.create(session, referral_type="REFER_IN", partner_institution="B의원",
                     referred_on=date(2026, 7, 2))
    assert len(await svc.list_referrals(session, referral_type="REFER_OUT")) == 1
    assert len(await svc.list_referrals(session)) == 2
    assert len(await svc.list_referrals(session, start=date(2026, 7, 2))) == 1


async def test_invalid_type_and_partner(session):
    with pytest.raises(ValidationError):
        await svc.create(session, referral_type="BAD", partner_institution="X",
                         referred_on=date(2026, 7, 1))
    with pytest.raises(ValidationError):
        await svc.create(session, referral_type="REFER_OUT", partner_institution="  ",
                         referred_on=date(2026, 7, 1))


async def test_stats_back_refer_rate_and_fee(session):
    # 의뢰접수 4건, 회송 1건 → 회송률 25%. 의뢰회송료 2건.
    for i in range(4):
        await svc.create(session, referral_type="REFER_IN", partner_institution=f"의원{i}",
                         referred_on=date(2026, 7, 5), has_fee=(i < 2))
    await svc.create(session, referral_type="BACK_REFER", partner_institution="의원0",
                     referred_on=date(2026, 7, 6))
    s = await svc.stats(session, start=date(2026, 7, 1), end=date(2026, 7, 31))
    assert s["by_type"]["REFER_IN"]["count"] == 4 and s["by_type"]["BACK_REFER"]["count"] == 1
    assert s["by_type"]["REFER_IN"]["fee"] == 2
    assert s["back_refer_rate"] == 25.0 and s["total"] == 5 and s["fee_total"] == 2


async def test_stats_no_refer_in_rate_none(session):
    await svc.create(session, referral_type="REFER_OUT", partner_institution="A",
                     referred_on=date(2026, 7, 1))
    s = await svc.stats(session, start=date(2026, 7, 1), end=date(2026, 7, 31))
    assert s["back_refer_rate"] is None       # 의뢰접수 0 → 회송률 None(0나눗셈 방지)


async def test_stats_period_bounds(session):
    await svc.create(session, referral_type="REFER_IN", partner_institution="A",
                     referred_on=date(2026, 6, 30))     # 기간 밖
    s = await svc.stats(session, start=date(2026, 7, 1), end=date(2026, 7, 31))
    assert s["total"] == 0


async def test_close(session):
    r = await svc.create(session, referral_type="REFER_OUT", partner_institution="A",
                         referred_on=date(2026, 7, 1))
    r2 = await svc.close(session, referral_id=r.id)
    assert r2.status == "CLOSED"
    with pytest.raises(ValidationError):
        await svc.close(session, referral_id=999999)
