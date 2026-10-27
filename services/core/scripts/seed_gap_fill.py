"""prod 데이터 갭필(데모) — 빈/희소 테이블을 도메인 서비스 경유로 채움. 멱등·TEST- 식별·안전.

채우는 영역: 종별가산율·회계기간·차입금·리스(IFRS16)·부양가족·벤더평가·방사선(장비/선량)·마약류 수불.
각 섹션 독립 try/except — 한 섹션 실패가 전체를 막지 않음. 재실행 안전(존재검사/멱등키).
"""
import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, "/app/src")

from sqlalchemy import select, text  # noqa: E402

from erp.db import session_factory  # noqa: E402

_SYS = 1


async def _uid(session) -> int:
    r = await session.scalar(text("select id from common.users order by id limit 1"))
    return int(r or 1)


async def sec_surcharge(session):
    from erp.claims import rule_loader
    from erp.claims.rule_loader import SurchargeRateRow
    rows = [SurchargeRateRow(grade="CLINIC", rate=0.15), SurchargeRateRow(grade="HOSPITAL", rate=0.20),
            SurchargeRateRow(grade="GENERAL", rate=0.25), SurchargeRateRow(grade="SUPERIOR", rate=0.30)]
    n = await rule_loader.load_surcharge_rates(session, rows)
    return f"종별가산율 {n}건(grade upsert)"


async def sec_fiscal_periods(session):
    from erp.finance.models import FiscalPeriod
    made = 0
    for m in range(1, 13):
        ym = f"2026-{m:02d}"
        if await session.get(FiscalPeriod, ym) is None:
            session.add(FiscalPeriod(yyyymm=ym, fiscal_year=2026,
                                     status="CLOSED" if m == 1 else "OPEN"))
            made += 1
    await session.flush()
    return f"회계기간 {made}건(2026-01 CLOSED·이후 OPEN)"


async def sec_loans(session):
    from erp.finance.loan_service import LoanService
    from erp.finance.models import Loan
    svc = LoanService()
    specs = [("TEST-국민은행 운영자금", 500_000_000, 0.045, date(2026, 2, 16), 36, "LONG"),
             ("TEST-기업은행 장비구입", 200_000_000, 0.052, date(2026, 3, 2), 24, "SHORT")]
    made = intr = 0
    for lender, principal, rate, on, term, kind in specs:
        loan = await session.scalar(select(Loan).where(Loan.lender == lender))
        if loan is None:
            loan = await svc.register(session, lender=lender, principal=principal, annual_rate=rate,
                                      started_on=on, term_months=term, kind=kind, actor=_SYS)
            made += 1
        # 실행 다음 달부터 2026-07까지 월 이자 인식(이자비용5720/현금·멱등 loan-int-{id}-{ym})
        m0 = on.month + 1
        for mm in range(m0, 8):
            r = await svc.accrue_interest(session, loan_id=loan.id, yyyymm=f"2026-{mm:02d}", actor=_SYS)
            intr += 1 if r.get("posted") else 0
    return f"차입금 {made}건·월이자 {intr}기(실행+이자 전표)"


async def sec_leases(session):
    from erp.finance.lease_service import LeaseService
    from erp.finance.models import LeaseContract
    svc = LeaseService()
    specs = [("TEST-LS-001", "TEST-MRI 장비 리스", date(2026, 2, 1), 60, 8_000_000, 0.05, False),
             ("TEST-LS-002", "TEST-업무용 차량 리스", date(2026, 2, 1), 36, 1_200_000, 0.045, False),
             ("TEST-LS-003", "TEST-복합기(단기면제)", date(2026, 4, 1), 12, 150_000, 0.0, True)]
    made = accr = 0
    for no, name, on, term, pay, rate, exempt in specs:
        lease = await session.scalar(select(LeaseContract).where(LeaseContract.lease_no == no))
        if lease is None:
            lease = await svc.create(session, lease_no=no, asset_name=name, start_on=on,
                                     term_months=term, monthly_payment=pay, annual_rate=rate,
                                     exempt=exempt, user_id=_SYS)
            made += 1
        if not exempt:      # 월 상각·이자 몇 기 전기(lease_accruals 채움·멱등 source_ref)
            for m in range(2, 7):   # 2026-02~06
                try:
                    await svc.accrue_monthly(session, lease_id=lease.id, period=f"2026-{m:02d}",
                                             user_id=_SYS)
                    accr += 1
                except Exception:  # noqa: BLE001 — 이미 전기/기간초과 무시
                    pass
    return f"리스계약 {made}건·월상각 {accr}기(IFRS16 전표 자동)"


async def sec_dependents(session):
    from erp.hr.dependent_service import DependentService
    from erp.hr.models import Dependent, Employee
    svc = DependentService()
    emps = (await session.scalars(select(Employee).where(Employee.resigned_on.is_(None))
                                  .order_by(Employee.id).limit(30))).all()
    made = 0
    for e in emps:
        if await session.scalar(select(Dependent).where(Dependent.employee_id == e.id)) is not None:
            continue
        # 배우자 + 자녀 1(결정적)
        await svc.add(session, actor=_SYS, employee_id=e.id, name="TEST-배우자",
                      relation="SPOUSE", birth_date=date(1985, 6, 1))
        if e.id % 2 == 0:
            await svc.add(session, actor=_SYS, employee_id=e.id, name="TEST-자녀",
                          relation="CHILD", birth_date=date(2015, 3, 1))
        made += 1
    return f"부양가족 {made}명분(배우자+일부 자녀)"


