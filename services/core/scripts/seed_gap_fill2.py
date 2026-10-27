"""prod 데이터 갭필 2차(데모) — 자기완결 도메인. 멱등·TEST-·섹션별 savepoint.

세금계산서·조달(RFQ→견적→낙찰/단가계약)·의약품회수·의료폐기물·환자환불·결재선규칙·환자안전사고·IT예산.
"""
import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, "/app/src")

from sqlalchemy import select  # noqa: E402

from erp.db import session_factory  # noqa: E402

_SYS = 1


async def sec_tax_invoices(session):
    from erp.finance.models import TaxInvoice
    from erp.finance.tax_invoice_service import TaxInvoiceService
    svc = TaxInvoiceService()
    specs = [("TEST-TXI-001", date(2026, 5, 10), "1018212345", 30_000_000, 3_000_000, "TEST-대한의료기기"),
             ("TEST-TXI-002", date(2026, 6, 5), "2208312456", 8_000_000, 800_000, "TEST-메디칼소모품"),
             ("TEST-TXI-003", date(2026, 6, 20), "1138599012", 1_500_000, 150_000, "TEST-청소용역")]
    made = 0
    for appr, dt, bizno, supply, vat, name in specs:
        if await session.scalar(select(TaxInvoice).where(TaxInvoice.approval_no == appr)) is None:
            await svc.register(session, actor=_SYS, approval_no=appr, issue_date=dt,
                               supplier_bizno=bizno, supply_amount=supply, vat_amount=vat,
                               supplier_name=name, direction="IN")
            made += 1
    return f"전자세금계산서(매입) {made}건"


async def sec_procurement(session):
    from erp.scm.models import Item, PriceAgreement, Supplier
    from erp.scm.rfq_service import RfqService
    svc = RfqService()
    if await session.scalar(select(PriceAgreement).where(
            PriceAgreement.title.like("TEST-%"))) is not None:
        return "조달 체인 이미 존재(skip)"
    items = (await session.scalars(select(Item).order_by(Item.id).limit(2))).all()
    sup = await session.scalar(select(Supplier).where(Supplier.is_active.is_(True)).order_by(Supplier.id))
    rfq = await svc.create_rfq(session, actor=_SYS, title="TEST-소모품 견적요청",
                               lines=[{"item_id": it.id, "qty": 100} for it in items],
                               due_on=date(2026, 7, 20))
    await svc.open_rfq(session, actor=_SYS, rfq_id=rfq.id)
    q = await svc.submit_quote(session, actor=_SYS, rfq_id=rfq.id, supplier_id=sup.id,
                               lines=[{"item_id": it.id, "unit_price": 4500 + it.id} for it in items],
                               submitted_on=date(2026, 7, 15))
    await svc.award(session, actor=_SYS, rfq_id=rfq.id, quote_id=q.id,
                    valid_from=date(2026, 7, 16), valid_to=date(2026, 12, 31),
                    title="TEST-단가계약 2026H2")
    return "조달 체인 1건(RFQ→견적→낙찰·단가계약)"


async def sec_recall_waste(session):
    from erp.scm.models import DrugRecall, Item, MedicalWasteLog
    from erp.scm.safety_service import DrugRecallService, MedicalWasteService
    made_r = made_w = 0
    it = await session.scalar(select(Item).where(Item.is_narcotic.is_(False)).order_by(Item.id))
    if it and await session.scalar(select(DrugRecall).where(DrugRecall.item_id == it.id)) is None:
        await DrugRecallService().register(session, item_id=it.id, reason="TEST-제조사 자진회수(이물)",
                                           recall_on=date(2026, 6, 10), lot_no="TEST-LOT-R1")
        made_r += 1
    waste = [("PATHOLOGICAL", 12.5, date(2026, 6, 3)), ("BLOOD", 8.0, date(2026, 6, 17)),
             ("GENERAL_MEDICAL", 45.0, date(2026, 7, 1))]
    for wt, kg, on in waste:
        ex = await session.scalar(select(MedicalWasteLog).where(
            MedicalWasteLog.waste_type == wt, MedicalWasteLog.stored_on == on))
        if ex is None:
            await MedicalWasteService().record(session, waste_type=wt, weight_kg=kg,
                                               stored_on=on, actor=_SYS)
            made_w += 1
    return f"의약품회수 {made_r}건·의료폐기물 {made_w}건"


