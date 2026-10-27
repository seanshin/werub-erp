"""급여 기초정보·급여 데이터 시드 (테스트용) — 재직 직원 전원에 보수 프로필 + 월 급여 생성.

HIS 동기화로 생성된 직원(보수 프로필 없음)에 직군별 기본급·부양가족·계좌를 채우고(온보딩 완료),
지정 월 급여를 build_payroll로 생성·확정(GL 전기 포함)한다. **테스트 합성 데이터** — 계좌는
TEST- 마커라 식별·제거 가능. 멱등(재실행 안전: 보수 upsert·급여 라인 교체·전표 source_ref 멱등).

사용: python scripts/seed_payroll_demo.py [YYYY-MM] [--no-confirm]
  YYYY-MM 미지정 시 2026-07. --no-confirm 시 급여 라인만(전표 미전기).
대상: DATABASE_URL — 테스트/파일럿 DB에서만 실행 권장.
"""
import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import select  # noqa: E402

from erp.db import session_factory  # noqa: E402
from erp.hr import payroll_rules  # noqa: E402
from erp.hr.compensation_service import CompensationService  # noqa: E402
from erp.hr.employee_service import PayrollService  # noqa: E402
from erp.hr.models import Employee  # noqa: E402
from erp.hr.pay_rates import load_rates  # noqa: E402

_SYSTEM = 1   # 시드 actor
_BASE_BY_JOB = {"DOCTOR": 8_000_000, "RESIDENT": 4_500_000, "NURSE": 3_500_000,
                "TECH": 3_200_000, "STAFF": 2_800_000}


def _inputs(emp: Employee) -> dict:
    """직군별 근태 변수(테스트) — 당직·야간·연장 일부 부여해 계산근거 노출."""
    jc = emp.job_category or emp.job_group
    return {"duty_count": 2 if jc in ("DOCTOR", "RESIDENT") else 0,
            "night_hours": 20 if jc == "NURSE" else 0,
            "ot_hours": 5}


async def main(yyyymm: str, confirm: bool) -> None:
    comp, pay = CompensationService(), PayrollService()
    async with session_factory()() as session:
        emps = (await session.scalars(
            select(Employee).where(Employee.resigned_on.is_(None))
            .order_by(Employee.employee_no))).all()
        rates = await load_rates(session)
        n_comp = n_pay = 0
        for emp in emps:
            base = _BASE_BY_JOB.get(emp.job_group, 2_800_000)
            dependents = 1 + (emp.id % 3)                       # 1~3 결정적
            existing = await comp.get(session, employee_id=emp.id)
            if existing is None or float(existing.base_pay) <= 0 or existing.bank_account_enc is None:
                await comp.upsert(session, actor=_SYSTEM, employee_id=emp.id, base_pay=base,
                                  dependents=dependents, bank_name="테스트은행",
                                  bank_account=f"TEST-{emp.employee_no}")
                n_comp += 1
            r = payroll_rules.build_payroll(
                staff_class=emp.staff_class, job_category=emp.job_category or emp.job_group,
                employment_type=emp.employment_type, base_pay=base, dependents=dependents,
                year=int(yyyymm[:4]), month=int(yyyymm.replace("-", "")[4:6]), rates=rates,
                **_inputs(emp))
            await pay.upsert_lines(session, actor=_SYSTEM, employee_id=emp.id,
                                   yyyymm=yyyymm, lines=r["lines"])
            if confirm:
                await pay.confirm(session, actor=_SYSTEM, employee_id=emp.id, yyyymm=yyyymm,
                                  paid_on=date(int(yyyymm[:4]), int(yyyymm[5:7]), 25))
            n_pay += 1
            print(f"  {emp.employee_no} {emp.name} base={base:,} net~", end="\r")
        await session.commit()
    print(f"\n완료: 직원 {len(emps)}명 · 보수 프로필 신규/보완 {n_comp} · 급여({yyyymm}) 생성 {n_pay}"
          f"{' · 확정(GL 전기)' if confirm else ' · 라인만(미확정)'}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ym = args[0] if args else "2026-07"
    asyncio.run(main(ym, confirm="--no-confirm" not in sys.argv))
