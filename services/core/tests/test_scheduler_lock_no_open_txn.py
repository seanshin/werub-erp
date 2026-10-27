"""스케줄러 리더 락 커넥션은 트랜잭션을 열어 두지 않는다(PG 레인).

2026-10-10 prod: 락 커넥션이 autobegin 트랜잭션을 프로세스 생애 내내 유지 → idle in transaction·backend_xmin 고정으로
공유 PG 전체 VACUUM 경계를 붙잡았다. 세션 advisory lock은 커밋 후에도 유지되므로 커밋해도 리더십은 그대로다.
"""
import os

import pytest

_pg_only = pytest.mark.skipif("postgresql" not in os.environ.get("TEST_DATABASE_URL", ""),
                              reason="advisory lock·pg_stat_activity는 PG 전용")


@_pg_only
async def test_leader_lock_held_without_open_transaction(monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from erp.workers import scheduler
    eng = create_async_engine(os.environ["TEST_DATABASE_URL"])
    monkeypatch.setattr("erp.db.get_engine", lambda: eng)
    conn = await scheduler._acquire_scheduler_lock()
    try:
        assert conn is not None and not conn.in_transaction()
        async with eng.connect() as probe:
            pid = await conn.scalar(text("SELECT pg_backend_pid()"))
            await conn.commit()
            row = (await probe.execute(text(
                "SELECT state, backend_xmin FROM pg_stat_activity WHERE pid = :p"), {"p": pid})).one()
            assert row.state == "idle" and row.backend_xmin is None
            held = await probe.scalar(text(
                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid = :p"), {"p": pid})
            assert held == 1                                        # 리더 락은 그대로
            other = await probe.scalar(text("SELECT pg_try_advisory_lock(:k)"),
                                       {"k": scheduler._SCHED_LOCK_KEY})
            assert other is False                                   # 다른 인스턴스는 못 잡는다
    finally:
        await conn.close()
        await eng.dispose()


async def test_lock_watch_exits_when_connection_lost(monkeypatch):
    """락 커넥션이 끊기면 프로세스를 끝내 재시작으로 락을 다시 잡는다(이중 리더 방지)."""
    from erp.workers import scheduler

    class Dead:
        async def scalar(self, *_a, **_k):
            raise ConnectionError("server closed the connection")

        async def commit(self):
            pass
    exited = []
    monkeypatch.setattr("os._exit", lambda code: (exited.append(code), (_ for _ in ()).throw(SystemExit(code))))
    with pytest.raises(SystemExit):
        await scheduler._watch_scheduler_lock(Dead(), interval=0)
    assert exited == [1]
