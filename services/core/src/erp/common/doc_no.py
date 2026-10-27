"""문서번호 채번 직렬화 — count(*)+1 채번의 동시성 경합(unique 위반→500) 방지 (v1.204).

count 기반 채번(구매요청/자재청구/결재선요청/기안)은 사람이 읽는 연번을 유지해야 하므로
시퀀스로 바꾸지 않고, prefix별 PG advisory xact lock으로 "조회→+1→insert"를 직렬화한다.
sqlite(테스트)는 단일 커넥션이라 no-op. 관례: approval_ledger·patient.close_day와 동형.
"""
import zlib

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

_NS = 0x444F434E << 32          # 'DOCN' 네임스페이스 — 타 advisory lock 키와 충돌 방지


async def lock_doc_no(session: AsyncSession, prefix: str) -> None:
    """같은 prefix(예: '구매요청-2026') 채번을 트랜잭션 동안 직렬화."""
    if session.bind.dialect.name == "postgresql":
        key = _NS | zlib.crc32(prefix.encode("utf-8"))
        await session.execute(select(func.pg_advisory_xact_lock(key)))
