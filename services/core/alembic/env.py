"""Alembic 환경 — 동기 드라이버로 마이그레이션 (asyncpg → psycopg 호환 URL 변환)."""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from erp.db import Base
import erp.common.models  # noqa: F401 — 메타데이터 등록
import erp.regulatory.models  # noqa: F401
import erp.integrations.models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://erp_app:erp_dev@localhost:5432/erp")
    return url.replace("+asyncpg", "")


def run_migrations_offline() -> None:
    context.configure(url=_sync_url(), target_metadata=target_metadata,
                      literal_binds=True, include_schemas=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sync_url())
    # 다중 컨테이너 동시 기동 방어 (design §11.0) — advisory lock은 별도 AUTOCOMMIT 세션.
    # ⚠️ 마이그레이션 커넥션에서 lock을 잡으면 암묵 트랜잭션이 alembic 커밋과 분리되어
    #    전체 롤백됨 (통합 검증에서 발견된 버그)
    lock_conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    lock_conn.exec_driver_sql("SELECT pg_advisory_lock(727274)")
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata,
                              include_schemas=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        lock_conn.exec_driver_sql("SELECT pg_advisory_unlock(727274)")
        lock_conn.close()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
