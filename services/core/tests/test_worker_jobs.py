"""워커 잡 테스트 (P1-3) — 법정 접속기록 flush의 delete-after-commit 불변식 + 하트비트.

핵심 회귀 방지: job_access_log_flush는 Stream(xdel) 삭제를 **DB 커밋 성공 후에만** 수행해야 한다
(v1.65 HIGH 수정). 커밋 전 xdel하면 커밋 실패 시 법정 2년 접속기록이 양쪽에서 영구 유실.
그 불변식이 주석으로만 있어(audit/service.py:59) 테스트로 고정한다.
"""
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp.audit.models import AccessLog
from erp.audit.service import ACCESS_STREAM
from erp.db import Base


class FakeRedis:
    """xrange/xdel만 구현한 최소 스텁 — deleted로 삭제 순서·발생 여부 추적."""

    def __init__(self, entries):
        self._entries = list(entries)   # [(msg_id_bytes, {b"data": json_bytes}), ...]
        self.deleted: list = []

    async def xrange(self, key, count=None):
        assert key == ACCESS_STREAM
        return list(self._entries)

    async def xdel(self, key, msg_id):
        self.deleted.append(msg_id)

    async def aclose(self):
        pass


def _entry(msg_id: str, *, actor=1, action="VIEW", entity="patient.patients",
           target_id="7", route="/patients"):
    data = {"actor": actor, "action": action, "entity": entity,
            "target_id": target_id, "route": route}
    return (msg_id.encode(), {b"data": json.dumps(data).encode()})


@pytest.fixture
async def worker_env(monkeypatch):
    """워커 잡용 격리 DB(engine+factory) + scheduler.session_factory 패치. FakeRedis는 잡별 주입."""
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        execution_options={"schema_translate_map": {"itam": "scm"}})
    async with engine.begin() as conn:
        for schema in ("common", "hr", "finance", "scm", "patient",
                       "claims", "regulatory", "integration", "audit", "reporting"):
            await conn.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {schema}")
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import erp.workers.scheduler as sched
    monkeypatch.setattr(sched, "session_factory", lambda: factory)
    yield factory
    await engine.dispose()


async def _run_flush_with(monkeypatch, fake: FakeRedis):
    import redis.asyncio as aioredis

    from erp.workers import scheduler
    monkeypatch.setattr(aioredis, "from_url", lambda *a, **k: fake)
    await scheduler.job_access_log_flush()


async def test_access_log_flush_writes_rows_and_deletes_after(worker_env, monkeypatch):
    """정상 — 스트림 3건 소비 → access_log 3행 커밋 + 그 후 xdel 3건."""
    fake = FakeRedis([_entry("1-0"), _entry("2-0", actor=2), _entry("3-0", actor=3)])
    await _run_flush_with(monkeypatch, fake)
    async with worker_env() as s:
        n = await s.scalar(select(func.count()).select_from(AccessLog))
    assert n == 3                                   # DB 커밋됨
    assert fake.deleted == [b"1-0", b"2-0", b"3-0"]  # 커밋 후 전건 xdel


async def test_access_log_flush_no_xdel_on_flush_failure(worker_env, monkeypatch):
    """불변식 — flush 중 실패(악성 데이터)면 커밋 안 됨 + **xdel 미호출**(메시지 보존)."""
    bad = (b"9-0", {b"data": json.dumps({"action": "VIEW"}).encode()})  # actor 누락 → KeyError
    fake = FakeRedis([bad])
    await _run_flush_with(monkeypatch, fake)         # 잡은 예외를 잡아 경고만(비차단)
    async with worker_env() as s:
        n = await s.scalar(select(func.count()).select_from(AccessLog))
    assert n == 0                                    # 롤백 — 행 없음
    assert fake.deleted == []                        # ★ xdel 미호출 → 법정기록 보존


async def test_approval_ledger_verify_ok_no_alert(worker_env):
    """결재 원장 검증 잡 — 무결 체인이면 알림 없음."""
    from datetime import datetime, timezone

    from erp.common.approval_ledger import ApprovalLedgerService
    from erp.common.models import Notification, User
    from erp.workers import scheduler
    async with worker_env() as s:
        s.add(User(id=1, email="adm@x.example", password_hash="x", role="ADMIN"))
        await ApprovalLedgerService().append(
            s, instance_id=1, doc_type="PO", doc_id=1, step_no=None, action="BUILD",
            actor=1, acted_at=datetime.now(timezone.utc))
        await s.commit()
    await scheduler.job_approval_ledger_verify()
    async with worker_env() as s:
        n = await s.scalar(select(func.count()).select_from(Notification))
    assert n == 0


