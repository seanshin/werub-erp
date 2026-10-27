"""테스트 픽스처 — 기본 sqlite(aiosqlite) 인메모리, `TEST_DATABASE_URL` 설정 시 실제 Postgres(P2-7).

sqlite 경로(기본): EXCLUDE(GiST)·스키마는 PG 전용이라 스키마 접두 없이 ATTACH로 에뮬레이트(단위 테스트).
PG 경로(nightly): `TEST_DATABASE_URL`(postgresql+asyncpg://...) 지정 시 전 테스트를 실제 PG에서 실행 —
스키마 자격·EXCLUDE 겹침제약·JSONB·트랜잭션 의미 등 sqlite가 가리던 방언 회귀를 야간 레인에서 적발.
격리는 테스트별 외부 트랜잭션 롤백(스키마·테이블은 최초 1회 생성, 이후 재사용).
"""
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import erp.analytics.mart_models  # noqa: F401
import erp.audit.models  # noqa: F401
import erp.claims.models  # noqa: F401
import erp.common.models  # noqa: F401
import erp.finance.models  # noqa: F401
import erp.hr.models  # noqa: F401
import erp.integrations.models  # noqa: F401
import erp.patient.models  # noqa: F401
import erp.itam.models  # noqa: F401
import erp.regulatory.models  # noqa: F401
import erp.scm.models  # noqa: F401
from erp.db import Base


@pytest.fixture(autouse=True)
def _legacy_sso_defaults(monkeypatch):
    """공개판 — SSO 미결정 기본값을 안전한 쪽으로 바꿨다. 기존 시험은 종전 기본값을 전제로 하므로
    여기서만 되돌린다(공개판 기본값은 tests/test_public_edition_sso_defaults.py 가 따로 본다)."""
    import erp.common.org_service as org
    monkeypatch.setattr(org, "SAFE_DEFAULTS", False)

_PG_URL = os.environ.get("TEST_DATABASE_URL")
_PG_SCHEMAS = ("common", "hr", "finance", "scm", "patient", "claims",
               "regulatory", "integration", "audit", "reporting", "itam")
_pg_ready = False  # 스키마·테이블 최초 1회 생성 가드(테스트 세션 내 재사용)


async def _pg_bootstrap(engine):
    """실제 PG에 스키마·확장·테이블을 1회 생성(빈 상태 — sqlite create_all과 동형)."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS btree_gist")
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        for schema in _PG_SCHEMAS:
            await conn.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def _pg_session():
    """PG 테스트 세션 — 외부 트랜잭션에 조인, 종료 시 롤백해 테스트간 격리."""
    global _pg_ready
    # asyncpg 엔진은 이벤트 루프에 묶여 있어 테스트(=루프)마다 새로 생성(루프 재사용 이슈 회피)
    engine = create_async_engine(_PG_URL)
    if not _pg_ready:
        await _pg_bootstrap(engine)
        _pg_ready = True
    conn = await engine.connect()
    trans = await conn.begin()
    # commit()은 세이브포인트로 흡수 → 바깥 트랜잭션 롤백이 전부 되돌림(외부 트랜잭션 조인 패턴)
    factory = async_sessionmaker(bind=conn, expire_on_commit=False,
                                 join_transaction_mode="create_savepoint")
    try:
        async with factory() as s:
            yield s
    finally:
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _sqlite_session():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        # sqlite ATTACH는 최대 10개 — 11번째 스키마(itam)는 기존 scm db에 접어 매핑(테스트 전용)
        execution_options={"schema_translate_map": {"itam": "scm"}},
    )
    # sqlite는 스키마 미지원 — 테이블 스키마 접두를 ATTACH로 에뮬레이트
    async with engine.begin() as conn:
        for schema in ("common", "hr", "finance", "scm", "patient",
                       "claims", "regulatory", "integration", "audit", "reporting"):
            await conn.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {schema}")
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
async def session():
    if _PG_URL:
        async for s in _pg_session():
            yield s
    else:
        async for s in _sqlite_session():
            yield s


@pytest.fixture
async def client(session):
    """HTTP 계층 테스트 클라이언트 — 실제 앱(create_app)을 ASGI로 구동해 DI·인증·Pydantic 검증·
    exception handler를 전부 통과시킨다(서비스 직호출 테스트가 못 잡는 라우터 시그니처·RBAC·422 회귀).

    get_session을 시드용 session으로 오버라이드(sqlite StaticPool 단일 커넥션/PG 단일 트랜잭션 공유
    → 시드↔요청 동일 DB 뷰). 인증은 tests/_http.py의 bearer()로 Bearer 토큰 주입."""
    from httpx import ASGITransport, AsyncClient

    from erp.db import get_session
    from erp.main import create_app
    app = create_app()

    async def _override_session():
        yield session

    app.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
