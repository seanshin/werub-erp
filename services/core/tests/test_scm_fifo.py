"""FIFO 재고 엔진 (FR-12/46/13) — 순서·임박 차단·override·음수 방어·안전재고 알림."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from erp.common.models import Notification, User
from erp.common.security import hash_password
from erp.scm.models import Item, Lot
from erp.scm.stock_service import InsufficientStockError, StockService

svc = StockService()
D = date(2026, 6, 7)
FAR = D + timedelta(days=365)


async def _item(session, safety=0.0, narcotic=False):
    item = Item(code="MED-001", name="진통제", category="DRUG",
                safety_stock_qty=safety, is_narcotic=narcotic)
    session.add(item)
    await session.flush()
    return item


async def test_fifo_picks_earliest_expiry_first(session):
    item = await _item(session)
    await svc.receive(session, item_id=item.id, lot_no="L-NEW", expiry_on=FAR,
                      qty=100, moved_on=D, actor=1)
    await svc.receive(session, item_id=item.id, lot_no="L-OLD",
                      expiry_on=D + timedelta(days=120), qty=50, moved_on=D, actor=1)
    movements = await svc.issue(session, item_id=item.id, qty=60, moved_on=D, actor=1)
    # 유효기간 빠른 L-OLD(50) 먼저 소진 → L-NEW(10)
    lots = {lot.id: lot for lot in (await session.execute(select(Lot))).scalars().all()}
    assert len(movements) == 2
    assert float(lots[movements[0].lot_id].remaining_qty) == 0    # L-OLD 소진
    assert float(lots[movements[1].lot_id].remaining_qty) == 90   # L-NEW 10 차감


async def test_expiring_lot_blocked_from_issue(session):
    """3개월 미만 lot 출고 차단 (FR-46) — 가용량에서 제외."""
    item = await _item(session)
    await svc.receive(session, item_id=item.id, lot_no="L-EXP",
                      expiry_on=D + timedelta(days=30), qty=100, moved_on=D, actor=1)
    with pytest.raises(InsufficientStockError):
        await svc.issue(session, item_id=item.id, qty=10, moved_on=D, actor=1)


async def test_override_allows_expiring_with_reason(session):
    item = await _item(session)
    await svc.receive(session, item_id=item.id, lot_no="L-EXP",
                      expiry_on=D + timedelta(days=30), qty=100, moved_on=D, actor=1)
    movements = await svc.issue(session, item_id=item.id, qty=10, moved_on=D, actor=1,
                                override_expiry=True, override_reason="긴급 사용 — 약제팀 승인")
    assert movements[0].override_reason  # 사유 기록 (감사 추적)


async def test_insufficient_stock_rejected(session):
    item = await _item(session)
    await svc.receive(session, item_id=item.id, lot_no="L-1", expiry_on=FAR,
                      qty=5, moved_on=D, actor=1)
    with pytest.raises(InsufficientStockError):
        await svc.issue(session, item_id=item.id, qty=10, moved_on=D, actor=1)


async def test_safety_stock_alert_on_breach(session):
    """안전재고 미달 → 관리자 알림 (FR-13 — v1.0 복원 요구사항)."""
    admin = User(email="adm@h.example", password_hash=hash_password("Secure!Pass12"), role="ADMIN")
    session.add(admin)
    item = await _item(session, safety=50)
    await svc.receive(session, item_id=item.id, lot_no="L-1", expiry_on=FAR,
                      qty=60, moved_on=D, actor=1)
    await svc.issue(session, item_id=item.id, qty=20, moved_on=D, actor=1)  # 잔량 40 < 50
    notif = (await session.execute(select(Notification))).scalars().all()
    assert len(notif) == 1 and notif[0].type == "STOCK"


async def test_expiring_lots_report(session):
    item = await _item(session)
    await svc.receive(session, item_id=item.id, lot_no="L-60d",
                      expiry_on=D + timedelta(days=60), qty=10, moved_on=D, actor=1)
    await svc.receive(session, item_id=item.id, lot_no="L-1y", expiry_on=FAR,
                      qty=10, moved_on=D, actor=1)
    rows = await svc.expiring_lots(session, within_days=90, today=D)
    assert [r.lot_no for r in rows] == ["L-60d"]
