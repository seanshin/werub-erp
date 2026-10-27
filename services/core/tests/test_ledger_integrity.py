"""원장 무결성 해시 체인 (G11) — 체크포인트·검증·변조 탐지."""
from datetime import date

from sqlalchemy import select, update

from erp.finance.ledger_integrity_service import LedgerIntegrityService
from erp.finance.models import Account, VoucherLine
from erp.finance.service import VoucherLineDTO, VoucherService

vs = VoucherService()
lis = LedgerIntegrityService()


async def _seed(session):
    session.add_all([Account(code="4110", name="수익", kind="REVENUE"),
                     Account(code="1110", name="현금", kind="ASSET")])
    await session.flush()


async def _v(session, ref, amt=1000):
    return await vs.create(session, source_module="manual", source_ref=ref,
                           voucher_on=date(2026, 3, 1),
                           lines=[VoucherLineDTO("4110", "CREDIT", amt),
                                  VoucherLineDTO("1110", "DEBIT", amt)])


async def test_checkpoint_chains_and_verifies(session):
    await _seed(session)
    await _v(session, "v1")
    await _v(session, "v2")
    await session.flush()
    out = await lis.checkpoint(session, actor=1)
    assert out["created"] is True and out["line_count"] == 4
    v = await lis.verify(session)
    assert v["ok"] is True and v["checkpoints"] == 1
    # 신규 라인 추가 → 2번째 체크포인트가 체인 확장(증분)
    await _v(session, "v3")
    await session.flush()
    out2 = await lis.checkpoint(session, actor=1)
    assert out2["line_count"] == 6 and out2["content_hash"] != out["content_hash"]
    assert (await lis.verify(session))["ok"] is True


async def test_checkpoint_noop_when_no_new_lines(session):
    await _seed(session)
    await _v(session, "v1")
    await session.flush()
    await lis.checkpoint(session, actor=1)
    out = await lis.checkpoint(session, actor=1)
    assert out["created"] is False


async def test_verify_detects_tampering(session):
    await _seed(session)
    await _v(session, "v1")
    await session.flush()
    await lis.checkpoint(session, actor=1)
    assert (await lis.verify(session))["ok"] is True
    # 트리거 없는 sqlite에서 라인 금액 변조 = superuser의 트리거 우회 변조를 시뮬
    lid = await session.scalar(select(VoucherLine.id).order_by(VoucherLine.id).limit(1))
    await session.execute(update(VoucherLine).where(VoucherLine.id == lid).values(amount=99999))
    await session.flush()
    v = await lis.verify(session)
    assert v["ok"] is False and v["broken_checkpoint_id"] is not None
