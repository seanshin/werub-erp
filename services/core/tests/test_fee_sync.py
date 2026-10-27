"""수가 동기화 (FR-34) — 멱등·close-then-open·이상 변동 보류 (design §3.2)."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.regulatory.models import FeeMaster
from erp.regulatory.service import FeeLookupService
from erp.regulatory.sync import FeeSyncService, FeeUpdate

sync = FeeSyncService()
lookup = FeeLookupService()


def _u(code="AA100", price=15000.0, vfrom=date(2026, 1, 1), name="진찰료"):
    return FeeUpdate(code=code, code_type="TARIFF", name=name,
                     unit_price=price, valid_from=vfrom)


async def test_initial_load(session):
    n = await sync.apply(session, source_version="v2026-01", updates=[_u()])
    assert n == 1


async def test_idempotent_same_source_version_noop(session):
    """② 멱등키 — 동일 source_version 재실행 no-op."""
    await sync.apply(session, source_version="v2026-01", updates=[_u()])
    n = await sync.apply(session, source_version="v2026-01", updates=[_u()])
    assert n == 0
    rows = (await session.execute(select(FeeMaster))).scalars().all()
    assert len(rows) == 1


async def test_close_then_open_single_version_chain(session):
    """① 구 버전 닫기 + 신 버전 — temporal 체인 정합."""
    await sync.apply(session, source_version="v1", updates=[_u(price=15000)])
    await sync.apply(session, source_version="v2",
                     updates=[_u(price=15800, vfrom=date(2026, 7, 1))])

    old = await lookup.get_price(session, code="AA100", code_type="TARIFF",
                                 at_date=date(2026, 6, 30))
    new = await lookup.get_price(session, code="AA100", code_type="TARIFF",
                                 at_date=date(2026, 7, 1))
    assert float(old.unit_price) == 15000 and old.valid_to == date(2026, 7, 1)
    assert float(new.unit_price) == 15800 and new.valid_to is None


async def test_retroactive_version_skipped(session):
    """과거 소급 신 버전은 자동 반영 금지 — 수동 검토."""
    await sync.apply(session, source_version="v1",
                     updates=[_u(price=15000, vfrom=date(2026, 7, 1))])
    n = await sync.apply(session, source_version="v2",
                         updates=[_u(price=14000, vfrom=date(2026, 1, 1))])
    assert n == 0  # 스킵


async def test_anomaly_mass_change_held(session):
    """③ 활성 코드(모수 ≥20) 50% 초과 변경 → 자동 반영 보류, force 시 통과."""
    await sync.apply(session, source_version="base", updates=[
        _u(code=f"AA{i:03d}") for i in range(25)])
    bulk = [_u(code=f"AA{i:03d}", price=99999, vfrom=date(2026, 9, 1)) for i in range(20)]
    with pytest.raises(ValidationError):
        await sync.apply(session, source_version="bulk", updates=bulk)
    n = await sync.apply(session, source_version="bulk", updates=bulk, force=True)
    assert n == 20


async def test_small_dataset_skips_anomaly_check(session):
    """모수 20 미만(초기 적재)은 이상 변동 판정 제외."""
    await sync.apply(session, source_version="v1", updates=[_u()])
    n = await sync.apply(session, source_version="v2",
                         updates=[_u(price=16000, vfrom=date(2026, 7, 1))])
    assert n == 1


async def test_no_change_skipped(session):
    await sync.apply(session, source_version="v1", updates=[_u()])
    n = await sync.apply(session, source_version="v2",
                         updates=[_u(vfrom=date(2026, 7, 1))])  # 가격·명칭 동일
    assert n == 0
