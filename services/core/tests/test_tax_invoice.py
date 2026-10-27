"""전자세금계산서 수취·대사 (A-1) — 등록 멱등·검증·CSV·대사·집계."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.finance.models import Payable
from erp.finance.tax_invoice_service import TaxInvoiceService

svc = TaxInvoiceService()


async def _register(session, no="20260611-0001", total=None, **kw):
    defaults = dict(approval_no=no, issue_date=date(2026, 6, 1),
                    supplier_bizno="123-45-67890", supplier_name="메디서플",
                    supply_amount=1_000_000, vat_amount=100_000)
    defaults.update(kw)
    return await svc.register(session, actor=1, total_amount=total, **defaults)


async def test_register_idempotent_and_validation(session):
    inv = await _register(session)
    dup = await _register(session)                          # 승인번호 멱등
    assert dup.id == inv.id
    assert float(inv.total_amount) == 1_100_000

    with pytest.raises(ValidationError, match="세액이 0"):  # 면세 계산서에 세액
        await _register(session, no="X-1", invoice_type="EXEMPT", vat_amount=10)
    with pytest.raises(ValidationError, match="합계"):      # 합계 ≠ 공급+세액
        await _register(session, no="X-2", total=999_999)


async def test_match_amount_validation(session):
    inv = await _register(session, no="M-1")
    ap_ok = Payable(party="메디서플", amount=1_100_000, incurred_on=date(2026, 6, 1),
                    due_on=date(2026, 7, 1))
    ap_bad = Payable(party="기타", amount=500_000, incurred_on=date(2026, 6, 1),
                     due_on=date(2026, 7, 1))
    session.add_all([ap_ok, ap_bad])
    await session.flush()

    with pytest.raises(ValidationError, match="금액 불일치"):
        await svc.match(session, actor=1, invoice_id=inv.id, payable_id=ap_bad.id)
    m = await svc.match(session, actor=1, invoice_id=inv.id, payable_id=ap_ok.id)
    assert m.status == "MATCHED" and m.payable_id == ap_ok.id
    with pytest.raises(ValidationError, match="이미 대사"):
        await svc.match(session, actor=1, invoice_id=inv.id, payable_id=ap_ok.id)

    await svc.unmatch(session, actor=1, invoice_id=inv.id)
    assert (await svc.list(session, status="RECEIVED"))["items"][0]["id"] == inv.id


async def test_csv_upload_row_isolation(session):
    csv_text = ("승인번호,작성일자,공급자사업자등록번호,공급자상호,공급가액,세액,합계금액,종류\n"
                "C-1,2026-06-02,111-11-11111,약품상사,500000,50000,550000,세금계산서\n"
                "C-2,2026-06-03,222-22-22222,면세유통,300000,0,300000,계산서\n"
                "C-1,2026-06-02,111-11-11111,약품상사,500000,50000,550000,세금계산서\n"
                ",2026-06-04,333-33-33333,불량행,100000,10000,110000,세금계산서\n")
    out = await svc.upload_csv(session, actor=1, content=csv_text.encode("utf-8"))
    assert out["created"] == 2 and out["duplicated"] == 1 and out["failed"] == 1
    items = {i["approval_no"]: i for i in (await svc.list(session))["items"]}
    assert items["C-2"]["invoice_type"] == "EXEMPT"          # '계산서' → 면세 분류


async def test_summary_by_type_and_unmatched(session):
    await _register(session, no="S-1", issue_date=date(2026, 3, 5))
    await _register(session, no="S-2", issue_date=date(2026, 3, 20),
                    invoice_type="EXEMPT", vat_amount=0, supply_amount=200_000)
    s = await svc.summary(session, year=2026)
    by_key = {(m["month"], m["invoice_type"]): m for m in s["months"]}
    assert by_key[("2026-03", "TAX")]["vat_amount"] == 100_000
    assert by_key[("2026-03", "EXEMPT")]["supply_amount"] == 200_000
    assert s["unmatched"]["count"] == 2
