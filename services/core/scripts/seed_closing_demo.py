"""연말결산 + 월별 기간마감(closing_snapshots) 데모. 멱등.

close_year(2025): 손익→이익잉여금 결산분개(closing:2025). 월별 close(): 재무제표 스냅샷 4종+CLOSED.
대상 = 2025 전월 + 2026-01~06(과거 완결월). 2026-07(성과급)·08(지급대사)은 OPEN 유지(최근 활동).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, "/app/src")

from erp.db import session_factory  # noqa: E402

_SYS = 1


async def sec_close_year(session):
    from erp.finance.closing_service import ClosingService
    out = await ClosingService().close_year(session, fiscal_year=2025, user_id=_SYS)
    if out.get("already_closed"):
        return f"2025 연말결산 이미 완료(voucher {out['voucher_id']})"
    return (f"2025 연말결산: 수익 {out.get('total_revenue',0):,.0f}·비용 {out.get('total_expense',0):,.0f}"
            f"·순이익 {out.get('net_income',0):,.0f}→이익잉여금(3300)")


async def sec_close_periods(session):
    from erp.finance.models import FiscalPeriod
    from erp.finance.period_service import PeriodService
    svc = PeriodService()
    months = [f"2025-{m:02d}" for m in range(1, 13)] + [f"2026-{m:02d}" for m in range(1, 7)]
    closed = resnap = 0
    for ym in months:
        p = await session.get(FiscalPeriod, ym)
        if p is not None and p.status == "CLOSED":
            # 스냅샷 없이 CLOSED된 기간(1차 갭필의 2026-01 등) → 재오픈 후 정식 마감해 스냅샷 생성
            has_snap = await session.scalar(
                __import__("sqlalchemy").text(
                    "select count(*) from finance.closing_snapshots where yyyymm=:y"), {"y": ym})
            if has_snap and int(has_snap) > 0:
                continue
            await svc.reopen(session, yyyymm=ym, user_id=_SYS, note="TEST-스냅샷 재생성")
            resnap += 1
        await svc.close(session, yyyymm=ym, user_id=_SYS, note="TEST-월마감", enforce=False)
        closed += 1
    return f"기간마감 {closed}개월(재오픈-재마감 {resnap})·스냅샷 4종/월 생성"


SECTIONS = [sec_close_year, sec_close_periods]


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
