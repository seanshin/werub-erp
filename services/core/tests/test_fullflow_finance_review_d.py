"""2026-10-09 전체 흐름 검토 D — 재무 원장 결함 회귀(이중 전기·체크포인트·해시체인·결재선·외화·결산)."""
from datetime import date

import pytest
from sqlalchemy import func, select, update

from erp.errors import ValidationError
from erp.finance.models import (
    Account, AccountBalanceCheckpoint, CorporateAccount, FiscalPeriod, Voucher, VoucherLine,
)
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.finance.statements import StatementService

vs = VoucherService()
ss = StatementService()


async def _accounts(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="1115", name="외화예금", kind="ASSET"),
        Account(code="4110", name="외래수익", kind="REVENUE"),
        Account(code="5310", name="급여비용", kind="EXPENSE"),
        Account(code="3300", name="이익잉여금", kind="CAPITAL"),
        Account(code="4650", name="외화환산이익", kind="REVENUE"),
        Account(code="5670", name="외화환산손실", kind="EXPENSE")])
    await session.flush()


def _pair(amount, dr="1110", cr="4110"):
    return [VoucherLineDTO(dr, "DEBIT", amount), VoucherLineDTO(cr, "CREDIT", amount)]


async def _line_count(session, voucher_id):
    return await session.scalar(select(func.count()).select_from(VoucherLine)
                                .where(VoucherLine.voucher_id == voucher_id))


# ── HIGH-1 동시 승인 이중 전기 ─────────────────────────────────────
async def test_approve_rereads_committed_status_no_double_post(session):
    """다른 트랜잭션이 먼저 승인·커밋한 전표를 낡은 PENDING 스냅샷으로 다시 승인하면 안 된다.

    종전 session.get은 identity map의 PENDING 객체를 그대로 써 라인을 한 번 더 INSERT(이중 전기)."""
    await _accounts(session)
    v = await vs.create_draft(session, source_ref="d-hi1", voucher_on=date(2026, 6, 1),
                              lines=_pair(1000), created_by=1)
    await vs.submit(session, voucher_id=v.id, actor=1)
    await vs.approve(session, voucher_id=v.id, actor=2)
    assert await _line_count(session, v.id) == 2
    # 경쟁 트랜잭션의 시야 재현 — 이 세션의 객체는 PENDING인 채(커밋 전 스냅샷)로 되돌린다.
    v.status = "PENDING"
    await session.execute(update(Voucher).where(Voucher.id == v.id).values(status="POSTED")
                          .execution_options(synchronize_session=False))
    with pytest.raises(ValidationError):
        await vs.approve(session, voucher_id=v.id, actor=3)
    assert await _line_count(session, v.id) == 2


# ── MED-1 결재 도중 룰 비활성화 ──────────────────────────────────────
async def test_rule_disabled_midflight_keeps_remaining_steps(session):
    from erp.common.models import ApprovalInstance, ApprovalRule, User
    await _accounts(session)
    session.add_all([User(id=1, email="a@x", password_hash="x", role="STAFF"),
                     User(id=2, email="b@x", password_hash="x", role="MANAGER"),
                     User(id=3, email="c@x", password_hash="x", role="DIRECTOR")])
    rule = ApprovalRule(doc_type="VOUCHER", min_amount=0, active=True,
                        steps=[{"type": "ROLE", "value": "MANAGER"},
                               {"type": "ROLE", "value": "DIRECTOR"}])
    session.add(rule)
    await session.flush()
    v = await vs.create_draft(session, source_ref="d-med1", voucher_on=date(2026, 6, 1),
                              lines=_pair(1000), created_by=1)
    await vs.submit(session, voucher_id=v.id, actor=1)
    await vs.approve(session, voucher_id=v.id, actor=2)      # 1단계(MANAGER)
    assert v.status == "PENDING"
    rule.active = False                                       # 결재 도중 룰 끔
    await session.flush()
    # 남은 2단계(DIRECTOR) 적격이 아닌 MANAGER가 다시 승인 → 레거시 폴백으로 전기되면 안 된다
    with pytest.raises(ValidationError):
        await vs.approve(session, voucher_id=v.id, actor=2)
    assert v.status == "PENDING" and await _line_count(session, v.id) == 0
    await vs.approve(session, voucher_id=v.id, actor=3)      # 2단계 완주 → 전기
    assert v.status == "POSTED"
    inst = await session.scalar(select(ApprovalInstance).where(ApprovalInstance.doc_id == v.id))
    assert inst.status == "APPROVED"


