"""의사별 진료실적 집계 (P4-2) — 성과급 산정 입력. 기간(YYYY-MM)·담당의별.

데이터 정합(검토 보완 §5.5): 청구(ClaimStatement, 발생주의=공단+본인부담)를 총진료수익으로 우선.
단 실 통보서에 명세별 담당의 미포함 시(doctor_id 0) → **수납(Reception.doctor_id→Payment, 현금주의·
본인부담)** 을 가용 1차로. claim_revenue 있으면 발생주의 사용. direct_cost는 costing 배부(PR-2)라 0 입력.
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.claims.models import ClaimBatch, ClaimStatement
from erp.patient.models import Payment, Reception


def _month_range(period: str) -> tuple[date, date]:
    y, m = int(period[:4]), int(period[5:7])
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return start, end


class PhysicianRevenueService:
    async def aggregate(self, session: AsyncSession, *, period: str,
                        doctor_id: int | None = None) -> dict:
        """기간 의사별 실적 — 수납(현금)·청구(발생)·삭감 집계. net = 진료수익 − 직접원가 − 삭감."""
        start, end = _month_range(period)
        # 수납(현금주의·본인부담) by 담당의
        pay_q = (select(Reception.doctor_id, func.sum(Payment.amount))
                 .join(Reception, Reception.id == Payment.reception_id)
                 .where(Payment.paid_on >= start, Payment.paid_on < end,
                        Payment.status == "PAID", Reception.doctor_id.isnot(None))
                 .group_by(Reception.doctor_id))
        if doctor_id is not None:
            pay_q = pay_q.where(Reception.doctor_id == doctor_id)
        collection = {d: float(a or 0) for d, a in (await session.execute(pay_q)).all()}

        # 청구(발생주의·공단+본인부담) + 삭감 by 담당의 (doctor_id 채워진 경우)
        claim_q = (select(ClaimStatement.doctor_id,
                          func.sum(ClaimStatement.claimed_amt),
                          func.sum(ClaimStatement.adjusted_amt))
                   .join(ClaimBatch, ClaimBatch.id == ClaimStatement.batch_id)
                   .where(ClaimBatch.care_month == period,
                          ClaimStatement.doctor_id.isnot(None))
                   .group_by(ClaimStatement.doctor_id))
        if doctor_id is not None:
            claim_q = claim_q.where(ClaimStatement.doctor_id == doctor_id)
        claims = {d: (float(c or 0), float(a or 0))
                  for d, c, a in (await session.execute(claim_q)).all()}

        doctors = sorted(set(collection) | set(claims))
        rows = []
        for d in doctors:
            claim_rev, deduction = claims.get(d, (0.0, 0.0))
            coll = collection.get(d, 0.0)
            # 발생주의(청구) 우선, 없으면 수납(현금·본인부담) 사용
            basis = "CLAIM" if claim_rev else "COLLECTION"
            gross_revenue = claim_rev if claim_rev else coll
            direct_cost = 0.0           # costing 배부(PR-2 결정) — 후속
            net = gross_revenue - direct_cost - deduction
            rows.append({"doctor_id": d, "basis": basis, "collection": coll,
                         "claim_revenue": claim_rev, "deduction": deduction,
                         "direct_cost": direct_cost, "net_revenue": net})
        return {"period": period, "rows": rows,
                "note": "claim_revenue=0이면 수납(본인부담) 기준 — 청구 명세 담당의(doctor_id) 채워지면 "
                        "발생주의 총진료수익으로 자동 전환. direct_cost는 원가배부(PR-2) 후 반영."}
