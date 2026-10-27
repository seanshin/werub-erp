"""발생주의 전환 영향 시뮬레이션 — 결정 `patient-accrual-cutover` 판단 자료 (읽기 전용).

무엇을 재는가:
  ① 개통전기(`open_cutover`) 규모 — cutover **이전** 진료월의 미수납분(DR1131/CR4110)
  ② 전환 후 월별 발생 인식 규모 — cutover **이후** 각 월의 `patient_share`
  ③ 재무제표 영향 — 1131(자산)·4110(수익) 증가분과 현행 대비 차이
  ④ ⚠ **운영 제약**: 개통전기 일자는 `cutover 시작 −1일`이다. 그 날짜가 속한 회계기간이
     CLOSED면 전기가 거부된다(`open_cutover`는 allow_closed를 쓰지 않는다).
     → 선택 가능한 cutover는 "직전 달이 OPEN인 달"뿐이다.

**부작용 없음**: 전기를 만들지 않고 계산만 한다. 세션은 마지막에 롤백한다.
게이트(`patient_accrual_enabled`)는 건드리지 않는다 — 이 스크립트로는 전환이 일어나지 않는다.

실행:
  docker cp .../simulate_accrual_cutover.py infra-core-1:/tmp/
  docker exec -w /app -e PYTHONPATH=/app/src infra-core-1 python /tmp/simulate_accrual_cutover.py
"""
import asyncio
from datetime import date, timedelta

from sqlalchemy import select

from erp.db import session_factory
from erp.finance.models import FiscalPeriod
from erp.patient.billing_service import BillingService
from erp.patient.models import BillingLine
from erp.patient.receivable_service import PatientReceivableService as ReceivableService


def _month_start(ym: str) -> date:
    return date(int(ym[:4]), int(ym[5:7]), 1)


async def _chart_months(s) -> dict[str, list[tuple[str, str]]]:
    """진료월 → [(chart, care_month)] — 취소 라인 제외."""
    rows = (await s.execute(select(BillingLine.chart_no, BillingLine.treated_on).where(
        BillingLine.cancelled.is_(False)))).all()
    by_month: dict[str, set] = {}
    for chart_no, treated_on in rows:
        if chart_no and treated_on:
            cm = treated_on.strftime("%Y-%m")
            by_month.setdefault(cm, set()).add((chart_no, cm))
    return {m: sorted(v) for m, v in sorted(by_month.items())}


async def _month_totals(s, pairs) -> tuple[int, int, int]:
    """(본인부담 합, 수납 합, 미수 합) — accrue/open_cutover와 동일한 산식."""
    svc = ReceivableService()
    target_sum = collected_sum = 0
    for chart_no, cm in pairs:
        inv = await BillingService().invoice(s, chart_no=chart_no, care_month=cm)
        target = round(float(inv.get("patient_share") or 0)) if inv.get("available") else 0
        if target <= 0:
            continue
        collected = round(svc._clamp(  # noqa: SLF001 — 서비스와 동일 산식을 그대로 써야 수치가 일치
            await svc._collected(s, chart_no=chart_no, care_month=cm), target))
        target_sum += target
        collected_sum += collected
    return target_sum, collected_sum, target_sum - collected_sum


async def main() -> None:
    async with session_factory()() as s:
        periods = dict((await s.execute(select(FiscalPeriod.yyyymm, FiscalPeriod.status))).all())
        by_month = await _chart_months(s)
        if not by_month:
            print("진료 라인이 없습니다 — 시뮬레이션 대상 없음")
            return

        print("■ 진료월별 본인부담 / 수납 / 미수")
        print(f"  {'진료월':<9}{'환자':>5}{'본인부담':>14}{'수납':>14}{'미수':>14}")
        totals = {}
        for cm, pairs in by_month.items():
            t, c, o = await _month_totals(s, pairs)
            totals[cm] = (t, c, o)
            print(f"  {cm:<9}{len(pairs):>5}{t:>14,}{c:>14,}{o:>14,}")

        months = list(by_month)
        print("\n■ cutover 후보별 영향")
        print(f"  {'cutover':<9}{'개통전기일':<12}{'기간':<8}{'개통(1131/4110)':>18}{'이후 월발생':>14}")
        for cm in months + [(lambda m: f"{int(m[:4]) + (m[5:7] == '12')}-"
                            f"{(int(m[5:7]) % 12) + 1:02d}")(months[-1])]:
            open_on = _month_start(cm) - timedelta(days=1)
            pstat = periods.get(open_on.strftime("%Y-%m"), "미정의")
            prior = sum(totals[m][2] for m in totals if m < cm)      # 이전 진료월 미수 합
            after = sum(totals[m][0] for m in totals if m >= cm)     # 이후 진료월 본인부담 합
            mark = "✅" if pstat == "OPEN" else "❌ 전기 거부"
            print(f"  {cm:<9}{str(open_on):<12}{pstat:<8}{prior:>18,}{after:>14,}  {mark}")

        print("\n■ 현행(현금주의) 대비 재무제표 영향")
        grand_out = sum(v[2] for v in totals.values())
        grand_tar = sum(v[0] for v in totals.values())
        print("  · 현재 GL 1131 잔액: 0 (발생주의 OFF)")
        print(f"  · 전환 시 인식될 총 본인부담: {grand_tar:,}원")
        print(f"  · 그중 미수(1131로 남는 금액): {grand_out:,}원")
        print("  · 수익(4110) 증가: 개통분 + 이후 월 발생분 (cutover 위치에 따라 배분)")
        print("  ※ cutover 이전 수납분은 현금주의로 이미 수익 인식돼 **재인식하지 않는다**"
              "(이중계상 방지). 개통전기는 **미수납분만** 대상.")

        await s.rollback()      # 부작용 없음 — 조회만 했으나 명시적으로 롤백


if __name__ == "__main__":
    asyncio.run(main())
