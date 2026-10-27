"""ERP↔HIS 코드 마스터 교환 — fee 조회·미등재 리포트 (인증·필터)."""
from datetime import date

import pytest

from erp.config import get_settings
from erp.errors import ForbiddenError
from erp.integrations.regulatory_api import get_fees, unmapped_codes
from erp.patient.models import BillingLine
from erp.regulatory.models import FeeMaster


async def _with_key(fn):
    s = get_settings()
    old = s.his_webhook_key
    s.his_webhook_key = "rk_test"
    try:
        return await fn()
    finally:
        s.his_webhook_key = old


async def test_get_fees_auth_and_filter(session):
    session.add(FeeMaster(code="AA257", code_type="TARIFF", name="재진진찰료",
                          unit_price=14710, benefit_category="CONSULT",
                          valid_from=date(2026, 1, 1)))
    await session.flush()

    with pytest.raises(ForbiddenError):       # 키 없음 차단
        await get_fees(codes="AA257", x_integration_key=None, session=session)

    async def run():
        out = await get_fees(codes="AA257", as_of=date(2026, 6, 1),
                             x_integration_key="rk_test", session=session)
        assert out["count"] == 1
        f = out["items"][0]
        assert f["unit_price"] == 14710 and f["benefit_category"] == "CONSULT"
        assert str(f["valid_from"]) == "2026-01-01"
    await _with_key(run)


async def test_unmapped_codes_report(session):
    # 마스터 미등재 약가 라인 1건 + 등재 라인 1건
    session.add(FeeMaster(code="AA257", code_type="TARIFF", name="x",
                          unit_price=100, valid_from=date(2026, 1, 1)))
    session.add(BillingLine(line_id="L1", chart_no="C1", code="M999999",
                            code_type="DRUG", count=1, coverage="NHIS",
                            treated_on=date(2026, 5, 1), unit_price_his=890))
    session.add(BillingLine(line_id="L2", chart_no="C1", code="AA257",
                            code_type="TARIFF", count=1, coverage="NHIS",
                            treated_on=date(2026, 5, 1)))
    await session.flush()

    async def run():
        out = await unmapped_codes(x_integration_key="rk_test", session=session)
        codes = {i["code"] for i in out["items"]}
        assert "M999999" in codes and "AA257" not in codes   # 미등재만
        m = next(i for i in out["items"] if i["code"] == "M999999")
        assert m["his_unit_price"] == 890 and m["code_type"] == "DRUG"
    await _with_key(run)