async def sec_vendor_eval(session):
    from erp.scm.models import Supplier, VendorEvaluation
    from erp.scm.vendor_service import VendorService
    svc = VendorService()
    sups = (await session.scalars(select(Supplier).order_by(Supplier.id))).all()
    made = 0
    for i, s in enumerate(sups):
        for period in ("2026-Q1", "2026-Q2"):
            if await session.scalar(select(VendorEvaluation).where(
                    VendorEvaluation.supplier_id == s.id, VendorEvaluation.period == period)) is not None:
                continue
            base = 75 + (i * 3 + (0 if period.endswith("Q1") else 5)) % 15   # 75~89 → ±5 후 65~94
            await svc.evaluate(session, supplier_id=s.id, period=period,
                               delivery=base + 5, quality=base, price=base - 5,
                               note="TEST-분기평가", actor=_SYS)
            made += 1
    return f"벤더평가 {made}건(공급사×분기)"


async def sec_radiation(session):
    from erp.hr.models import Employee
    from erp.regulatory.models import RadiationEquipment
    from erp.regulatory.radiation_service import RadiationService
    svc = RadiationService()
    made_eq = made_dose = 0
    eqs = [("TEST-XR-01", "TEST-일반촬영 X선", date(2025, 8, 1), date(2026, 8, 1)),
           ("TEST-CT-01", "TEST-CT 스캐너", date(2026, 1, 15), date(2027, 1, 15)),
           ("TEST-MG-01", "TEST-유방촬영기", date(2025, 3, 1), date(2026, 3, 1))]
    for no, name, li, ni in eqs:
        if await session.scalar(select(RadiationEquipment).where(
                RadiationEquipment.equipment_no == no)) is None:
            session.add(RadiationEquipment(equipment_no=no, name=name,
                                           last_inspected_on=li, next_inspection_on=ni))
            made_eq += 1
    await session.flush()
    # 방사선 관계 종사자(기사) 일부 선량 기록
    techs = (await session.scalars(select(Employee).where(
        Employee.job_group == "TECH", Employee.resigned_on.is_(None)).limit(5))).all()
    for e in techs:
        for m in ("2026-04", "2026-05", "2026-06"):
            r = await svc.record_dose(session, employee_id=e.id, period=m,
                                      dose_msv=1.2 + (e.id % 3) * 0.4,
                                      measured_on=date(int(m[:4]), int(m[5:7]), 28),
                                      instrument="TEST-TLD", actor=_SYS)
            made_dose += 1
    return f"방사선 장비 {made_eq}대·선량기록 {made_dose}건"


async def sec_narcotics(session):
    from erp.scm.models import Item
    from erp.scm.narcotics_service import NarcoticsService
    svc = NarcoticsService()
    items = (await session.scalars(select(Item).where(Item.is_narcotic.is_(True))
                                   .order_by(Item.id))).all()
    made = 0
    for it in items:
        # 이미 수불 있으면 skip(멱등)
        cur = await session.scalar(text(
            "select count(*) from scm.narcotics_ledger where item_id = :i"), {"i": it.id})
        if cur and int(cur) > 0:
            continue
        # 입고 100 → 출고 30 → 출고 20 (시계열 단조)
        await svc.add_entry(session, item_id=it.id, entry_type="IN", qty=100, handler_id=_SYS,
                            entry_on=date(2026, 6, 2))
        await svc.add_entry(session, item_id=it.id, entry_type="OUT", qty=30, handler_id=_SYS,
                            entry_on=date(2026, 6, 15))
        await svc.add_entry(session, item_id=it.id, entry_type="OUT", qty=20, handler_id=_SYS,
                            entry_on=date(2026, 7, 1))
        made += 1
    return f"마약류 수불 {made}품목(입고·출고 시계열)"


SECTIONS = [sec_surcharge, sec_fiscal_periods, sec_loans, sec_leases, sec_dependents,
            sec_vendor_eval, sec_radiation, sec_narcotics]


async def main(commit: bool) -> None:
    async with session_factory()() as session:
        for fn in SECTIONS:
            try:
                async with session.begin_nested():   # 섹션 savepoint — 실패해도 이전 섹션 보존
                    msg = await fn(session)
                print(f"  [OK] {fn.__name__}: {msg}")
            except Exception as e:  # noqa: BLE001 — savepoint 자동 롤백, 외부 txn 유지
                print(f"  [SKIP/ERR] {fn.__name__}: {type(e).__name__}: {e}")
        if commit:
            await session.commit()
            print("== COMMIT 완료 ==")
        else:
            print("== DRY-RUN(미커밋) — --commit 로 반영 ==")


if __name__ == "__main__":
    asyncio.run(main("--commit" in sys.argv))
