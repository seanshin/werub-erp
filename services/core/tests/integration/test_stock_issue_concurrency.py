"""재고 출고 동시성 통합 테스트 (PostgreSQL 전용) — stock_service.issue() FIFO 행잠금.

sqlite는 FOR UPDATE 무시로 TOCTOU 과다출고를 재현 못 함(v1.74 Lot FIFO with_for_update).
실 PG에서 잔량 100인 품목에 동시 출고 15건(각 10)을 일으켜 (a) 정확히 10건 성공,
(b) 잔량이 음수로 내려가지 않음, (c) 초과분은 InsufficientStockError 차단을 검증.

실행: RUN_PG_INTEGRATION=1 + DATABASE_URL=postgresql+asyncpg://... (마이그 head 적용).
"""
import asyncio
import os
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from erp.scm.models import Item, Lot
from erp.scm.stock_service import InsufficientStockError, StockService

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_PG_INTEGRATION"),
    reason="PG 통합 테스트 — RUN_PG_INTEGRATION=1 + DATABASE_URL 필요")

svc = StockService()


@pytest.fixture
async def pg_sm():
    eng = create_async_engine(os.environ["DATABASE_URL"])
    sm = async_sessionmaker(eng, expire_on_commit=False)
    yield sm
    await eng.dispose()


async def _seed_item_with_stock(sm, *, code: str, qty: float) -> int:
    async with sm() as s:
        it = Item(code=code, name=code, category="SUPPLY")
        s.add(it)
        await s.flush()
        await svc.receive(s, item_id=it.id, lot_no=f"{code}-L1",
                          expiry_on=date.today() + timedelta(days=365),
                          qty=qty, moved_on=date.today(), actor=1, unit_cost=100)
        await s.commit()
        return it.id


async def test_concurrent_issue_no_oversell(pg_sm):
    """동시 출고 15건(각 10, 잔량 100) → 정확히 10건 성공·과다출고 없음·잔량 0."""
    item_id = await _seed_item_with_stock(pg_sm, code="ITEST-STK-CONC", qty=100)

    async def one():
        async with pg_sm() as s:
            await svc.issue(s, item_id=item_id, qty=10, moved_on=date.today(), actor=1)
            await s.commit()

    results = await asyncio.gather(*[one() for _ in range(15)], return_exceptions=True)
    ok = sum(1 for r in results if r is None)
    rejected = [r for r in results if isinstance(r, InsufficientStockError)]
    assert ok == 10                        # 잔량만큼만 성공
    assert len(rejected) == 5              # 초과분 차단

    async with pg_sm() as s:
        remaining = await s.scalar(select(Lot.remaining_qty).where(
            Lot.item_id == item_id, Lot.lot_no == "ITEST-STK-CONC-L1"))
        assert float(remaining) == 0       # 음수 없음·정확 소진
