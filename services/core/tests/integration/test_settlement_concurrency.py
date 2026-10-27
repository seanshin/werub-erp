"""정산 동시성 통합 테스트 (PostgreSQL 전용) — PR #280 행잠금(with_for_update) 실검증.

sqlite 단위테스트는 FOR UPDATE를 무시해 경합을 재현 못 함. 실 PG에서 asyncio.gather로
동시 수금을 일으켜 (a) 잔액 lost-update 없음, (b) 채번(seq) 충돌 없음, (c) 잔액 초과
과수금 차단을 검증한다.

실행: RUN_PG_INTEGRATION=1 + DATABASE_URL=postgresql+asyncpg://... (마이그 head 적용된 DB).
미설정 시 전체 skip(로컬 sqlite `pytest -q`는 건너뜀).
"""
import asyncio
import os
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from erp.errors import ValidationError
from erp.finance.models import Account, Receivable, Voucher
from erp.finance.settlement_service import SettlementService

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_PG_INTEGRATION"),
    reason="PG 통합 테스트 — RUN_PG_INTEGRATION=1 + DATABASE_URL 필요")

svc = SettlementService()
_ACCS = [("1110", "현금성자산", "ASSET"), ("1130", "미수금", "ASSET"),
         ("2110", "미지급금", "LIABILITY"), ("4110", "외래수익", "REVENUE")]


@pytest.fixture
async def pg_sm():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:                       # 전표용 계정 보장(없으면 삽입)
        for code, name, kind in _ACCS:
            if not await s.scalar(select(Account).where(Account.code == code)):
                s.add(Account(code=code, name=name, kind=kind))
        await s.commit()
    yield sm
    await engine.dispose()


async def _seed_receivable(sm, *, amount: float) -> int:
    async with sm() as s:
        r = await svc.record_receivable(s, party="ITEST-CONC", amount=amount,
                                        billed_on=date.today(), due_on=date.today())
        await s.commit()
        return r.id


async def test_concurrent_collect_no_lost_update(pg_sm):
    """동시 수금 10건(각 100) → 합산 정확(lost-update 없음)·seq 유일."""
    rid = await _seed_receivable(pg_sm, amount=1000)

    async def one():
        async with pg_sm() as s:
            await svc.collect(s, receivable_id=rid, amount=100, on=date.today())
            await s.commit()
    await asyncio.gather(*[one() for _ in range(10)])

    async with pg_sm() as s:
        r = await s.get(Receivable, rid)
        assert float(r.collected_amount) == 1000        # 증분 소실 없음
        assert r.status == "COLLECTED"
        refs = (await s.scalars(select(Voucher.source_ref).where(
            Voucher.source_module == "ar",
            Voucher.source_ref.like(f"ar-collect-{rid}-%")))).all()
        assert len(refs) == 10 and len(set(refs)) == 10  # 채번 충돌 없음


async def test_concurrent_collect_no_over_collection(pg_sm):
    """동시 수금 15건(각 100, 잔액 1000) → 정확히 10건 성공·과수금 차단."""
    rid = await _seed_receivable(pg_sm, amount=1000)

    async def one():
        async with pg_sm() as s:
            await svc.collect(s, receivable_id=rid, amount=100, on=date.today())
            await s.commit()
    results = await asyncio.gather(*[one() for _ in range(15)], return_exceptions=True)

    ok = sum(1 for r in results if r is None)
    rejected = [r for r in results if isinstance(r, ValidationError)]
    assert ok == 10                                  # 잔액만큼만 성공
    assert len(rejected) == 5                         # 초과분은 검증 차단
    async with pg_sm() as s:
        r = await s.get(Receivable, rid)
        assert float(r.collected_amount) == 1000      # 과수금 없음