# ── HIGH-2 소급 전기 vs 잔액 체크포인트 ──────────────────────────────
async def _bs_cash(session, as_of):
    rows = {c: (d, cr) for c, _n, _k, d, cr in await ss._by_account_as_of(session, until=as_of)}
    d, cr = rows.get("1110", (0, 0))
    return int(d) - int(cr)


async def test_backdated_posting_invalidates_checkpoint(session):
    await _accounts(session)
    await vs.create(session, source_module="manual", source_ref="jan", voucher_on=date(2026, 1, 10),
                    lines=_pair(1000))
    await ss.refresh_balance_checkpoint(session, through=date(2026, 1, 31))
    # 체크포인트 경계 이전 날짜로 늦게 전기(결산 조정 등)
    await vs.create(session, source_module="manual", source_ref="late", voucher_on=date(2026, 1, 20),
                    lines=_pair(500))
    await session.flush()
    assert await _bs_cash(session, date(2026, 3, 31)) == 1500
    assert (await ss.verify_balance_checkpoint(session, as_of=date(2026, 3, 31)))["ok"]


async def test_balance_checkpoint_handler_self_heals(session):
    """무효화를 비껴간 낡은 체크포인트(배치 경합)도 배치가 재적재로 고친다 — 경보만 남기던 종전 동작 회귀."""
    from erp.app.recurring_handlers import balance_checkpoint
    await _accounts(session)
    await vs.create(session, source_module="manual", source_ref="jan", voucher_on=date(2026, 1, 10),
                    lines=_pair(1000))
    await ss.refresh_balance_checkpoint(session, through=date(2026, 1, 31))
    # 경합 재현 — 체크포인트를 틀린 값으로 오염(전기 시점 무효화가 놓친 상태)
    await session.execute(update(AccountBalanceCheckpoint)
                          .where(AccountBalanceCheckpoint.account_code == "1110")
                          .values(cum_debit=1)
                          .execution_options(synchronize_session=False))
    session.expire_all()
    out = await balance_checkpoint(session, None, {})
    assert out["ok"] and out["healed"]
    assert await _bs_cash(session, date(2026, 3, 31)) == 1000
    stale = await session.scalar(select(AccountBalanceCheckpoint.cum_debit).where(
        AccountBalanceCheckpoint.through == date(2026, 1, 31),
        AccountBalanceCheckpoint.account_code == "1110"))
    assert int(stale) == 1000


# ── HIGH-3 해시체인 수평선 잠금 ──────────────────────────────────────
class _FakeDialect:
    name = "postgresql"


class _FakeBind:
    dialect = _FakeDialect()


class _FakeNested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePgSession:
    def __init__(self, fail_with=None):
        self.sql: list[str] = []
        self.fail_with = fail_with

    def get_bind(self):
        return _FakeBind()

    def begin_nested(self):
        return _FakeNested()

    async def execute(self, stmt):
        self.sql.append(str(stmt))
        if self.fail_with and "LOCK TABLE" in str(stmt):
            raise self.fail_with


async def test_ledger_checkpoint_takes_share_lock_on_pg():
    from erp.finance.ledger_integrity_service import _acquire_horizon_lock
    s = _FakePgSession()
    assert await _acquire_horizon_lock(s) == (True, None)
    assert any("LOCK TABLE finance.voucher_lines IN SHARE ROW EXCLUSIVE MODE" in q for q in s.sql)
    assert any("lock_timeout" in q for q in s.sql)


