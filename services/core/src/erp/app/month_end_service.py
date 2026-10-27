"""월마감 일괄(month-end close) — 감가상각·퇴직급여충당·리스 회계를 한 번에 실행.

각 단계는 멱등(자산·직원·리스 × 기간 UNIQUE)이라 재실행 안전. 기간 잠금/결산분개는 별도(관리자 확인).
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.finance.models import LeaseContract
from erp.scm.models import OutsourcingContract


class MonthEndService:
    async def run(self, session: AsyncSession, *, yyyymm: str, user_id: int | None = None) -> dict:
        from erp.finance.asset_service import AssetService
        from erp.finance.lease_service import LeaseService
        from erp.hr.retirement_service import RetirementService
        from erp.scm.contract_service import OutsourcingContractService

        dep = await AssetService().run_depreciation(session, period=yyyymm, created_by=user_id)
        ret = await RetirementService().accrue_monthly(session, period=yyyymm, user_id=user_id)
        leases = (await session.execute(
            select(LeaseContract).where(LeaseContract.status == "ACTIVE"))).scalars().all()
        lease_done = lease_skip = 0
        lease_svc = LeaseService()
        for lease in leases:
            r = await lease_svc.accrue_monthly(session, lease_id=lease.id, period=yyyymm,
                                               user_id=user_id)
            if r.get("already"):
                lease_skip += 1
            else:
                lease_done += 1
        # 위수탁 월 위탁료 — 월정액(MONTHLY) 계약 중 그 달과 계약기간이 겹치는 것(멱등: contract:{id}:{yyyymm})
        # 2026-10-09 전체 흐름 검토 2차 E: 종전엔 ACTIVE/EXPIRING만 골라, 일 배치가 EXPIRED로 바꾸거나
        # 월중 TERMINATED된 계약의 **마지막 달** 위탁료가 영영 발생하지 않았다. 상태 대신 기간 겹침으로
        # 판정(accruable_for_month: 만료·해지 계약은 해지일까지의 계약기간 안 월) — 미발효·반려 계약은 제외.
        from erp.scm.contract_service import MONTH_END_ACCRUABLE
        contracts = (await session.execute(select(OutsourcingContract).where(
            OutsourcingContract.status.in_(tuple(MONTH_END_ACCRUABLE)),
            OutsourcingContract.billing_type == "MONTHLY"))).scalars().all()
        c_svc = OutsourcingContractService()
        contract_accrued = contract_skip = 0
        # 2026-10-09 전체 흐름 검토 E: accrue_month가 계약기간 밖 월을 거부하게 됐으므로, 일괄은
        # 그 달과 기간이 겹치는 계약만 대상으로 한다(시작 전·만료 후 계약 때문에 월마감 전체가 실패 방지).
        from calendar import monthrange
        from datetime import date as _date

        from erp.scm.contract_service import accruable_for_month
        y, m = int(yyyymm[:4]), int(yyyymm[5:7])
        m_start, m_end = _date(y, m, 1), _date(y, m, monthrange(y, m)[1])
        for c in contracts:
            if not accruable_for_month(c, m_start, m_end):
                if c.status in ("ACTIVE", "EXPIRING"):   # 종료된 과거 계약은 건너뜀 집계에서 제외(소음)
                    contract_skip += 1
                continue
            r = await c_svc.accrue_month(session, actor=user_id or 0, contract_id=c.id,
                                         yyyymm=yyyymm)
            if r.get("duplicate") or r.get("skipped"):
                contract_skip += 1
            else:
                contract_accrued += 1
        return {"period": yyyymm,
                "depreciation_count": dep,
                "retirement_employees": ret["employees"],
                "retirement_increment": ret["total_increment"],
                "lease_accrued": lease_done, "lease_skipped": lease_skip,
                "contract_accrued": contract_accrued, "contract_skipped": contract_skip}
