"""원천징수 요율 — 시점 버전관리 CRUD + 시점 적용 조회(세법 개정 대응)."""
from datetime import date

import pytest

from erp.common.router import CurrentUser
from erp.errors import ForbiddenError
from erp.finance.models import WithholdingRate
from erp.finance.router import (
    WithholdingBody, create_withholding_rate, list_withholding_rates, update_withholding_rate,
)
from erp.integrations.inbound_consumer import _resolve_withholding_rate

ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
STAFF = CurrentUser(user_id=2, role="STAFF", dept_id=1, license_flag=True)


async def test_create_list_withholding(session):
    out = await create_withholding_rate(WithholdingBody(
        dealer_type="DOMESTIC", rate_pct=3.3, effective_from=date(2026, 1, 1),
        tax_invoice_by="병원역발행", note="잠정"), user=ADMIN, session=session)
    assert out["rate_pct"] == 3.3 and out["dealer_type"] == "DOMESTIC"
    listed = await list_withholding_rates(user=ADMIN, session=session)
    assert any(r["id"] == out["id"] for r in listed["items"])


async def test_non_admin_cannot_write(session):
    with pytest.raises(ForbiddenError):
        await create_withholding_rate(WithholdingBody(rate_pct=3.3, effective_from=date(2026, 1, 1)),
                                      user=STAFF, session=session)


async def test_time_versioning_resolves_by_date(session):
    """세법 개정 — 시행일 구간으로 시점별 다른 요율 적용."""
    session.add_all([
        WithholdingRate(dealer_type="DOMESTIC", rate_pct=3.3,
                        effective_from=date(2025, 1, 1), effective_to=date(2025, 12, 31)),
        WithholdingRate(dealer_type="DOMESTIC", rate_pct=4.0, effective_from=date(2026, 1, 1)),
    ])
    await session.flush()
    r2025 = await _resolve_withholding_rate(session, date(2025, 6, 1), "DOMESTIC")
    r2026 = await _resolve_withholding_rate(session, date(2026, 6, 1), "DOMESTIC")
    assert float(r2025.rate_pct) == 3.3 and float(r2026.rate_pct) == 4.0
    # 구간 밖(개정 전) → 없음
    assert await _resolve_withholding_rate(session, date(2024, 6, 1), "DOMESTIC") is None


def test_rate_pct_range_validated():
    """검토보완 — 음수·100 초과 요율 입력 거부(pydantic Field)."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        WithholdingBody(rate_pct=150, effective_from=date(2026, 1, 1))
    with pytest.raises(ValidationError):
        WithholdingBody(rate_pct=-1, effective_from=date(2026, 1, 1))


async def test_update_closes_period(session):
    created = await create_withholding_rate(WithholdingBody(
        dealer_type="DOMESTIC", rate_pct=3.3, effective_from=date(2026, 1, 1)),
        user=ADMIN, session=session)
    upd = await update_withholding_rate(created["id"], WithholdingBody(
        dealer_type="DOMESTIC", rate_pct=3.3, effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31), active=False), user=ADMIN, session=session)
    assert upd["effective_to"] == "2026-12-31" and upd["active"] is False