async def test_ledger_checkpoint_lock_timeout_skips_and_error_flags():
    from sqlalchemy.exc import DBAPIError

    from erp.finance.ledger_integrity_service import _acquire_horizon_lock
    t = DBAPIError("LOCK", {}, Exception("canceling statement due to lock timeout"))
    assert await _acquire_horizon_lock(_FakePgSession(t)) == (False, "timeout")
    e = DBAPIError("LOCK", {}, Exception("permission denied for table voucher_lines"))
    ok, why = await _acquire_horizon_lock(_FakePgSession(e))
    assert not ok and why.startswith("error:")


async def test_ledger_checkpoint_skips_when_lock_unavailable(session, monkeypatch):
    from erp.finance import ledger_integrity_service as lis
    await _accounts(session)
    await vs.create(session, source_module="manual", source_ref="x", voucher_on=date(2026, 6, 1),
                    lines=_pair(100))

    async def _no_lock(_s):
        return False, "timeout"
    monkeypatch.setattr(lis, "_acquire_horizon_lock", _no_lock)
    out = await lis.LedgerIntegrityService().checkpoint(session)
    assert out["created"] is False and out["skipped"]


# ── MED-2 외화환산 마감기간·멱등 응답 ─────────────────────────────────
async def _fx_seed(session):
    await _accounts(session)
    acct = CorporateAccount(alias="USD", bank_name="신한", account_no="1", currency="USD",
                            gl_account_code="1115")
    session.add(acct)
    await session.flush()
    await vs.create(session, source_module="manual", source_ref="fxopen",
                    voucher_on=date(2026, 1, 5), lines=_pair(12_000_000, "1115", "1110"))
    return acct


async def test_forex_post_blocked_in_closed_period(session):
    from erp.finance.forex_service import ForexTranslationService
    acct = await _fx_seed(session)
    session.add(FiscalPeriod(yyyymm="2026-06", fiscal_year=2026, status="CLOSED"))
    await session.flush()
    with pytest.raises(ValidationError):
        await ForexTranslationService().post(session, account_id=acct.id, fc_balance=10000,
                                             rate=1300, yyyymm="2026-06")


async def test_forex_post_idempotent_hit_reports_not_posted(session):
    from erp.finance.forex_service import ForexTranslationService
    svc = ForexTranslationService()
    acct = await _fx_seed(session)
    r1 = await svc.post(session, account_id=acct.id, fc_balance=10000, rate=1300, yyyymm="2026-06")
    assert r1["posted"]
    await session.flush()
    # 다른 환율로 재호출 — 새 전표는 안 생기는데 posted:True·가짜 delta를 돌려주면 안 된다
    r2 = await svc.post(session, account_id=acct.id, fc_balance=10000, rate=1400, yyyymm="2026-06")
    assert r2["posted"] is False and r2["voucher_id"] == r1["voucher_id"]
    assert "delta" not in r2


# ── MED-3 결산 후 전기 손익의 보충 결산 ───────────────────────────────
async def test_supplementary_closing_moves_late_pl(session):
    from erp.finance.closing_service import ClosingService
    cs = ClosingService()
    await _accounts(session)
    await vs.create(session, source_module="manual", source_ref="rev", voucher_on=date(2025, 6, 1),
                    lines=_pair(1000))
    await session.flush()
    o1 = await cs.close_year(session, fiscal_year=2025)
    # 결산 후 그 회계연도로 비용 300 전기(마감 우회 조정)
    await vs.create(session, source_module="closing-ifrs", source_ref="late-adj",
                    voucher_on=date(2025, 12, 31), lines=_pair(300, "5310", "1110"),
                    allow_closed=True)
    await session.flush()
    o2 = await cs.close_year(session, fiscal_year=2025)
    assert o2.get("supplementary") and o2["voucher_id"] != o1["voucher_id"]
    assert o2["net_income"] == -300
    v2 = await session.get(Voucher, o2["voucher_id"])
    assert v2.source_ref == "closing:2025:1"
    await session.flush()
    bs = await ss.balance_sheet(session, as_of=date(2025, 12, 31))
    assert bs["capital_total"] == 700 and round(bs["retained_earnings"]) == 0 and bs["balanced"]
    # 잔여 없으면 멱등
    o3 = await cs.close_year(session, fiscal_year=2025)
    assert o3["already_closed"] and o3["voucher_id"] == o2["voucher_id"]
