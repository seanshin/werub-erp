"""공유 PG 연결 슬롯 진단 (관리자, 읽기전용) — ops-runbook §9.1 신호의 앱 노출."""
import pytest

from erp.app.admin_router import db_connection_diagnostics
from erp.common.router import CurrentUser
from erp.errors import ForbiddenError

ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
STAFF = CurrentUser(user_id=2, role="STAFF", dept_id=1, license_flag=True)


async def test_diagnostics_admin_returns_pool(session):
    out = await db_connection_diagnostics(user=ADMIN, session=session)
    # ERP 자체 풀 통계는 항상 노출(필드 존재)
    assert set(out["erp_pool"]) >= {"size", "checked_out", "overflow"}
    # sqlite(단위테스트)는 pg_stat_activity 부재 → shared_pg 정직하게 null.
    # PG 레인(P2-7)에선 실제 pg_stat_activity 통계 노출 → dict.
    if session.bind.dialect.name == "sqlite":
        assert out["shared_pg"] is None
    else:
        assert isinstance(out["shared_pg"], dict) and "total" in out["shared_pg"]


async def test_diagnostics_non_admin_forbidden(session):
    with pytest.raises(ForbiddenError):
        await db_connection_diagnostics(user=STAFF, session=session)


async def test_shared_pg_stats_none_on_sqlite(session):
    """P2-4 — 이웃감시 잡이 쓰는 순수 통계. 비-PG(sqlite)는 None → 잡이 조용히 스킵.

    PG 레인에선 실 통계 dict가 나오는 게 정상(위 test_diagnostics_admin_returns_pool이 검증) —
    본 테스트는 sqlite 분기 전용이라 레인별 분기."""
    from erp.app.db_diagnostics_service import shared_pg_stats
    out = await shared_pg_stats(session)
    if session.bind.dialect.name == "sqlite":
        assert out is None
    else:
        assert isinstance(out, dict)
