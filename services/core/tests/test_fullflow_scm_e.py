"""2026-10-09 전체 흐름 검토 E — scm·AP 흐름 회귀(위탁료 GL·기간·as-of·결재선·반품·지급결의·자산 AP·중복품목)."""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.common.models import ApprovalRule, User
from erp.errors import ValidationError
from erp.finance.asset_service import AssetService
from erp.finance.disbursement_service import DisbursementService
from erp.finance.models import Payable, Voucher, VoucherLine
from erp.finance.settlement_service import SettlementService
from erp.scm.contract_service import OutsourcingContractService
from erp.scm.models import Item, Lot, Supplier
from erp.scm.po_service import PurchaseOrderService
from erp.scm.return_service import PurchaseReturnService

oc = OutsourcingContractService()
po_svc = PurchaseOrderService()
ret_svc = PurchaseReturnService()
disb = DisbursementService()
settle = SettlementService()


async def _lines(session, ref):
    rows = (await session.execute(select(VoucherLine).join(
        Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.source_ref == ref))).scalars().all()
    return {(x.account_code, x.side, float(x.amount)) for x in rows}


async def _contract(session, **kw):
    d = dict(vendor_name="청정급식", category="MEAL", title="급식 위탁",
             monthly_amount=8_000_000, start_on=date(2026, 3, 1), end_on=date(2026, 12, 31))
    d.update(kw)
    return await oc.register(session, actor=1, **d)


# 1. 위탁료 발생이 GL 전표(DR 비용 / CR 2110)를 동반
async def test_accrue_month_posts_expense_and_ap_voucher(session):
    c = await _contract(session)
    out = await oc.accrue_month(session, actor=1, contract_id=c.id, yyyymm="2026-06")
    assert await _lines(session, f"ap-bill-{out['payable_id']}") == {
        ("5510", "DEBIT", 8_000_000), ("2110", "CREDIT", 8_000_000)}
    ap = await session.get(Payable, out["payable_id"])
    assert ap.source_ref == f"contract:{c.id}:2026-06"


# 2-a. 계약기간 밖 월 발생 차단
async def test_accrue_month_outside_period_rejected(session):
    c = await _contract(session)
    with pytest.raises(ValidationError, match="계약기간"):
        await oc.accrue_month(session, actor=1, contract_id=c.id, yyyymm="2026-02")
    with pytest.raises(ValidationError, match="계약기간"):
        await oc.accrue_month(session, actor=1, contract_id=c.id, yyyymm="2027-01")


# 2-b. 변경 발효일 이전 월은 원 단가(as-of)
async def test_accrue_month_uses_amount_as_of_effective_date(session):
    c = await _contract(session)
    await oc.amend_amount(session, actor=1, contract_id=c.id, new_monthly=9_000_000,
                          effective_on=date(2026, 7, 1))
    may = await oc.accrue_month(session, actor=1, contract_id=c.id, yyyymm="2026-05")
    jul = await oc.accrue_month(session, actor=1, contract_id=c.id, yyyymm="2026-07")
    assert may["amount"] == 8_000_000 and jul["amount"] == 9_000_000


# 2-c. 월마감 일괄은 기간 밖 계약을 건너뜀(실패하지 않음)
async def test_month_end_skips_out_of_period_contract(session):
    from erp.app.month_end_service import MonthEndService
    await _contract(session, start_on=date(2026, 9, 1))
    out = await MonthEndService().run(session, yyyymm="2026-06", user_id=1)
    assert out["contract_accrued"] == 0 and out["contract_skipped"] == 1


# 3. 상신 없이 DRAFT 바로 승인해도 결재선 룰이 있으면 다단계를 탄다
async def test_approve_without_submit_builds_chain(session):
    session.add_all([User(id=1, email="s@x.example", role="STAFF", password_hash="x"),
                     User(id=2, email="m@x.example", role="MANAGER", password_hash="x"),
                     User(id=3, email="d@x.example", role="DIRECTOR", password_hash="x")])
    session.add(ApprovalRule(doc_type="OUTSOURCING_CONTRACT", min_amount=0,
                             steps=[{"type": "ROLE", "value": "MANAGER"},
                                    {"type": "ROLE", "value": "DIRECTOR"}],
                             active=True, created_by=1))
    await session.flush()
    c = await _contract(session)
    assert c.status == "DRAFT"
    await oc.approve(session, actor=2, contract_id=c.id)        # 1단계만
    assert c.status == "PENDING_APPROVAL"
    await oc.approve(session, actor=3, contract_id=c.id)
    assert c.status == "APPROVED"


async def _po_received(session, sup, item, qty=100, price=5000, lot_no="L1"):
    po = await po_svc.request(session, supplier_id=sup.id, actor=1,
                              lines=[{"item_id": item.id, "qty": qty, "unit_price": price}])
    await po_svc.approve(session, po_id=po.id, actor=2)
    await po_svc.receive(session, po_id=po.id, actor=2, received_on=date(2026, 5, 1),
                         receipts=[{"item_id": item.id, "lot_no": lot_no,
                                    "expiry_on": date(2027, 5, 1), "qty": qty}])
    return po


