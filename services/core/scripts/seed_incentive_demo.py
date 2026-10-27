"""진료수익(청구 담당의) → 성과급(register→review→approve·GL) 데모 시드. 멱등·TEST-.

2026-07 ACTIVE 의료진 계약 의사별로 청구명세(doctor_id) 생성 → 순수입 → 성과급 산정·승인(전표 5310/2110).
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
_PERIOD = "2026-07"
_RECEIPT = "TEST-INC-2026-07"


async def _active_doctors(session):
    rows = (await session.execute(text(
        "select employee_id from hr.medical_contracts "
        "where status='ACTIVE' and incentive_rate>0 order by employee_id"))).scalars().all()
    return list(rows)


async def sec_revenue(session):
    from erp.claims.models import ClaimBatch, ClaimStatement
    docs = await _active_doctors(session)
    batch = await session.scalar(select(ClaimBatch).where(ClaimBatch.receipt_no == _RECEIPT))
    if batch is None:
        batch = ClaimBatch(receipt_no=_RECEIPT, care_month=_PERIOD,
                           notice_arrived_on=date(2026, 7, 10),
                           claimed_amt=0, decided_amt=0)
        session.add(batch)
        await session.flush()
    have = {d for (d,) in (await session.execute(
        select(ClaimStatement.doctor_id).where(ClaimStatement.batch_id == batch.id))).all()}
    made = 0
    for i, emp_id in enumerate(docs):
        if emp_id in have:
            continue
        claimed = 40_000_000 + (emp_id % 7) * 5_000_000     # 40~70M 결정적
        adjusted = round(claimed * 0.05)                    # 삭감 5%
        session.add(ClaimStatement(
            batch_id=batch.id, stmt_serial=f"INC-{emp_id:04d}", doctor_id=emp_id,
            dept_code="IM", claimed_amt=claimed, adjusted_amt=adjusted,
            decided_amt=claimed - adjusted))
        made += 1
    await session.flush()
    return f"청구명세(담당의 진료수익) {made}건 / 의사 {len(docs)}명"


async def sec_incentive(session):
    from erp.hr.incentive_service import IncentiveService
    from erp.hr.models import IncentiveCalculation
    svc = IncentiveService()
    docs = await _active_doctors(session)
    approved = total = 0
    for emp_id in docs:
        ic = await session.scalar(select(IncentiveCalculation).where(
            IncentiveCalculation.employee_id == emp_id, IncentiveCalculation.period == _PERIOD))
        if ic is not None and ic.status == "APPROVED":
            continue
        ic = await svc.register(session, actor=_SYS, employee_id=emp_id, period=_PERIOD)
        if float(ic.amount) <= 0:
            continue
        await svc.review(session, actor=_SYS, calc_id=ic.id)
        ic = await svc.approve(session, actor=_SYS, calc_id=ic.id)
        approved += 1
        total += int(ic.amount)
    return f"성과급 승인 {approved}건·합계 {total:,}원(전표 급여비용5310/미지급2110)"


SECTIONS = [sec_revenue, sec_incentive]


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
