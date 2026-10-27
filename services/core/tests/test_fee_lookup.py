"""FR-35 temporal 경계 케이스 — 설계 확정 3케이스를 독립 테스트 (design §3.2)."""
from datetime import date

import pytest

from erp.errors import FeeNotFoundError
from erp.regulatory.models import FeeMaster
from erp.regulatory.service import FeeLookupService

svc = FeeLookupService()


async def _seed(session):
    # v1: 1/1 ~ 7/1(exclusive), v2: 7/1 ~ 현행
    session.add_all([
        FeeMaster(code="AA100", code_type="TARIFF", name="진찰료", unit_price=15000,
                  valid_from=date(2026, 1, 1), valid_to=date(2026, 7, 1)),
        FeeMaster(code="AA100", code_type="TARIFF", name="진찰료", unit_price=15800,
                  valid_from=date(2026, 7, 1), valid_to=None),
    ])
    await session.flush()


async def test_boundary_case1_effective_date_is_inclusive_new_version(session):
    """① 시행일 당일(7/1)은 '신 버전' 적용 — valid_from inclusive."""
    await _seed(session)
    fee = await svc.get_price(session, code="AA100", code_type="TARIFF",
                              at_date=date(2026, 7, 1))
    assert float(fee.unit_price) == 15800


async def test_boundary_case1b_day_before_is_old_version(session):
    """① 보강: 시행일 전날(6/30)은 구 버전 — valid_to exclusive."""
    await _seed(session)
    fee = await svc.get_price(session, code="AA100", code_type="TARIFF",
                              at_date=date(2026, 6, 30))
    assert float(fee.unit_price) == 15000


async def test_boundary_case2_null_valid_to_is_current(session):
    """② valid_to NULL = 현행 — 먼 미래 조회도 매치."""
    await _seed(session)
    fee = await svc.get_price(session, code="AA100", code_type="TARIFF",
                              at_date=date(2030, 12, 31))
    assert float(fee.unit_price) == 15800


async def test_boundary_case3_before_any_version_raises(session):
    """③ 어떤 버전도 없는 과거 시점 — FeeNotFoundError (잘못된 단가 적용 방지)."""
    await _seed(session)
    with pytest.raises(FeeNotFoundError):
        await svc.get_price(session, code="AA100", code_type="TARIFF",
                            at_date=date(2025, 12, 31))


async def test_past_reproduction_after_new_version_added(session):
    """과거 시점 정산 재현 — 신 버전 적재 후에도 과거일은 당시 단가 (NFR Temporal)."""
    await _seed(session)
    fee = await svc.get_price(session, code="AA100", code_type="TARIFF",
                              at_date=date(2026, 3, 15))
    assert float(fee.unit_price) == 15000