async def sec_patient_refund(session):
    from erp.patient.models import Refund
    from erp.patient.service import RefundService
    if await session.scalar(select(Refund).where(Refund.reason.like("TEST-%"))) is not None:
        return "환불 이미 존재(skip)"
    await RefundService().refund(session, chart_no="TEST-00000", amount=5000,
                                  reason="TEST-과오납 환불", refunded_on=date(2026, 7, 5),
                                  reception_id=1, actor=_SYS)
    return "환자 환불 1건"


async def sec_approval_rules(session):
    from erp.common.models import ApprovalRule
    specs = [("PO", None, 10_000_000, None,
              [{"type": "DEPT_HEAD"}, {"type": "ROLE", "value": "DIRECTOR"}]),
             ("OUTSOURCING", None, 0, None,
              [{"type": "ROLE", "value": "MANAGER"}, {"type": "ROLE", "value": "DIRECTOR"}]),
             ("ITAM_DISPOSAL", None, 1_000_000, None, [{"type": "ROLE", "value": "IT_MANAGER"}])]
    made = 0
    for dt, cat, mn, mx, steps in specs:
        if await session.scalar(select(ApprovalRule).where(
                ApprovalRule.doc_type == dt, ApprovalRule.min_amount == mn)) is None:
            session.add(ApprovalRule(doc_type=dt, category=cat, min_amount=mn, max_amount=mx,
                                     steps=steps, active=True))
            made += 1
    await session.flush()
    return f"결재선 규칙 {made}건(PO/위수탁/ITAM폐기)"


async def sec_safety_incidents(session):
    from erp.regulatory.models import PatientSafetyIncident
    from erp.regulatory.safety_service import PatientSafetyService
    svc = PatientSafetyService()
    # incident_type: NEAR_MISS(근접오류)·ADVERSE(위해사건)·SENTINEL(적신호). category=낙상/투약 등.
    specs = [(date(2026, 5, 20), "ADVERSE", "FALL", "TEST-병동 낙상(경상)", "MILD", 1),
             (date(2026, 6, 12), "NEAR_MISS", "MEDICATION", "TEST-투약오류 사전차단(무해)", "NONE", 2)]
    made = 0
    for on, typ, cat, desc, sev, dept in specs:
        if await session.scalar(select(PatientSafetyIncident).where(
                PatientSafetyIncident.description == desc)) is None:
            await svc.report(session, occurred_on=on, incident_type=typ, category=cat,
                             description=desc, severity=sev, department_id=dept, reporter_id=_SYS)
            made += 1
    return f"환자안전사고 {made}건"


async def sec_it_budgets(session):
    from erp.itam.cost_service import CostService
    from erp.itam.models import ITBudget
    svc = CostService()
    specs = [("2026-07", None, 5_000_000), ("2026-07", "8", 2_000_000), ("2026-08", None, 5_000_000)]
    made = 0
    for period, cc, amt in specs:
        exists = await session.scalar(select(ITBudget).where(
            ITBudget.period == period,
            ITBudget.cost_center == cc if cc is not None else ITBudget.cost_center.is_(None)))
        if exists is None:
            await svc.upsert_budget(session, actor=_SYS, period=period, cost_center=cc,
                                    amount_krw=amt, note="TEST-IT예산")
            made += 1
    return f"IT 예산 {made}건"


SECTIONS = [sec_tax_invoices, sec_procurement, sec_recall_waste, sec_patient_refund,
            sec_approval_rules, sec_safety_incidents, sec_it_budgets]


async def main(commit: bool) -> None:
    async with session_factory()() as session:
        for fn in SECTIONS:
            try:
                async with session.begin_nested():
                    msg = await fn(session)
                print(f"  [OK] {fn.__name__}: {msg}")
            except Exception as e:  # noqa: BLE001
                print(f"  [SKIP/ERR] {fn.__name__}: {type(e).__name__}: {e}")
        if commit:
            await session.commit()
            print("== COMMIT 완료 ==")
        else:
            print("== DRY-RUN(미커밋) ==")


if __name__ == "__main__":
    asyncio.run(main("--commit" in sys.argv))
