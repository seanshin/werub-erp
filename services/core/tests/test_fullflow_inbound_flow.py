"""2026-10-09 전체 흐름 검토 C·E·F — 인박스 소비 신뢰성."""
from datetime import date

from sqlalchemy import func, select

from erp.common.models import Department, Notification, User
from erp.integrations.inbound_consumer import InboundConsumer
from tests.test_inbound_consumer import _ev

consumer = InboundConsumer()


async def test_dead_event_notifies_active_admins(session):
    """F — DEAD 전이 시 관리자 알림(종전 없음·prod DEAD 13건 무인지)."""
    session.add_all([User(email="ad1@x.example", password_hash="x", role="ADMIN", is_active=True),
                     User(email="ad2@x.example", password_hash="x", role="ADMIN", is_active=False)])
    session.add(_ev("assets", "equipment.out_of_service", "OOS-1", {"assetNumber": "Q1"}))
    await session.flush()
    await consumer.consume(session)
    n = (await session.scalars(select(Notification).where(Notification.type == "INBOUND_DEAD"))).all()
    assert len(n) == 1 and "equipment.out_of_service" in n[0].body


async def test_failed_event_does_not_poison_dept_cache(session, monkeypatch):
    """F — 실패 이벤트가 만든 부서 id가 같은 배치 다음 이벤트에 쓰이지 않는다."""
    from erp.hr.models import Employee
    from erp.integrations import inbound_consumer as ic
    real = ic._staff

    async def flaky(session, p, event_type, ctx):
        if p.get("employeeNumber") == "BOOM":
            await ic._resolve_dept(session, "신설부서", ctx)
            raise RuntimeError("일시 오류")
        return await real(session, p, event_type, ctx)
    monkeypatch.setitem(ic.HANDLERS, ("hr", "staff.created"), flaky)
    session.add(_ev("hr", "staff.created", "D-1", {"employeeNumber": "BOOM", "department": "신설부서"}))
    session.add(_ev("hr", "staff.created", "D-2", {"employeeNumber": "OK-1", "name": "ok",
                                                    "department": "신설부서", "status": "ACTIVE"}))
    await session.flush()
    await consumer.consume(session)
    emp = await session.scalar(select(Employee).where(Employee.employee_no == "OK-1"))
    assert emp is not None
    assert await session.get(Department, emp.department_id) is not None


async def test_resign_redelivery_keeps_original_date(session):
    """C — resignDate 없는 퇴사 재수신이 퇴사일을 오늘로 덮어쓰지 않는다."""
    from erp.hr.models import Employee
    session.add(_ev("hr", "staff.created", "RS-0", {"employeeNumber": "RS-1", "name": "r",
                                                     "status": "ACTIVE"}))
    await session.flush()
    await consumer.consume(session)
    emp = await session.scalar(select(Employee).where(Employee.employee_no == "RS-1"))
    emp.source = "HIS"
    await session.flush()
    session.add(_ev("hr", "staff.resigned", "RS-A", {"employeeNumber": "RS-1",
                                                      "resignDate": "2026-09-30"}))
    await session.flush()
    await consumer.consume(session)
    session.add(_ev("hr", "staff.updated", "RS-B", {"employeeNumber": "RS-1", "status": "RESIGNED"}))
    await session.flush()
    await consumer.consume(session)
    await session.refresh(emp)
    assert emp.resigned_on == date(2026, 9, 30)


async def test_his_disposal_event_disposes_asset_with_voucher(session):
    """E — HIS 장비 폐기가 처분 전표까지(종전 상태만 → 1710·1810 영구 잔존)."""
    from erp.finance.models import FixedAsset, Voucher
    session.add(_ev("assets", "equipment.acquired", "EQ-A", {
        "assetNumber": "EQ-D1", "name": "장비", "purchasePrice": 1_200_000,
        "purchaseDate": "2025-01-01"}))
    await session.flush()
    await consumer.consume(session)
    before = await session.scalar(select(func.count()).select_from(Voucher))
    session.add(_ev("assets", "equipment.status_changed", "EQ-S", {
        "assetNumber": "EQ-D1", "status": "DISPOSED", "changedAt": "2026-09-30T01:00:00.000Z"}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    asset = await session.scalar(select(FixedAsset).where(FixedAsset.asset_no == "EQ-D1"))
    assert asset.status == "DISPOSED"
    assert await session.scalar(select(func.count()).select_from(Voucher)) > before
