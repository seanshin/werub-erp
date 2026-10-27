"""예산 집행률 (FR-09) + 월별 정산 리포트 — caller_scope 명시 인자 (design §6.2)."""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.finance.models import Budget, Voucher, VoucherLine


def _year_range(year: int) -> tuple[date, date]:
    """역년 sargable 경계 — extract(year) 대신 범위로 ix_voucher_status_on 활용(성능)."""
    return date(year, 1, 1), date(year + 1, 1, 1)


class FinanceReportService:
    async def budget_execution(self, session: AsyncSession, *, fiscal_year: int,
                               scope_dept_id: int | None = None) -> list[dict]:
        """예산 대비 집행 — 부서장 scope면 본부서만 (집계에 명시 WHERE)."""
        bq = select(Budget).where(Budget.fiscal_year == fiscal_year)
        if scope_dept_id is not None:
            bq = bq.where(Budget.department_id == scope_dept_id)
        budgets = (await session.execute(bq)).scalars().all()
        if not budgets:
            return []

        # 분석 M1 — N+1 제거: (부서, 계정)별 DEBIT 합계를 단일 group-by로
        spent_rows = (await session.execute(
            select(VoucherLine.department_id, VoucherLine.account_code,
                   func.coalesce(func.sum(VoucherLine.amount), 0).label("spent"))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.side == "DEBIT",
                   Voucher.status == "POSTED",
                   Voucher.voucher_on >= _year_range(fiscal_year)[0],   # sargable(인덱스)
                   Voucher.voucher_on < _year_range(fiscal_year)[1])
            .group_by(VoucherLine.department_id, VoucherLine.account_code)
        )).all()
        spent_map = {(r.department_id, r.account_code): float(r.spent) for r in spent_rows}

        # 약정(OPEN encumbrance) 합계 — (부서, 계정)별
        from erp.finance.models import Encumbrance
        enc_rows = (await session.execute(
            select(Encumbrance.department_id, Encumbrance.account_code,
                   func.coalesce(func.sum(Encumbrance.amount), 0).label("committed"))
            .where(Encumbrance.fiscal_year == fiscal_year, Encumbrance.status == "OPEN")
            .group_by(Encumbrance.department_id, Encumbrance.account_code))).all()
        committed_map = {(r.department_id, r.account_code): float(r.committed) for r in enc_rows}

        result = []
        for b in budgets:
            amount = float(b.amount)
            spent = spent_map.get((b.department_id, b.account_code), 0.0)
            committed = committed_map.get((b.department_id, b.account_code), 0.0)
            available = amount - spent - committed           # 가용 = 예산 - 집행 - 약정
            used_rate = (spent + committed) / amount * 100 if amount else 0.0
            status = "OVER" if available < 0 else "WARNING" if used_rate >= 90 else "NORMAL"
            result.append({"department_id": b.department_id, "account_code": b.account_code,
                           "budget": amount, "spent": spent, "committed": committed,
                           "available": available, "variance": amount - spent,
                           "rate": round(spent / amount * 100, 1) if amount else 0.0,
                           "used_rate": round(used_rate, 1), "status": status})
        return result

    async def monthly_summary(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """월별 정산 — 계정과목별 차/대 합계 (역분개 반영: POSTED+REVERSED 상계·시산표와 정합)."""
        year, month = int(yyyymm[:4]), int(yyyymm[5:7])
        m_start = date(year, month, 1)
        m_end = date(year + (month == 12), (month % 12) + 1, 1)
        rows = (await session.execute(
            select(VoucherLine.account_code, VoucherLine.side,
                   func.coalesce(func.sum(VoucherLine.amount), 0).label("amt"))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(Voucher.status.in_(("POSTED", "REVERSED")),   # 원(REVERSED)+반전(POSTED) 상계
                   Voucher.voucher_on >= m_start,   # sargable 월 범위(ix_voucher_status_on)
                   Voucher.voucher_on < m_end)
            .group_by(VoucherLine.account_code, VoucherLine.side)
        )).all()
        summary: dict[str, dict] = {}
        for code, side, amt in rows:
            entry = summary.setdefault(code, {"debit": 0.0, "credit": 0.0})
            entry["debit" if side == "DEBIT" else "credit"] += float(amt)
        return {"yyyymm": yyyymm, "accounts": summary}
