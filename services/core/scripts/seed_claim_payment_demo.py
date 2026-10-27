"""공단 지급대사 데모 — 청구제출(1140 인식)→공단지급(1140 소멸) 생애주기. 멱등·TEST-.

2026-08: ClaimSubmission 10건 + 미수금(1140/외래수익4110) 인식 → 공단 실지급(1110/1140) 92%
→ 잔여 미수 8% + variance 표면화. reconciliation·refresh_status로 대사 상태 갱신.
"""
import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, "/app/src")

from sqlalchemy import func, select  # noqa: E402

from erp.db import session_factory  # noqa: E402

_SYS = 1
_CM = "2026-08"


async def sec_submissions(session):
    from erp.claims.models import ClaimSubmission
    from erp.finance.service import VoucherLineDTO, VoucherService
    vs = VoucherService()
    made = total = 0
    for i in range(1, 11):
        cid = f"TEST-CLM-2026-08-{i:03d}"
        if await session.scalar(select(ClaimSubmission).where(ClaimSubmission.claim_id == cid)) is not None:
            continue
        amt = 3_000_000 + i * 500_000            # 3.5~8M
        await vs.create(session, source_module="his.claim", source_ref=f"claim:submit:{cid}",
                        voucher_on=date(2026, 8, 10),
                        lines=[VoucherLineDTO("1140", "DEBIT", amt),
                               VoucherLineDTO("4110", "CREDIT", amt)],
                        memo=f"보험청구 제출 {cid}", created_by=_SYS)
        session.add(ClaimSubmission(claim_id=cid, claim_number=cid, care_month=_CM,
                                    claimed_amt=amt, submitted_on=date(2026, 8, 5)))
        made += 1
        total += amt
    return f"청구제출 {made}건·인식 보험미수금(1140) {total:,}원"


async def sec_payment(session):
    from erp.claims.models import ClaimSubmission
    from erp.claims.service import ClaimPaymentService, ClaimSubmissionService
    recognized = float((await session.execute(
        select(func.coalesce(func.sum(ClaimSubmission.claimed_amt), 0))
        .where(ClaimSubmission.care_month == _CM))).scalar() or 0)
    paid = round(recognized * 0.92)              # 92% 지급 → 잔여 미수 8%
    r = await ClaimPaymentService().record(
        session, actor=_SYS, care_month=_CM, payment_ref="TEST-PAY-2026-08",
        paid_on=date(2026, 9, 15), paid_amt=paid, note="TEST-공단 지급")
    await ClaimPaymentService().refresh_status(session, _CM)
    recon = await ClaimSubmissionService().reconciliation(session, care_month=_CM)
    return (f"공단지급 {paid:,}원(인식AR {recognized:,.0f}의 92%)·잔여미수 {recognized-paid:,.0f}"
            f"·대사 variance {recon.get('variance')}")


SECTIONS = [sec_submissions, sec_payment]


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
