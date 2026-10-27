"""외화 입출금 동시성 통합 테스트 (PostgreSQL 전용) — forex_service.record_movement() 행잠금.

sqlite는 FOR UPDATE 무시로 채번(seq) 경합을 재현 못 함. 실 PG에서 동일 외화계좌에 동시 입금 N건을
일으켜 (a) 전부 성공, (b) source_ref(fx-{acct}-{seq})가 유일 — seq 충돌/전표 유실 없음을 검증.

실행: RUN_PG_INTEGRATION=1 + DATABASE_URL=postgresql+asyncpg://... (마이그 head 적용).
"""
import asyncio
import os
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from erp.finance.forex_service import ForexTranslationService
from erp.finance.models import Account, CorporateAccount, Voucher

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_PG_INTEGRATION"),
    reason="PG 통합 테스트 — RUN_PG_INTEGRATION=1 + DATABASE_URL 필요")

svc = ForexTranslationService()
_ACCS = [("1110", "현금성자산", "ASSET"), ("1115", "외화예금", "ASSET")]


@pytest.fixture
async def pg_sm():
    eng = create_async_engine(os.environ["DATABASE_URL"])
    sm = async_sessionmaker(eng, expire_on_commit=False)
    async with sm() as s:
        for code, name, kind in _ACCS:
            if not await s.scalar(select(Account).where(Account.code == code)):
                s.add(Account(code=code, name=name, kind=kind))
        await s.commit()
    yield sm
    await eng.dispose()


async def _seed_account(sm) -> int:
    async with sm() as s:
        acct = CorporateAccount(alias="ITEST USD", bank_name="ITEST", account_no="ITEST-FX-1",
                                currency="USD", gl_account_code="1115")
        s.add(acct)
        await s.commit()
        return acct.id


async def test_concurrent_movements_unique_seq(pg_sm):
    """동시 입금 10건 → 전부 성공·source_ref seq 유일(채번 충돌 없음)."""
    account_id = await _seed_account(pg_sm)

    async def one():
        async with pg_sm() as s:
            out = await svc.record_movement(s, account_id=account_id, direction="IN",
                                            fc_amount=100, rate=1350, on=date.today(), actor=1)
            await s.commit()
            return out["voucher_id"]

    results = await asyncio.gather(*[one() for _ in range(10)], return_exceptions=True)
    ok = [r for r in results if isinstance(r, int)]
    assert len(ok) == 10                    # 전부 성공

    async with pg_sm() as s:
        refs = (await s.scalars(select(Voucher.source_ref).where(
            Voucher.source_module == "finance",
            Voucher.source_ref.like(f"fx-{account_id}-%")))).all()
        assert len(refs) == 10 and len(set(refs)) == 10   # seq 유일 — 충돌·유실 없음