async def test_approval_ledger_verify_tamper_alerts_admin_and_director(worker_env):
    """변조 감지 시 ADMIN+DIRECTOR 알림 + 재실행 시 디둡(미확인 동일 경보 재발화 없음)."""
    from datetime import datetime, timezone

    from sqlalchemy import update

    from erp.common.approval_ledger import ApprovalLedgerService
    from erp.common.models import ApprovalLedger, Notification, User
    from erp.workers import scheduler
    async with worker_env() as s:
        s.add_all([User(id=1, email="adm@x.example", password_hash="x", role="ADMIN"),
                   User(id=2, email="dir@x.example", password_hash="x", role="DIRECTOR"),
                   User(id=3, email="staff@x.example", password_hash="x", role="STAFF")])
        await ApprovalLedgerService().append(
            s, instance_id=1, doc_type="PO", doc_id=1, step_no=None, action="BUILD",
            actor=1, acted_at=datetime.now(timezone.utc))
        await s.execute(update(ApprovalLedger).values(actor=999))   # 변조 시뮬레이션
        await s.commit()
    await scheduler.job_approval_ledger_verify()
    async with worker_env() as s:
        rows = (await s.execute(select(Notification))).scalars().all()
    assert {r.user_id for r in rows} == {1, 2}                      # ADMIN+DIRECTOR (STAFF 제외)
    assert all(r.type == "APPROVAL_LEDGER_TAMPER" and "seq 1" in r.body for r in rows)
    # 재실행 — 미확인 동일 경보가 이미 있으므로 추가 생성 없음(디둡)
    await scheduler.job_approval_ledger_verify()
    async with worker_env() as s:
        n2 = await s.scalar(select(func.count()).select_from(Notification))
    assert n2 == 2                                                  # 중복 미생성


async def test_license_expiry_catchup_band_and_dedup(worker_env):
    """면허 만료 경보 — 정확 D-day가 아닌 밴드 내(잔여 22일=D-30 밀스톤)도 발화(catch-up),
    잡 재실행 시 동일 밀스톤 중복 미생성(dedup)."""
    from datetime import timedelta

    from erp.common.kst import kst_today
    from erp.common.models import Notification, User
    from erp.hr.models import Employee, License
    from erp.workers import scheduler
    today = kst_today()
    async with worker_env() as s:
        e = Employee(id=1, employee_no="D001", name="김의사", hired_on=today - timedelta(days=1000))
        s.add(e)
        await s.flush()
        s.add(User(id=1, email="doc@x.example", password_hash="x", role="STAFF", employee_id=1))
        # 잔여 22일 — 구 로직(정확 D-30/D-7)에선 미발화, 밴드 로직에선 D-30 catch-up
        s.add(License(employee_id=1, license_type="의사", license_no="MD-1",
                      issued_on=today - timedelta(days=1000), expires_on=today + timedelta(days=22)))
        await s.commit()
    await scheduler.job_license_expiry()
    async with worker_env() as s:
        rows = (await s.execute(select(Notification).where(
            Notification.type == "LICENSE_EXPIRY"))).scalars().all()
    assert len(rows) == 1 and "D-30" in rows[0].body       # catch-up 발화
    # 재실행 — 동일 밀스톤 이미 통지 → 중복 없음
    await scheduler.job_license_expiry()
    async with worker_env() as s:
        n = await s.scalar(select(func.count()).select_from(Notification).where(
            Notification.type == "LICENSE_EXPIRY"))
    assert n == 1                                          # dedup


async def test_balance_checkpoint_job_creates_and_verifies(worker_env):
    """H-2 잡 — 전월말 체크포인트 적재 + 자기검증 통과 시 경보 없음."""
    from datetime import date

    from erp.common.models import Notification
    from erp.finance.models import Account, AccountBalanceCheckpoint
    from erp.finance.service import VoucherLineDTO, VoucherService
    from erp.workers import scheduler
    async with worker_env() as s:
        s.add_all([Account(code="1110", name="현금", kind="ASSET"),
                   Account(code="4110", name="수익", kind="REVENUE")])
        await s.flush()
        await VoucherService().create(s, source_module="manual", source_ref="bc1",
                                      voucher_on=date(2026, 1, 10),
                                      lines=[VoucherLineDTO("1110", "DEBIT", 500000),
                                             VoucherLineDTO("4110", "CREDIT", 500000)])
        await s.commit()
    await scheduler.job_balance_checkpoint()
    async with worker_env() as s:
        cps = await s.scalar(select(func.count()).select_from(AccountBalanceCheckpoint))
        alerts = await s.scalar(select(func.count()).select_from(Notification).where(
            Notification.type == "BALANCE_CHECKPOINT_MISMATCH"))
    assert cps >= 1 and alerts == 0     # 체크포인트 생성·검증 통과(경보 없음)
