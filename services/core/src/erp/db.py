"""DB 세션 — UoW 규약(design §4.4): 트랜잭션은 유스케이스(라우터/서비스 진입점)가 소유."""
from collections.abc import AsyncIterator

from sqlalchemy import BigInteger, Integer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from erp.config import get_settings


class Base(DeclarativeBase):
    pass


# PK 타입 — PG는 BIGINT IDENTITY, sqlite(단위 테스트)는 INTEGER autoincrement
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine, _session_factory
    if _engine is None:
        # 공유 PG(HIS·OpenPACS 동거) — 기본 성능 유지 수준 튜닝. DB 서비스별 분리는 백로그.
        #  pre_ping: 서버측 종료된(예: 슬롯 회수) 죽은 커넥션을 사용 전 감지·교체
        #  recycle 1800s: ERP 자신은 장기 유휴 커넥션을 남기지 않음(공유 박스 매너)
        #  timeout 10s: 풀 고갈 시 30s 매달림 대신 빠르게 실패(503) — 핸들러 점유 방지
        #  footprint(10+5)는 의도적으로 작게 유지 — 슬롯 경쟁 최소화
        _engine = create_async_engine(
            get_settings().database_url,
            pool_size=10, max_overflow=5,
            pool_pre_ping=True, pool_recycle=1800, pool_timeout=10,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


# 요청 세션 훅 — 하위 모듈이 "요청마다(대개 TTL 게이트) DB에서 갱신할 프로세스 캐시"를 등록한다.
# db는 상위 모듈을 import하지 않는다(계층) — 등록은 그 모듈이 import될 때 스스로 한다.
_session_hooks: list = []


def register_session_hook(needs, run) -> None:
    """needs() → bool(동기·DB 없이 판단), run(session) 코루틴 — needs()가 참일 때만 호출.

    요청마다 SAVEPOINT를 열지 않도록 판단과 실행을 나눈다(v1.288 흐름검토 F1 — 대부분 요청은 TTL
    안이라 DB 왕복 0). run 예외는 요청을 막지 않는다.
    """
    if (needs, run) not in _session_hooks:
        _session_hooks.append((needs, run))


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — 요청 단위 트랜잭션 경계 (성공 시 commit, 예외 시 rollback)."""
    async with session_factory()() as session:
        async with session.begin():
            for needs, run in _session_hooks:
                if not needs():
                    continue
                try:
                    async with session.begin_nested():
                        await run(session)
                except Exception:  # noqa: BLE001 — 캐시 갱신 실패는 요청을 막지 않는다
                    import logging
                    logging.getLogger("erp.db").warning("세션 훅 실패", exc_info=True)
            yield session
