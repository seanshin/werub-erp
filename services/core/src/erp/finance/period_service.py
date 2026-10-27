"""회계 기간 마감 (월 결산) — CLOSED 기간 전표 생성 차단 + 마감 시 손익 스냅샷 보존.

마감 후 정정은 FR-54 역분개(현재 개방기간 전기)만 허용 — 마감 기간 직접 전기 불가.
연말 결산분개(손익→이익잉여금)는 자본계정 도입 후 별도(현 chart에 EQUITY 미존재).
"""
from calendar import monthrange
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.months import require_ym
from erp.errors import ValidationError
from erp.finance.models import FiscalPeriod, Voucher
from erp.finance.statements import StatementService


def _bounds(yyyymm: str) -> tuple[date, date]:
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


class PeriodService:
    async def is_locked(self, session: AsyncSession, yyyymm: str) -> bool:
        return (await session.scalar(
            select(FiscalPeriod.status).where(FiscalPeriod.yyyymm == yyyymm))) == "CLOSED"

    async def list_periods(self, session: AsyncSession, *, year: int | None = None) -> list[FiscalPeriod]:
        q = select(FiscalPeriod).order_by(FiscalPeriod.yyyymm.desc())
        if year:
            q = q.where(FiscalPeriod.fiscal_year == year)
        return list((await session.execute(q)).scalars())

    async def preclose_check(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """마감 전 점검 (M3) — 미결전표·차대불일치·감가미계상·미배부원가·미매칭세금계산서.

        경고 목록 반환(차단 아님 — close(enforce=True)일 때만 차단). 결산 정합 사전 확인."""
        yyyymm = require_ym(yyyymm)              # 단일 소스(common.months) — 종전 자체 검사는 월 13·문자도 통과
        from erp.finance.models import (
            CostAllocationSnapshot, DepreciationEntry, FixedAsset, TaxInvoice,
        )
        since, until = _bounds(yyyymm)
        warnings: list[dict] = []

        # 1) 미결 전표(M2) — DRAFT/PENDING은 미전기라 마감 전 처리 필요
        unposted = await session.scalar(select(func.count()).select_from(Voucher).where(
            Voucher.voucher_on >= since, Voucher.voucher_on <= until,
            Voucher.status.in_(("DRAFT", "PENDING"))))
        if unposted:
            warnings.append({"code": "UNPOSTED_VOUCHERS", "count": int(unposted),
                             "message": f"미결 전표 {unposted}건(상신/승인 필요)"})

        # 2) 차대 불일치
        tb = await StatementService().trial_balance(session, since=since, until=until)
        if not tb["balanced"]:
            warnings.append({"code": "UNBALANCED", "count": 1,
                             "message": "시산표 차대 불일치"})

        # 3) 감가 미계상 — IN_USE·취득완료 자산 중 당월 감가내역 없음
        dep_sub = (select(DepreciationEntry.asset_id)
                   .where(DepreciationEntry.period == yyyymm))
        missing_dep = await session.scalar(select(func.count()).select_from(FixedAsset).where(
            FixedAsset.status == "IN_USE", FixedAsset.acquired_on <= until,
            FixedAsset.id.not_in(dep_sub)))
        if missing_dep:
            warnings.append({"code": "MISSING_DEPRECIATION", "count": int(missing_dep),
                             "message": f"감가 미계상 자산 {missing_dep}건"})

        # 4) 미배부 원가 — 당월 ABC 배부 스냅샷 부재
        has_cost = await session.scalar(select(func.count()).select_from(CostAllocationSnapshot)
                                        .where(CostAllocationSnapshot.period == yyyymm))
        if not has_cost:
            warnings.append({"code": "NO_COST_ALLOCATION", "count": 1,
                             "message": "원가 배부 스냅샷 없음"})

        # 5) 미매칭 세금계산서 — 당월 발행분 중 RECEIVED(미대사)
        unmatched_ti = await session.scalar(select(func.count()).select_from(TaxInvoice).where(
            TaxInvoice.issue_date >= since, TaxInvoice.issue_date <= until,
            TaxInvoice.status == "RECEIVED"))
        if unmatched_ti:
            warnings.append({"code": "UNMATCHED_TAX_INVOICE", "count": int(unmatched_ti),
                             "message": f"미대사 세금계산서 {unmatched_ti}건"})

        # 6) 대손충당금 미전기(K-IFRS 1109) — 목표 충당과 차액이 있으면 경고
        from erp.finance.allowance_service import AllowanceService
        try:
            alw = await AllowanceService().preview(session, yyyymm=yyyymm)
            if alw["delta"]:
                warnings.append({"code": "ALLOWANCE_NOT_POSTED", "count": 1,
                                 "message": f"대손충당금 조정 미전기(차액 {alw['delta']:,}원)"})
        except Exception:                       # 계정 미시드 등 — 점검만, 차단 안 함
            pass

        # 7) 이연법인세 미계상(K-IFRS 1012) — 연말(12월)만 점검
        if until.month == 12:
            from erp.finance.deferred_tax_service import DeferredTaxService
            try:
                dt = await DeferredTaxService().preview(session, year=until.year)
                if dt["delta_dta"] or dt["delta_dtl"]:
                    warnings.append({"code": "DEFERRED_TAX_NOT_POSTED", "count": 1,
                                     "message": "이연법인세 조정 미전기"})
            except Exception:
                pass

        return {"yyyymm": yyyymm, "ok": not warnings, "warnings": warnings}

    async def _save_snapshots(self, session: AsyncSession, *, yyyymm: str,
                              since: date, until: date, user_id: int) -> None:
        """마감 확정 재무제표 스냅샷 보존 — 버전 append(무버전 덮어쓰기 금지·G2).

        재개 시 활성 스냅샷은 supersede되므로 재마감은 새 버전을 남겨 확정 재무제표 이력을 보존한다."""
        from erp.finance.models import ClosingSnapshot
        ss = StatementService()
        snaps = {
            "trial_balance": await ss.trial_balance(session, since=since, until=until),
            "income": await ss.income_statement(session, since=since, until=until),
            "balance_sheet": await ss.balance_sheet(session, as_of=until),
            "cash_flow": await ss.cash_flow_statement(session, since=since, until=until),
        }
        now = datetime.now(timezone.utc)
        for kind, payload in snaps.items():
            prev_max = await session.scalar(select(func.max(ClosingSnapshot.version)).where(
                ClosingSnapshot.yyyymm == yyyymm, ClosingSnapshot.kind == kind))
            # 활성 스냅샷이 남아있으면(재개 없이 중복호출 등) supersede해 부분유니크 위반 방지.
            active = (await session.execute(select(ClosingSnapshot).where(
                ClosingSnapshot.yyyymm == yyyymm, ClosingSnapshot.kind == kind,
                ClosingSnapshot.superseded_at.is_(None)))).scalar_one_or_none()
            if active is not None:
                active.superseded_at = now
            session.add(ClosingSnapshot(yyyymm=yyyymm, kind=kind, payload=payload,
                                        created_by=user_id, version=(prev_max or 0) + 1))

    async def close(self, session: AsyncSession, *, yyyymm: str, user_id: int,
                    note: str = "", enforce: bool = False) -> FiscalPeriod:
        """기간 마감 — 사전점검(enforce 시 차단) → 손익 스냅샷 + 재무제표 확정본 보존 후 CLOSED."""
        yyyymm = require_ym(yyyymm)              # 단일 소스(common.months) — 종전 자체 검사는 월 13·문자도 통과
        period = await session.get(FiscalPeriod, yyyymm)
        if period is not None and period.status == "CLOSED":
            raise ValidationError(f"이미 마감된 기간입니다: {yyyymm}")
        check = await self.preclose_check(session, yyyymm=yyyymm)
        if enforce and not check["ok"]:
            raise ValidationError("마감 전 점검 실패 — 경고 해소 필요",
                                  {"warnings": check["warnings"]})
        since, until = _bounds(yyyymm)
        inc = await StatementService().income_statement(session, since=since, until=until)
        vc = await session.scalar(select(func.count()).select_from(Voucher).where(
            Voucher.voucher_on >= since, Voucher.voucher_on <= until))
        if period is None:
            period = FiscalPeriod(yyyymm=yyyymm, fiscal_year=int(yyyymm[:4]))
            session.add(period)
        period.status = "CLOSED"
        period.total_revenue = inc["total_revenue"]
        period.total_expense = inc["total_expense"]
        period.net_income = inc["net_income"]
        period.voucher_count = vc
        period.closed_by = user_id
        period.closed_at = datetime.now(timezone.utc)
        period.note = note or None
        await session.flush()
        await self._save_snapshots(session, yyyymm=yyyymm, since=since, until=until,
                                   user_id=user_id)
        return period

    async def reopen(self, session: AsyncSession, *, yyyymm: str, user_id: int,
                     note: str = "") -> FiscalPeriod:
        """마감 재개방 — 정정 필요 시. 직무분리(마감자≠재개자) + 마감 귀속 보존 + 스냅샷 버전보존(G2)."""
        period = await session.get(FiscalPeriod, yyyymm)
        if period is None or period.status != "CLOSED":
            raise ValidationError(f"마감 상태가 아닙니다: {yyyymm}")
        # 직무분리(G2) — 마감한 본인은 재개할 수 없음. 다른 관리자의 재개가 필요.
        if period.closed_by is not None and period.closed_by == user_id:
            raise ValidationError(
                "직무분리 — 마감을 수행한 본인은 기간을 재개할 수 없습니다 (다른 관리자가 재개해야 합니다)")
        now = datetime.now(timezone.utc)
        period.status = "OPEN"
        # 마감 귀속(closed_by/at)은 파괴하지 않고 보존 — 재개 이력은 별도 컬럼에 append.
        period.reopened_by = user_id
        period.reopened_at = now
        period.reopen_note = note or None
        period.reopen_count = (period.reopen_count or 0) + 1
        # 확정 재무제표 스냅샷 supersede(버전 보존) — 재마감 시 새 버전 append.
        from erp.finance.models import ClosingSnapshot
        actives = (await session.execute(select(ClosingSnapshot).where(
            ClosingSnapshot.yyyymm == yyyymm,
            ClosingSnapshot.superseded_at.is_(None)))).scalars().all()
        for snap in actives:
            snap.superseded_at = now
        return period

    async def reconcile(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """마감 무결성 대사(G6·S6) — 마감 확정 스냅샷 대비 현재 재도출 재무제표의 드리프트 탐지.

        마감월 GL은 P1-a(DB 트리거+마감가드)로 불변이라 정상 시 드리프트=0. 연말결산 등
        allow_closed 전기·서브원장 변경·통제 우회가 있으면 총계 델타로 드러난다. 읽기 전용(비침습).
        """
        yyyymm = require_ym(yyyymm)              # 단일 소스(common.months) — 종전 자체 검사는 월 13·문자도 통과
        period = await session.get(FiscalPeriod, yyyymm)
        if period is None or period.status != "CLOSED":
            raise ValidationError(f"마감된 기간이 아닙니다: {yyyymm}")
        from erp.finance.models import ClosingSnapshot
        snaps = {r.kind: r.payload for r in (await session.execute(select(ClosingSnapshot).where(
            ClosingSnapshot.yyyymm == yyyymm,
            ClosingSnapshot.superseded_at.is_(None)))).scalars()}
        since, until = _bounds(yyyymm)
        ss = StatementService()
        tb_now = await ss.trial_balance(session, since=since, until=until)
        if not snaps:
            return {"yyyymm": yyyymm, "snapshot_present": False, "drifted": None, "drifts": [],
                    "current_balanced": tb_now["balanced"],
                    "note": "확정 스냅샷 없음(마감 전 버전 데이터) — 대사 불가"}
        inc_now = await ss.income_statement(session, since=since, until=until)
        bs_now = await ss.balance_sheet(session, as_of=until)

        def _d(now: dict, kind: str, key: str) -> int:
            return round(float(now.get(key, 0) or 0) - float((snaps.get(kind) or {}).get(key, 0) or 0))

        checks = [
            ("trial_balance", "total_debit", _d(tb_now, "trial_balance", "total_debit")),
            ("trial_balance", "total_credit", _d(tb_now, "trial_balance", "total_credit")),
            ("income", "net_income", _d(inc_now, "income", "net_income")),
            ("balance_sheet", "total_assets", _d(bs_now, "balance_sheet", "total_assets")),
            ("balance_sheet", "total_liabilities", _d(bs_now, "balance_sheet", "total_liabilities")),
            ("balance_sheet", "equity", _d(bs_now, "balance_sheet", "equity")),
        ]
        drifts = [{"statement": s, "field": f, "delta": d} for s, f, d in checks if d != 0]
        return {"yyyymm": yyyymm, "snapshot_present": True, "drifted": bool(drifts),
                "drifts": drifts, "current_balanced": tb_now["balanced"]}
