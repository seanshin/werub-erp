"""마약류 수불부 (FR-20) — append-only·해시 체인·정정 규칙·무결성 검증."""
from datetime import date

import pytest
from sqlalchemy import update

from erp.errors import ValidationError
from erp.scm.models import Item, NarcoticsLedger
from erp.scm.narcotics_service import NarcoticsService

svc = NarcoticsService()


async def _narcotic_item(session):
    item = Item(code="NARC-001", name="모르핀주", category="DRUG", is_narcotic=True)
    session.add(item)
    await session.flush()
    return item


async def test_backdated_entry_rejected(session):
    """심층 재검토 — 직전 기록보다 이른 거래일 append 차단(수불일지 잔량 비단조 방지)."""
    item = await _narcotic_item(session)
    await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=100, handler_id=1,
                        entry_on=date(2026, 6, 10))
    # 같은 날·이후는 허용
    await svc.add_entry(session, item_id=item.id, entry_type="OUT", qty=10, handler_id=1,
                        entry_on=date(2026, 6, 10))
    # 과거 거래일 → 차단
    with pytest.raises(ValidationError, match="직전 기록일 이후"):
        await svc.add_entry(session, item_id=item.id, entry_type="OUT", qty=5, handler_id=1,
                            entry_on=date(2026, 5, 3))


async def test_non_narcotic_item_rejected(session):
    item = Item(code="MED-X", name="일반약", category="DRUG", is_narcotic=False)
    session.add(item)
    await session.flush()
    with pytest.raises(ValidationError):
        await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=10, handler_id=1)


async def test_balance_and_seq_are_system_calculated(session):
    item = await _narcotic_item(session)
    e1 = await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=100, handler_id=1)
    e2 = await svc.add_entry(session, item_id=item.id, entry_type="OUT", qty=30, handler_id=2)
    e3 = await svc.add_entry(session, item_id=item.id, entry_type="DISPOSE", qty=5, handler_id=1)
    assert (e1.seq_no, e2.seq_no, e3.seq_no) == (1, 2, 3)
    assert (float(e1.balance_after), float(e2.balance_after), float(e3.balance_after)) \
        == (100.0, 70.0, 65.0)


async def test_negative_balance_rejected(session):
    item = await _narcotic_item(session)
    await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=10, handler_id=1)
    with pytest.raises(ValidationError):
        await svc.add_entry(session, item_id=item.id, entry_type="OUT", qty=20, handler_id=1)


async def test_hash_chain_valid_and_tamper_detected(session):
    """④ 무결성 — 정상 체인 통과, 변조 시 탐지."""
    item = await _narcotic_item(session)
    await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=100, handler_id=1)
    target = await svc.add_entry(session, item_id=item.id, entry_type="OUT", qty=30,
                                 handler_id=2)
    assert await svc.verify_chain(session, item_id=item.id) == []

    # 변조 시뮬레이션 (운영 DB는 REVOKE로 불가 — 검증 잡의 탐지력 확인)
    await session.execute(update(NarcoticsLedger)
                          .where(NarcoticsLedger.id == target.id).values(qty=-10))
    bad = await svc.verify_chain(session, item_id=item.id)
    assert target.id in bad


async def test_correction_appends_delta_and_preserves_original(session):
    """③ 정정 — 원본 보존, 차액 CORRECTION, balance 시스템 산출."""
    item = await _narcotic_item(session)
    await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=100, handler_id=1)
    out = await svc.add_entry(session, item_id=item.id, entry_type="OUT", qty=30, handler_id=2)

    # 실제 불출은 25였음 — 정정
    fix = await svc.correct(session, original_id=out.id, corrected_qty=25,
                            reason="기록 오류 — 실불출 25", handler_id=3)
    assert fix.entry_type == "CORRECTION" and fix.corrects_id == out.id
    assert float(fix.qty) == 5.0            # -30 → -25: 차액 +5
    assert float(fix.balance_after) == 75.0  # 70 + 5
    assert float(out.qty) == -30.0           # 원본 불변
    assert await svc.verify_chain(session, item_id=item.id) == []


async def test_correction_of_correction_references_original(session):
    """정정의 정정 — CORRECTION 직접 정정 금지, 원본 재정정."""
    item = await _narcotic_item(session)
    await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=100, handler_id=1)
    out = await svc.add_entry(session, item_id=item.id, entry_type="OUT", qty=30, handler_id=2)
    fix1 = await svc.correct(session, original_id=out.id, corrected_qty=25,
                             reason="1차 정정", handler_id=3)
    with pytest.raises(ValidationError):
        await svc.correct(session, original_id=fix1.id, corrected_qty=26,
                          reason="정정의 정정", handler_id=3)
    # 원본 재정정 — 누적 반영: 유효 -25 → -26, 차액 -1
    fix2 = await svc.correct(session, original_id=out.id, corrected_qty=26,
                             reason="2차 정정", handler_id=3)
    assert float(fix2.qty) == -1.0 and float(fix2.balance_after) == 74.0
    assert await svc.verify_chain(session, item_id=item.id) == []


