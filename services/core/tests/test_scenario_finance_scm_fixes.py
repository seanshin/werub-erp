"""2026-10-09 시나리오 S-E/S-D/S-A — 재무·SCM 결함 회귀.

A 결재선 단계 적격자 인가·상신 시점 결재선 / B 동시 결산 500 / C 외화환산 역분개 일자 /
D 지급결의 중 미지급의 반품 감액 / E 출입증 폐기 커밋 / G 전표 출처키 길이 가드.
"""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.errors import ValidationError
from erp.finance.models import Account, CorporateAccount, FiscalPeriod, Voucher, VoucherLine
from erp.finance.service import VoucherLineDTO, VoucherService
from tests._http import auth_as, make_user

vs = VoucherService()


async def _accounts(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="1115", name="외화예금", kind="ASSET"),
        Account(code="4110", name="외래수익", kind="REVENUE"),
        Account(code="3300", name="이익잉여금", kind="CAPITAL"),
        Account(code="4650", name="외화환산이익", kind="REVENUE"),
        Account(code="5670", name="외화환산손실", kind="EXPENSE")])
    await session.flush()


def _pair(amount, dr="1110", cr="4110"):
    return [VoucherLineDTO(dr, "DEBIT", amount), VoucherLineDTO(cr, "CREDIT", amount)]


async def _rule(session, doc_type, *roles):
    from erp.common.models import ApprovalRule
    session.add(ApprovalRule(doc_type=doc_type, min_amount=0, active=True,
                             steps=[{"type": "ROLE", "value": r} for r in roles]))
    await session.flush()


async def _pending_voucher(session, *, creator=1):
    v = await vs.create_draft(session, source_ref=f"sc-a-{creator}", voucher_on=date(2026, 6, 1),
                              lines=_pair(1000), created_by=creator)
    await vs.submit(session, voucher_id=v.id, actor=creator)
    return v


# ── A. 결재선 ROLE 단계 적격자 인가 ──────────────────────────────────
async def test_voucher_role_steps_complete_via_http(session, client):
    """MANAGER→DIRECTOR 결재선 — 둘 다 finance.vouchers write가 없어 종전엔 403(완주 불가)."""
    await _accounts(session)
    await make_user(session, user_id=1, role="FINANCE_MANAGER")
    hm = await auth_as(session, user_id=2, role="MANAGER")
    hd = await auth_as(session, user_id=3, role="DIRECTOR")
    hs = await auth_as(session, user_id=4, role="STAFF")
    await _rule(session, "VOUCHER", "MANAGER", "DIRECTOR")
    v = await _pending_voucher(session)
    # 상신 시점에 결재선 생성 → 1단계가 MANAGER 결재함에 보인다(종전엔 approve 전까지 인스턴스 없음)
    r = await client.get("/api/v1/approvals/worklist", headers=hm)
    assert [(i["doc_type"], i["doc_id"]) for i in r.json()["items"]] == [("VOUCHER", v.id)]
    # 단계 비적격(DIRECTOR가 1단계 / STAFF)은 여전히 403
    assert (await client.post(f"/api/v1/finance/vouchers/{v.id}/approve",
                              headers=hd)).status_code == 403
    assert (await client.post(f"/api/v1/finance/vouchers/{v.id}/approve",
                              headers=hs)).status_code == 403
    r = await client.post(f"/api/v1/finance/vouchers/{v.id}/approve", headers=hm)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PENDING"
    r = await client.post(f"/api/v1/finance/vouchers/{v.id}/approve", headers=hd)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "POSTED"


