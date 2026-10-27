"""회계 기간 마감 — 손익 스냅샷·전표 차단·재개방."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.finance.period_service import PeriodService
from erp.finance.service import VoucherLineDTO, VoucherService

svc = PeriodService()
vs = VoucherService()


async def _seed_accounts(session):
    from erp.finance.models import Account
    session.add_all([Account(code="4110", name="외래수익", kind="REVENUE"),
                     Account(code="1110", name="현금", kind="ASSET")])
    await session.flush()


async def _voucher(session, on, ref):
    return await vs.create(session, source_module="manual", source_ref=ref, voucher_on=on,
                           lines=[VoucherLineDTO("4110", "CREDIT", 1000),
                                  VoucherLineDTO("1110", "DEBIT", 1000)])


async def test_close_snapshots_and_locks(session):
    await _seed_accounts(session)
    await _voucher(session, date(2026, 3, 10), "v-mar-1")
    await session.flush()
    p = await svc.close(session, yyyymm="2026-03", user_id=1)
    assert p.status == "CLOSED" and p.voucher_count == 1
    assert p.total_revenue == 1000 and p.net_income == 1000
    # 마감 기간 전표 생성 차단
    with pytest.raises(ValidationError, match="마감된 기간"):
        await _voucher(session, date(2026, 3, 15), "v-mar-2")


async def test_reopen_allows_posting(session):
    await _voucher(session, date(2026, 4, 1), "v-apr-1")
    await session.flush()
    await svc.close(session, yyyymm="2026-04", user_id=1)
    with pytest.raises(ValidationError):
        await _voucher(session, date(2026, 4, 5), "v-apr-2")
    await svc.reopen(session, yyyymm="2026-04", user_id=2, note="정정")   # 마감자≠재개자(SoD)
    v = await _voucher(session, date(2026, 4, 5), "v-apr-2")   # 재개방 후 통과
    assert v is not None


async def test_reopen_self_blocked_sod(session):
    """직무분리(G2) — 마감한 본인은 기간을 재개할 수 없다."""
    await svc.close(session, yyyymm="2026-10", user_id=7)
    with pytest.raises(ValidationError, match="직무분리"):
        await svc.reopen(session, yyyymm="2026-10", user_id=7, note="본인 재개 시도")
    # 다른 관리자는 재개 가능
    p = await svc.reopen(session, yyyymm="2026-10", user_id=8, note="타 관리자 재개")
    assert p.status == "OPEN"


async def test_reopen_preserves_close_attribution(session):
    """재개가 마감 귀속(closed_by/at)을 파괴하지 않고 재개 이력을 별도 보존(G2)."""
    p = await svc.close(session, yyyymm="2026-11", user_id=7)
    assert p.closed_by == 7 and p.closed_at is not None
    p = await svc.reopen(session, yyyymm="2026-11", user_id=8, note="정정")
    assert p.status == "OPEN"
    assert p.closed_by == 7 and p.closed_at is not None       # 마감 귀속 보존
    assert p.reopened_by == 8 and p.reopened_at is not None    # 재개 이력 기록
    assert p.reopen_note == "정정" and p.reopen_count == 1


async def test_snapshot_versioning_on_reopen_reclose(session):
    """재개 시 확정 스냅샷 supersede, 재마감 시 새 버전 append(무버전 덮어쓰기 금지·G2)."""
    from sqlalchemy import func, select

    from erp.finance.models import ClosingSnapshot
    await _seed_accounts(session)
    await _voucher(session, date(2026, 12, 3), "v-dec")
    await session.flush()
    await svc.close(session, yyyymm="2026-12", user_id=7)
    await svc.reopen(session, yyyymm="2026-12", user_id=8, note="정정")
    await svc.close(session, yyyymm="2026-12", user_id=7)      # 재마감 → v2
    # 총 스냅샷 8건(v1 superseded 4 + v2 active 4), 활성은 kind별 1개(4건)
    total = await session.scalar(select(func.count()).select_from(ClosingSnapshot)
                                 .where(ClosingSnapshot.yyyymm == "2026-12"))
    active = await session.scalar(select(func.count()).select_from(ClosingSnapshot)
                                  .where(ClosingSnapshot.yyyymm == "2026-12",
                                         ClosingSnapshot.superseded_at.is_(None)))
    assert total == 8 and active == 4
    maxv = await session.scalar(select(func.max(ClosingSnapshot.version))
                                .where(ClosingSnapshot.yyyymm == "2026-12"))
    assert maxv == 2


async def test_close_twice_rejected(session):
    await svc.close(session, yyyymm="2026-05", user_id=1)
    with pytest.raises(ValidationError, match="이미 마감"):
        await svc.close(session, yyyymm="2026-05", user_id=1)


# ── M3 마감 전 점검 + 확정 스냅샷 ────────────────────────────
async def test_preclose_check_flags_unposted_and_cost(session):
    """미결 전표(DRAFT)·미배부 원가를 경고로 포착."""
    await _seed_accounts(session)
    await _voucher(session, date(2026, 7, 3), "v-jul-ok")              # 정상(POSTED)
    await vs.create_draft(session, source_ref="v-jul-draft", voucher_on=date(2026, 7, 4),
                          lines=[VoucherLineDTO("4110", "CREDIT", 500),
                                 VoucherLineDTO("1110", "DEBIT", 500)], created_by=1)
    await session.flush()
    chk = await svc.preclose_check(session, yyyymm="2026-07")
    codes = {w["code"] for w in chk["warnings"]}
    assert chk["ok"] is False
    assert "UNPOSTED_VOUCHERS" in codes and "NO_COST_ALLOCATION" in codes


async def test_close_enforce_blocks_on_warnings(session):
    """enforce=True면 점검 경고 시 마감 차단(기본은 경고만)."""
    await _seed_accounts(session)
    await _voucher(session, date(2026, 8, 3), "v-aug")
    await session.flush()
    with pytest.raises(ValidationError, match="점검 실패"):
        await svc.close(session, yyyymm="2026-08", user_id=1, enforce=True)
    # 경고 무시(기본)면 마감 성공
    p = await svc.close(session, yyyymm="2026-08", user_id=1)
    assert p.status == "CLOSED"


async def test_reconcile_detects_post_close_drift(session):
    """마감 무결성 대사(G6·S6) — 스냅샷 일치 시 무드리프트, allow_closed 사후전기 시 드리프트 탐지."""
    await _seed_accounts(session)
    await _voucher(session, date(2027, 1, 5), "rec-1")
    await session.flush()
    await svc.close(session, yyyymm="2027-01", user_id=1)
    r = await svc.reconcile(session, yyyymm="2027-01")           # 마감 직후 = 일치
    assert r["snapshot_present"] is True and r["drifted"] is False
    # allow_closed로 마감월 사후 전기 → 재도출 재무제표가 확정 스냅샷과 어긋남
    await vs.create(session, source_module="closing-ifrs", source_ref="rec-adj",
                    voucher_on=date(2027, 1, 20),
                    lines=[VoucherLineDTO("4110", "CREDIT", 500),
                           VoucherLineDTO("1110", "DEBIT", 500)], allow_closed=True)
    await session.flush()
    r2 = await svc.reconcile(session, yyyymm="2027-01")
    assert r2["drifted"] is True
    assert ("income", "net_income") in {(d["statement"], d["field"]) for d in r2["drifts"]}


async def test_reconcile_requires_closed(session):
    with pytest.raises(ValidationError, match="마감된 기간이 아닙니다"):
        await svc.reconcile(session, yyyymm="2027-02")


async def test_voucher_create_emits_audit(session):
    """자동/시스템 전표도 원장 생성 감사에 남긴다(G3·G5) — created_by null·allow_closed 포착."""
    from sqlalchemy import select

    from erp.audit.models import AuditLog
    await _seed_accounts(session)
    v = await vs.create(session, source_module="depreciation", source_ref="dep-2026-01",
                        voucher_on=date(2026, 1, 10),
                        lines=[VoucherLineDTO("4110", "CREDIT", 1000),
                               VoucherLineDTO("1110", "DEBIT", 1000)],
                        created_by=None, allow_closed=True)
    await session.flush()
    row = (await session.execute(select(AuditLog).where(
        AuditLog.entity == "finance.vouchers", AuditLog.action == "VOUCHER_CREATE",
        AuditLog.target_id == str(v.id)))).scalar_one()
    assert row.actor is None                                   # 시스템 전기 = null actor
    assert row.after["source_module"] == "depreciation"
    assert row.after["allow_closed"] is True                   # 마감우회 전기 추적


async def test_close_saves_financial_snapshots(session):
    """마감 시 재무제표 4종 확정 스냅샷 보존."""
    from sqlalchemy import func, select

    from erp.finance.models import ClosingSnapshot
    await _seed_accounts(session)
    await _voucher(session, date(2026, 9, 3), "v-sep")
    await session.flush()
    await svc.close(session, yyyymm="2026-09", user_id=1)
    n = await session.scalar(select(func.count()).select_from(ClosingSnapshot)
                             .where(ClosingSnapshot.yyyymm == "2026-09"))
    assert n == 4                                          # trial_balance·income·balance_sheet·cash_flow
    kinds = set((await session.execute(
        select(ClosingSnapshot.kind).where(ClosingSnapshot.yyyymm == "2026-09"))).scalars())
    assert kinds == {"trial_balance", "income", "balance_sheet", "cash_flow"}