async def test_correction_requires_reason(session):
    item = await _narcotic_item(session)
    entry = await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=10, handler_id=1)
    with pytest.raises(ValidationError):
        await svc.correct(session, original_id=entry.id, corrected_qty=9,
                          reason="", handler_id=1)


async def test_monthly_ledger_for_report(session):
    item = await _narcotic_item(session)
    await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=10, handler_id=1)
    yyyymm = date.today().strftime("%Y-%m")
    rows = await svc.monthly_ledger(session, item_id=item.id, yyyymm=yyyymm)
    assert len(rows) == 1


async def test_monthly_ledger_by_entry_date_not_created_at(session):
    """월 수불일지는 거래일(entry_on) 기준 — 전월 거래를 익월 입력해도 거래월에 귀속(FR-21)."""
    item = await _narcotic_item(session)
    # 1월 거래로 명시 입력(시스템 입력시각은 오늘이지만 거래일은 1월)
    await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=10, handler_id=1,
                        entry_on=date(2026, 1, 15))
    assert len(await svc.monthly_ledger(session, item_id=item.id, yyyymm="2026-01")) == 1
    assert await svc.monthly_ledger(session, item_id=item.id, yyyymm="2026-02") == []


async def test_require_license_blocks_expired(session):
    """P0-4 FR-47 — 면허 만료(license_flag=False) 시 마약류 취급 차단."""
    import pytest

    from erp.common.policy import require_license
    from erp.common.router import CurrentUser
    from erp.errors import ForbiddenError
    ok = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    expired = CurrentUser(user_id=2, role="ADMIN", dept_id=None, license_flag=False)
    require_license(ok)                       # 유효 — 통과
    with pytest.raises(ForbiddenError):
        require_license(expired)              # 만료 — 차단


async def test_workstation_narcotics_receiving_only(session):
    """작업대(kiosk)는 마약류 입고(IN)만 — 출고/폐기/정정은 면허 사용자(사용자 결정: 입고 전용)."""
    from erp.common.router import CurrentUser
    from erp.errors import ForbiddenError
    from erp.scm.router import (NarcoticCorrectBody, NarcoticEntryBody, narcotic_correct,
                                narcotic_entry)

    item = await _narcotic_item(session)
    ws = CurrentUser(user_id=901, role="WORKSTATION", dept_id=1, license_flag=True)

    # IN(입고) 허용
    out = await narcotic_entry(NarcoticEntryBody(item_id=item.id, entry_type="IN", qty=10),
                               user=ws, session=session)
    assert out["balance"] == 10
    # OUT/DISPOSE/ADJUST 거부
    for et in ("OUT", "DISPOSE", "ADJUST"):
        with pytest.raises(ForbiddenError, match="입고"):
            await narcotic_entry(NarcoticEntryBody(item_id=item.id, entry_type=et, qty=1),
                                 user=ws, session=session)
    # 정정도 거부
    with pytest.raises(ForbiddenError, match="정정"):
        await narcotic_correct(entry_id=1, body=NarcoticCorrectBody(corrected_qty=5, reason="x"),
                               user=ws, session=session)

    # 면허 사용자(MANAGER)는 OUT 허용(회귀 방지)
    mgr = CurrentUser(user_id=902, role="MANAGER", dept_id=1, license_flag=True)
    out2 = await narcotic_entry(NarcoticEntryBody(item_id=item.id, entry_type="OUT", qty=3),
                                user=mgr, session=session)
    assert out2["balance"] == 7


async def test_narcotics_recent_duplicate_rejected(session):
    """서버 멱등 — 직전과 동일한 수불(품목·구분·수량·로트·취급자·거래일)의 즉시 재기입 차단."""
    item = await _narcotic_item(session)
    await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=50, handler_id=1)
    # 동일 반복 = 중복 기입(방금) → 차단
    with pytest.raises(ValidationError, match="중복 기입"):
        await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=50, handler_id=1)
    # 다른 수량·구분·취급자는 통과(정상 연속 기록)
    await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=70, handler_id=1)
    await svc.add_entry(session, item_id=item.id, entry_type="OUT", qty=50, handler_id=1)


async def test_narcotics_duplicate_guard_expires_by_window(session):
    """시간창 밖(과거 created_at)의 동일 수불은 정상 통과 — 의도적 재입고 허용."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from erp.scm.models import NarcoticsLedger
    item = await _narcotic_item(session)
    e1 = await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=50, handler_id=1)
    # 직전 기록 created_at을 창 밖으로 밀어 과거로(의도적 후속 입고 시뮬레이션)
    await session.execute(update(NarcoticsLedger).where(NarcoticsLedger.id == e1.id)
                          .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    e2 = await svc.add_entry(session, item_id=item.id, entry_type="IN", qty=50, handler_id=1)
    assert e2.seq_no == 2 and float(e2.balance_after) == 100.0
