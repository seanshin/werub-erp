"""의료진 개별 계약 시드 (테스트용) — DOCTOR/RESIDENT 전원에 활성 계약 1건.

의료진(staff_class=MEDICAL) 각자에게 기본급·당직비·성과급 계약형(NET_COLLECTION/DEPT_SHARE/
TARGET_EXCESS/NONE 순환)을 부여해 /erp/hr/medical-contracts 목록·성과급 산정을 테스트. 멱등(이미
ACTIVE 계약 있으면 건너뜀). **테스트 합성 데이터**.

사용: python scripts/seed_medical_contracts_demo.py
대상: DATABASE_URL — 테스트/파일럿 DB 권장.
"""
import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import select  # noqa: E402

from erp.db import session_factory  # noqa: E402
from erp.hr.contract_service import MedicalContractService  # noqa: E402
from erp.hr.models import Employee, MedicalStaffContract  # noqa: E402

_SYSTEM = 1
_BASE = {"DOCTOR": 8_000_000, "RESIDENT": 4_500_000}
# (incentive_type, rate, params) 순환 — 계약형 다양화
_PLANS = [
    ("NET_COLLECTION", 15.0, None),
    ("DEPT_SHARE", 10.0, None),
    ("TARGET_EXCESS", 20.0, {"target": 30_000_000}),
    ("NONE", None, None),
]


async def main() -> None:
    svc = MedicalContractService()
    async with session_factory()() as session:
        emps = (await session.scalars(
            select(Employee).where(Employee.resigned_on.is_(None),
                                   Employee.staff_class == "MEDICAL",
                                   Employee.job_group.in_(("DOCTOR", "RESIDENT")))
            .order_by(Employee.employee_no))).all()
        n_new = n_skip = 0
        for emp in emps:
            active = await session.scalar(select(MedicalStaffContract.id).where(
                MedicalStaffContract.employee_id == emp.id,
                MedicalStaffContract.status.in_(("ACTIVE", "EXPIRING", "APPROVED", "DRAFT"))))
            if active is not None:
                n_skip += 1
                continue
            itype, rate, params = _PLANS[emp.id % len(_PLANS)]
            await svc.create(session, actor=_SYSTEM, employee_id=emp.id,
                             valid_from=date(2026, 1, 1), base_pay=_BASE.get(emp.job_group, 8_000_000),
                             duty_pay=100_000, incentive_type=itype, incentive_rate=rate,
                             incentive_params=params, exclusive=True, require_approval=False,
                             note="시드(테스트)")
            n_new += 1
            print(f"  {emp.employee_no} {emp.name} {itype}", end="\r")
        await session.commit()
    print(f"\n완료: 의료진 {len(emps)}명 · 계약 신규 {n_new} · 기존 스킵 {n_skip}")


if __name__ == "__main__":
    asyncio.run(main())
