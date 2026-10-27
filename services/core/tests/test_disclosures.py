"""재무제표 주석 (K-IFRS 1001) — 유형자산·충당부채 증감 자동 산출 + 정성주석 저장."""
from datetime import date

from erp.finance.disclosure_service import DisclosureService
from erp.finance.models import Account
from erp.finance.service import VoucherLineDTO, VoucherService

svc = DisclosureService()
vs = VoucherService()


async def _seed(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="1710", name="의료장비", kind="ASSET"),
        Account(code="1810", name="감가상각누계", kind="ASSET"),
        Account(code="5210", name="감가상각비", kind="EXPENSE"),
        Account(code="2120", name="퇴직급여충당부채", kind="LIABILITY"),
        Account(code="5320", name="퇴직급여", kind="EXPENSE"),
    ])
    await session.flush()


async def test_ppe_and_provision_movement(session):
    await _seed(session)
    # 장비 취득 10,000,000
    await vs.create(session, source_module="manual", source_ref="acq", voucher_on=date(2026, 2, 1),
                    lines=[VoucherLineDTO("1710", "DEBIT", 10000000),
                           VoucherLineDTO("1110", "CREDIT", 10000000)])
    # 감가 1,000,000 (감가비/감가누계)
    await vs.create(session, source_module="manual", source_ref="dep", voucher_on=date(2026, 6, 30),
                    lines=[VoucherLineDTO("5210", "DEBIT", 1000000),
                           VoucherLineDTO("1810", "CREDIT", 1000000)])
    # 퇴직급여충당 전입 3,000,000
    await vs.create(session, source_module="manual", source_ref="prov", voucher_on=date(2026, 6, 30),
                    lines=[VoucherLineDTO("5320", "DEBIT", 3000000),
                           VoucherLineDTO("2120", "CREDIT", 3000000)])
    notes = await svc.notes(session, year=2026)
    ppe = next(n for n in notes["auto_notes"] if n["note_key"] == "ppe_movement")["payload"]
    assert ppe["cost"]["acquisition"] == 10000000
    assert ppe["accum_dep"]["charge"] == 1000000
    assert ppe["book_value"]["closing"] == 9000000     # 1000만 − 100만
    prov = next(n for n in notes["auto_notes"] if n["note_key"] == "provision_movement")["payload"]
    db = next(x for x in prov["items"] if x["account_code"] == "2120")
    assert db["charge"] == 3000000 and db["closing"] == 3000000


async def test_save_qualitative_note(session):
    await _seed(session)
    r = await svc.save(session, year=2026, note_key="accounting_policy",
                       title="주요 회계정책", payload={"text": "K-IFRS 적용..."})
    assert r["note_key"] == "accounting_policy"
    notes = await svc.notes(session, year=2026)
    assert any(n["note_key"] == "accounting_policy" for n in notes["notes"])