async def test_voucher_director_override_and_reject_by_step_approver(session, client):
    await _accounts(session)
    await make_user(session, user_id=1, role="FINANCE_MANAGER")
    hm = await auth_as(session, user_id=2, role="MANAGER")
    hd = await auth_as(session, user_id=3, role="DIRECTOR")
    await _rule(session, "VOUCHER", "MANAGER", "ADMIN")
    v = await _pending_voucher(session)
    r = await client.post(f"/api/v1/finance/vouchers/{v.id}/approve?override=true", headers=hd)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "POSTED"
    # 단계 적격자 반려
    v2 = await vs.create_draft(session, source_ref="sc-a-rej", voucher_on=date(2026, 6, 1),
                               lines=_pair(500), created_by=1)
    await vs.submit(session, voucher_id=v2.id, actor=1)
    r = await client.post(f"/api/v1/finance/vouchers/{v2.id}/reject", headers=hm,
                          json={"reason": "증빙 부족"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REJECTED"


async def test_voucher_without_rule_keeps_permission_gate(session, client):
    """룰 없는 문서(레거시 단일 승인)는 종전대로 도메인 권한만 — DIRECTOR 403 유지."""
    await _accounts(session)
    await make_user(session, user_id=1, role="FINANCE_MANAGER")
    hd = await auth_as(session, user_id=3, role="DIRECTOR")
    v = await _pending_voucher(session)
    assert (await client.post(f"/api/v1/finance/vouchers/{v.id}/approve?override=true",
                              headers=hd)).status_code == 403


async def test_step_approver_creator_still_blocked(session):
    """직무분리 — 작성자가 단계 적격 역할이어도 결재자 경로로 인가되지 않는다."""
    from erp.common.approval_engine import ApprovalEngine
    await _accounts(session)
    await make_user(session, user_id=2, role="MANAGER")
    await _rule(session, "VOUCHER", "MANAGER")
    v = await _pending_voucher(session, creator=2)
    assert not await ApprovalEngine().can_act_current_step(
        session, doc_type="VOUCHER", doc_id=v.id, actor=2)


async def _po_setup(session):
    from erp.scm.models import Item, Supplier
    from erp.scm.po_service import PurchaseOrderService
    sup = Supplier(name="대한약품", business_no="1018212345")
    item = Item(code="M001", name="수액세트", category="SUPPLY", is_narcotic=False,
                safety_stock_qty=10)
    session.add_all([sup, item])
    await session.flush()
    return await PurchaseOrderService().request(
        session, supplier_id=sup.id, actor=1,
        lines=[{"item_id": item.id, "qty": 10, "unit_price": 1000}])


async def test_po_director_step_and_worklist_at_request(session, client):
    await make_user(session, user_id=1, role="LOGISTICS_MANAGER")
    hm = await auth_as(session, user_id=2, role="MANAGER")
    hd = await auth_as(session, user_id=3, role="DIRECTOR")
    await _rule(session, "PO", "MANAGER", "DIRECTOR")
    po = await _po_setup(session)
    # 요청=상신 시점 결재선 → MANAGER 결재함에 1단계 노출
    r = await client.get("/api/v1/approvals/worklist", headers=hm)
    assert ("PO", po.id) in [(i["doc_type"], i["doc_id"]) for i in r.json()["items"]]
    r = await client.patch(f"/api/v1/inventory/purchase-orders/{po.id}/approve", headers=hm)
    assert r.status_code == 200 and r.json()["pending_next_step"], r.text
    # DIRECTOR는 scm.po write가 없어 종전엔 403 — 2단계 적격자로 인가
    r = await client.patch(f"/api/v1/inventory/purchase-orders/{po.id}/approve", headers=hd)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED"


async def test_contract_director_override(session, client):
    from erp.scm.contract_service import OutsourcingContractService
    svc = OutsourcingContractService()
    await make_user(session, user_id=1, role="LOGISTICS_MANAGER")
    hd = await auth_as(session, user_id=3, role="DIRECTOR")
    await _rule(session, "OUTSOURCING_CONTRACT", "MANAGER", "ADMIN")
    c = await svc.register(session, actor=1, vendor_name="청정급식", category="MEAL",
                           title="직원식당 위탁운영", monthly_amount=8_000_000,
                           start_on=date(2026, 1, 1), end_on=date(2026, 12, 31))
    await svc.submit_for_approval(session, actor=1, contract_id=c.id)
    r = await client.post(f"/api/v1/inventory/contracts/{c.id}/approve?override=true", headers=hd)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED"


# ── B. 동시 결산 경합 패자 ───────────────────────────────────────────
async def test_closing_unique_race_loser_recovers(session, monkeypatch):
    """경합 패자의 uq_voucher_source 위반을 500으로 흘리지 않고 재평가해 결과를 돌려준다."""
    from sqlalchemy.exc import IntegrityError

    from erp.finance.closing_service import ClosingService
    await _accounts(session)
    await vs.create(session, source_module="manual", source_ref="rev",
                    voucher_on=date(2025, 6, 1), lines=_pair(1000))
    await session.flush()
    cs = ClosingService()
    real = cs._vs.create
    calls = {"n": 0}

    async def racing_create(s, **kw):
        calls["n"] += 1
        if calls["n"] == 1:      # 첫 INSERT가 동시 호출과 부딪힌 상황
            raise IntegrityError("INSERT INTO finance.vouchers", {}, Exception("uq_voucher_source"))
        return await real(s, **kw)

    monkeypatch.setattr(cs._vs, "create", racing_create)
    out = await cs.close_year(session, fiscal_year=2025)
    assert out["net_income"] == 1000 and calls["n"] == 2
    n = await session.scalar(select(func.count()).select_from(Voucher).where(
        Voucher.source_module == "closing"))
    assert n == 1


# ── C. 외화환산 역분개는 원 전표일자 ─────────────────────────────────
async def _gl(session, code, until):
    d, c = (await session.execute(select(
        func.coalesce(func.sum(VoucherLine.amount).filter(VoucherLine.side == "DEBIT"), 0),
        func.coalesce(func.sum(VoucherLine.amount).filter(VoucherLine.side == "CREDIT"), 0))
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(VoucherLine.account_code == code, Voucher.voucher_on <= until,
               Voucher.status.in_(("POSTED", "REVERSED"))))).one()
    return float(d) - float(c)


async def test_fx_translation_reverse_rerun_correct_in_both_years(session):
    from erp.finance.forex_service import ForexTranslationService
    fx = ForexTranslationService()
    await _accounts(session)
    acct = CorporateAccount(alias="USD", bank_name="신한", account_no="1", currency="USD",
                            gl_account_code="1115")
    session.add(acct)
    await session.flush()
    await vs.create(session, source_module="manual", source_ref="fxopen",
                    voucher_on=date(2025, 1, 5), lines=_pair(12_000_000, "1115", "1110"))
    r1 = await fx.post(session, account_id=acct.id, fc_balance=10000, rate=1300, yyyymm="2025-12")
    assert r1["delta"] == 1_000_000             # 잘못된 환율 입력(정답 1250)
    await session.flush()
    rev = await vs.reverse(session, voucher_id=r1["voucher_id"], reason="환율 오입력")
    assert rev.voucher_on == date(2025, 12, 31)  # 원 전표일자
    await session.flush()
    r2 = await fx.post(session, account_id=acct.id, fc_balance=10000, rate=1250, yyyymm="2025-12")
    assert r2["posted"] and r2["delta"] == 500_000
    await session.flush()
    assert await _gl(session, "1115", date(2025, 12, 31)) == 12_500_000
    # 다음 연도에도 같은 잔액 — 종전엔 오늘자 역분개로 11,500,000(과소)·4650 차변 잔류
    assert await _gl(session, "1115", date(2026, 12, 31)) == 12_500_000
    assert await _gl(session, "4650", date(2026, 12, 31)) == -500_000
    assert await _gl(session, "4650", date(2025, 12, 31)) == -500_000


async def test_fx_translation_reverse_in_closed_period_needs_reopen(session):
    from erp.finance.forex_service import ForexTranslationService
    await _accounts(session)
    acct = CorporateAccount(alias="USD", bank_name="신한", account_no="1", currency="USD",
                            gl_account_code="1115")
    session.add(acct)
    await session.flush()
    await vs.create(session, source_module="manual", source_ref="fxopen",
                    voucher_on=date(2025, 1, 5), lines=_pair(12_000_000, "1115", "1110"))
    r1 = await ForexTranslationService().post(session, account_id=acct.id, fc_balance=10000,
                                              rate=1300, yyyymm="2025-12")
    session.add(FiscalPeriod(yyyymm="2025-12", fiscal_year=2025, status="CLOSED"))
    await session.flush()
    with pytest.raises(ValidationError, match="재개방"):
        await vs.reverse(session, voucher_id=r1["voucher_id"], reason="정정")


async def test_general_reversal_still_dated_today(session):
    await _accounts(session)
    v = await vs.create(session, source_module="manual", source_ref="gen",
                        voucher_on=date(2025, 3, 1), lines=_pair(100))
    await session.flush()
    rev = await vs.reverse(session, voucher_id=v.id, reason="정정")
    assert rev.voucher_on == date.today()


# ── D. 지급결의 중 미지급의 반품 감액 차단 ───────────────────────────
async def test_return_blocked_when_payable_in_open_batch(session):
    from erp.finance.disbursement_service import DisbursementService
    from erp.finance.models import Payable
    from erp.scm.models import Item, Lot, Supplier
    from erp.scm.po_service import PurchaseOrderService
    from erp.scm.return_service import PurchaseReturnService
    po_svc, disb = PurchaseOrderService(), DisbursementService()
    sup = Supplier(name="대한약품", business_no="1018212345")
    item = Item(code="M001", name="수액세트", category="SUPPLY", is_narcotic=False,
                safety_stock_qty=10)
    session.add_all([sup, item])
    await session.flush()
    po = await po_svc.request(session, supplier_id=sup.id, actor=1,
                              lines=[{"item_id": item.id, "qty": 100, "unit_price": 5000}])
    await po_svc.approve(session, po_id=po.id, actor=2)
    await po_svc.receive(session, po_id=po.id, actor=2, received_on=date(2026, 5, 1),
                         receipts=[{"item_id": item.id, "lot_no": "L1",
                                    "expiry_on": date(2027, 5, 1), "qty": 100}])
    lot = await session.scalar(select(Lot).where(Lot.item_id == item.id))
    pay = await session.scalar(select(Payable).where(Payable.source_ref == f"po-{po.id}-r1"))
    batch = await disb.create_batch(session, payable_ids=[pay.id],
                                    scheduled_on=date(2026, 5, 20), actor=3)
    with pytest.raises(ValidationError, match="지급결의"):
        await PurchaseReturnService().create_return(
            session, po_id=po.id, lot_id=lot.id, qty=30, reason="DAMAGE",
            returned_on=date(2026, 5, 10), actor=2)
    assert float(pay.amount) == 500_000 and float(lot.remaining_qty) == 100
    # 결의 취소 후에는 반품 가능
    await disb.cancel(session, batch_id=batch.id)
    await PurchaseReturnService().create_return(
        session, po_id=po.id, lot_id=lot.id, qty=30, reason="DAMAGE",
        returned_on=date(2026, 5, 10), actor=2)
    assert float(pay.amount) == 350_000


# ── E. 출입증 폐기는 응답 전에 커밋 ──────────────────────────────────
async def test_ws_pass_revoke_commits_before_response(session, client, monkeypatch):
    from erp.common.workstation_pass_service import WorkstationPassService
    h = await auth_as(session, user_id=1, role="ADMIN")
    wp, _pin = await WorkstationPassService().create(session, actor=1)
    commits = {"n": 0}
    real_commit = session.commit

    async def counting_commit():
        commits["n"] += 1
        await real_commit()

    monkeypatch.setattr(session, "commit", counting_commit)
    r = await client.post(f"/api/v1/inventory/workstation/passes/{wp.id}/revoke", headers=h)
    assert r.status_code == 200, r.text
    assert commits["n"] == 1


# ── G. 전표 출처키 길이 가드(PG VARCHAR(64)) ─────────────────────────
async def test_source_ref_over_64_rejected_on_any_db(session):
    await _accounts(session)
    long_ref = "his-refund:" + "0123456789abcdef" * 6        # 107자 — HIS UUID 기반 키 재현
    with pytest.raises(ValidationError, match="64자"):
        await vs.create(session, source_module="patient", source_ref=long_ref,
                        voucher_on=date(2026, 6, 1), lines=_pair(100))
    with pytest.raises(ValidationError, match="64자"):
        await vs.create_draft(session, source_ref=long_ref, voucher_on=date(2026, 6, 1),
                              lines=_pair(100))
    ok = await vs.create(session, source_module="patient", source_ref="x" * 64,
                         voucher_on=date(2026, 6, 1), lines=_pair(100))
    assert ok.id


async def test_reversal_of_long_ref_stays_within_64(session):
    await _accounts(session)
    v = await vs.create(session, source_module="patient", source_ref="r" * 60,
                        voucher_on=date(2026, 6, 1), lines=_pair(100))
    await session.flush()
    rev = await vs.reverse(session, voucher_id=v.id, reason="정정")
    assert len(rev.source_ref) <= 64 and rev.source_ref.endswith(f"/reversal-{v.id}")

