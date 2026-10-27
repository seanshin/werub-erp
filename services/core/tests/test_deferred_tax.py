"""이연법인세 (K-IFRS 1012) — 일시적차이×세율 DTA/DTL·차액 결산분개·멱등."""
from datetime import date

from erp.finance.deferred_tax_service import DeferredTaxService
from erp.finance.models import Account, TaxRateSetting
from erp.finance.service import VoucherLineDTO, VoucherService

svc = DeferredTaxService()
vs = VoucherService()


async def _seed(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="2120", name="퇴직급여충당부채", kind="LIABILITY"),
        Account(code="5320", name="퇴직급여", kind="EXPENSE"),
        Account(code="3400", name="재평가잉여금", kind="CAPITAL"),
        Account(code="1710", name="의료장비", kind="ASSET"),
        Account(code="1180", name="이연법인세자산", kind="ASSET"),
        Account(code="2260", name="이연법인세부채", kind="LIABILITY"),
        Account(code="5810", name="법인세비용", kind="EXPENSE"),
        TaxRateSetting(rate=0.20, valid_from=date(2025, 1, 1)),
    ])
    await session.flush()


async def test_preview_dta_dtl(session):
    await _seed(session)
    # 퇴직급여충당부채 1,000,000(대변) → 차감할 일시적차이 → DTA (P/L-origin)
    await vs.create(session, source_module="hr", source_ref="prov1", voucher_on=date(2026, 6, 30),
                    lines=[VoucherLineDTO("5320", "DEBIT", 1000000),
                           VoucherLineDTO("2120", "CREDIT", 1000000)])
    # 재평가잉여금(3400)은 OCI-origin → 이연법인세 엔진에서 제외(backwards tracing 필요)
    await vs.create(session, source_module="manual", source_ref="reval1", voucher_on=date(2026, 6, 30),
                    lines=[VoucherLineDTO("1710", "DEBIT", 500000),
                           VoucherLineDTO("3400", "CREDIT", 500000)])
    p = await svc.preview(session, year=2026)
    assert p["rate"] == 0.20
    assert p["dta"] == 200000          # 퇴직충당 100만×20%
    assert p["dtl"] == 0               # 재평가잉여금은 제외(OCI-origin)
    assert p["delta_dta"] == 200000


async def test_post_idempotent(session):
    await _seed(session)
    await vs.create(session, source_module="hr", source_ref="prov2", voucher_on=date(2026, 3, 31),
                    lines=[VoucherLineDTO("5320", "DEBIT", 2000000),
                           VoucherLineDTO("2120", "CREDIT", 2000000)])
    r1 = await svc.post(session, year=2026)
    assert r1["posted"] and r1["dta"] == 400000   # 200만×20%
    # 전기 후 재실행 — DTA 반영되어 차액 0(멱등)
    r2 = await svc.post(session, year=2026)
    assert r2["posted"] is False
    dta_bal = -(await svc._cr_balance(session, as_of=date(2026, 12, 31), code="1180"))
    assert dta_bal == 400000