# 4-a. 타 공급사 로트로 반품 차단
async def test_return_rejects_lot_from_other_supplier(session):
    a = Supplier(name="A약품", business_no="1018212345")
    b = Supplier(name="B약품", business_no="1018212346")
    item = Item(code="R1", name="수액", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add_all([a, b, item])
    await session.flush()
    po_a = await _po_received(session, a, item, lot_no="LA")
    await _po_received(session, b, item, lot_no="LB")
    lot_b = await session.scalar(select(Lot).where(Lot.lot_no == "LB"))
    with pytest.raises(ValidationError, match="다른 공급사"):
        await ret_svc.create_return(session, po_id=po_a.id, lot_id=lot_b.id, qty=10,
                                    reason="DEFECT", returned_on=date(2026, 5, 10), actor=2)


# 4-b. 라인 입고수량 초과 반품 차단(같은 공급사 다른 발주 로트의 잔량으로 부풀리기)
async def test_return_rejects_qty_over_line_received(session):
    a = Supplier(name="A약품", business_no="1018212345")
    item = Item(code="R2", name="거즈", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add_all([a, item])
    await session.flush()
    po_small = await _po_received(session, a, item, qty=10, lot_no="S1")
    await _po_received(session, a, item, qty=100, lot_no="B1")
    big_lot = await session.scalar(select(Lot).where(Lot.lot_no == "B1"))
    with pytest.raises(ValidationError, match="입고수량"):
        await ret_svc.create_return(session, po_id=po_small.id, lot_id=big_lot.id, qty=50,
                                    reason="DEFECT", returned_on=date(2026, 5, 10), actor=2)


async def _payable(session, amount=100_000):
    return await settle.record_payable(session, party="X상사", amount=amount,
                                       incurred_on=date(2026, 6, 1), due_on=date(2026, 6, 30))


# 5-a. 결의 중 미지급금 개별 지급 차단
async def test_direct_pay_blocked_while_in_open_batch(session):
    p = await _payable(session)
    await disb.create_batch(session, payable_ids=[p.id], scheduled_on=date(2026, 7, 10), actor=1)
    with pytest.raises(ValidationError, match="지급결의"):
        await disb.assert_not_in_open_batch(session, payable_id=p.id)


# 5-b. 미승인(DRAFT) 배치 이체파일 차단
async def test_export_csv_requires_approved(session):
    p = await _payable(session)
    b = await disb.create_batch(session, payable_ids=[p.id], scheduled_on=date(2026, 7, 10),
                                actor=1)
    with pytest.raises(ValidationError, match="승인된"):
        await disb.export_csv(session, batch_id=b.id)
    await disb.approve(session, batch_id=b.id, actor=2)
    assert "X상사" in await disb.export_csv(session, batch_id=b.id)


# 5-c. 라우터 지급 EP도 차단(HTTP)
async def test_pay_endpoint_blocked_for_batched_payable(session, monkeypatch):
    from erp.finance import router as fr
    p = await _payable(session)
    await disb.create_batch(session, payable_ids=[p.id], scheduled_on=date(2026, 7, 10), actor=1)

    class U:
        user_id = 9
    monkeypatch.setattr(fr, "_require", lambda *a, **k: None)
    with pytest.raises(ValidationError, match="지급결의"):
        await fr.pay_payable(p.id, amount=1000, on=date(2026, 7, 1), user=U(), session=session)
    assert float((await session.get(Payable, p.id)).paid_amount) == 0


# 6. 미지급 자산 취득이 AP 보조원장 행을 만들고 비용 이중계상 없음
async def test_asset_payable_acquisition_creates_payable_row(session):
    await AssetService().register(session, asset_no="EQ-1", name="초음파", category="MEDICAL",
                                  acquired_on=date(2026, 6, 1), acquisition_cost=30_000_000,
                                  useful_life_years=5, vendor="메디장비", create_payable=True)
    ap = await session.scalar(select(Payable).where(Payable.source_ref == "asset-acq-EQ-1"))
    assert ap is not None and float(ap.amount) == 30_000_000 and ap.party == "메디장비"
    # 전표는 취득 1건뿐(DR 1710 / CR 2110) — 비용 전표 없음
    n = await session.scalar(select(func.count()).select_from(Voucher)
                             .where(Voucher.source_module == "ap"))
    assert n == 0
    # AP로 지급 가능
    await settle.pay(session, payable_id=ap.id, amount=30_000_000, on=date(2026, 7, 1))
    assert ap.status == "PAID"


async def test_asset_cash_acquisition_no_payable(session):
    await AssetService().register(session, asset_no="EQ-2", name="모니터", category="MEDICAL",
                                  acquired_on=date(2026, 6, 1), acquisition_cost=1_000_000,
                                  useful_life_years=5, payment_method="CASH")
    assert await session.scalar(select(Payable).where(
        Payable.source_ref == "asset-acq-EQ-2")) is None


# 7. 같은 품목 중복 라인 발주 거부
async def test_po_request_rejects_duplicate_item_lines(session):
    a = Supplier(name="A약품", business_no="1018212345")
    item = Item(code="D1", name="주사기", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add_all([a, item])
    await session.flush()
    with pytest.raises(ValidationError, match="같은 품목"):
        await po_svc.request(session, supplier_id=a.id, actor=1,
                             lines=[{"item_id": item.id, "qty": 10, "unit_price": 100},
                                    {"item_id": item.id, "qty": 5, "unit_price": 100}])
