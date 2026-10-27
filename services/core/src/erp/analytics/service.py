"""경영분석 BI (FR-19 확장) — 재무·인사·진료·보험 데이터를 종합한 경영 KPI·추세.

읽기 전용 — 기존 모듈 서비스(재무제표·원가·채권채무) + 카운트 집계 조합. 신규 테이블 없음.
"""
import calendar
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.costing.service import CostingService, CostSnapshotService
from erp.finance.settlement_service import SettlementService
from erp.finance.statements import StatementService
from erp.hr.models import Employee
from erp.patient.models import Patient, Reception, Reservation


def _month_range(yyyymm: str) -> tuple[date, date]:
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


# 고정비(영업) 계정 — 인건비(급여·퇴직급여)·감가상각·리스(사용권자산상각·리스이자). 재료비(5410)=변동비.
FIXED_COST_CODES = ("5310", "5320", "5210", "5640", "5630")
LABOR_CODES = ("5310", "5320")


class AnalyticsService:
    def __init__(self):
        self._stmt = StatementService()
        self._cost = CostingService()
        self._settle = SettlementService()

    async def overview(self, session: AsyncSession, *, yyyymm: str,
                       reconstruct: bool = False) -> dict:
        """경영 요약 KPI — 재무·인사·진료·보험.

        reconstruct=True: 미수/미지급잔액을 as_of(월말) 시점 재구성(마트 소급 오염 방지) — 과거 월
        재갱신 시 안정. UI(현재 월)는 기본 False(빠른 현시점 경로, as_of=오늘이면 값 동일)."""
        since, until = _month_range(yyyymm)
        inc = await self._stmt.income_statement(session, since=since, until=until)
        bs = await self._stmt.balance_sheet(session, as_of=until)
        cf = await self._settle.cash_flow(session, since=since, until=until)
        ar = await self._settle.aging(session, as_of=until, kind="AR", reconstruct=reconstruct)
        ap = await self._settle.aging(session, as_of=until, kind="AP", reconstruct=reconstruct)
        labor = sum(r["labor_cost"]
                    for r in await self._cost.labor_by_department(session, yyyymm=yyyymm))
        loss = sum(r["deduction_loss"]
                   for r in await self._cost.deduction_loss_by_department(
                       session, care_month=yyyymm))

        # 재직인원·환자수 — reconstruct=True(마트)면 as_of(월말) 시점 재구성해 과거 월이 안정.
        # 미지정(라이브 UI)이면 현재 상태(빠른 현시점 경로). resigned_on IS NULL·count(Patient)를
        # 그대로 쓰면 이후 입·퇴사·신규환자가 과거 월 KPI를 소급 변경(전체검토 마트 as-of 불안정).
        if reconstruct:
            nxt = date(until.year + (until.month == 12), (until.month % 12) + 1, 1)
            headcount = await session.scalar(
                select(func.count()).select_from(Employee)
                .where(Employee.hired_on <= until,
                       (Employee.resigned_on.is_(None)) | (Employee.resigned_on > until)))
            patients = await session.scalar(
                select(func.count()).select_from(Patient).where(Patient.created_at < nxt))
        else:
            headcount = await session.scalar(
                select(func.count()).select_from(Employee).where(Employee.resigned_on.is_(None)))
            patients = await session.scalar(select(func.count()).select_from(Patient))
        # 이직률(최근 12개월) — 기간 퇴직자 / 재직(as_of 또는 현재) (연환산 trailing)
        year_ago = date(until.year - 1, until.month, 1)
        left_12m = await session.scalar(
            select(func.count()).select_from(Employee)
            .where(Employee.resigned_on >= year_ago, Employee.resigned_on <= until)) or 0
        joined_12m = await session.scalar(
            select(func.count()).select_from(Employee)
            .where(Employee.hired_on >= year_ago, Employee.hired_on <= until)) or 0
        turnover = round(left_12m / headcount * 100, 1) if headcount else 0
        receptions = await session.scalar(
            select(func.count()).select_from(Reception)
            .where(Reception.received_on >= since, Reception.received_on <= until))
        reservations = await session.scalar(select(func.count()).select_from(Reservation))

        revenue = inc["total_revenue"]
        labor_ratio = round(labor / revenue * 100, 1) if revenue else 0
        return {
            "yyyymm": yyyymm,
            "재무": {"수익": revenue, "비용": inc["total_expense"], "당기순이익": inc["net_income"],
                     "자산총계": bs["total_assets"], "부채총계": bs["total_liabilities"],
                     "순현금": cf["net"], "미수금잔액": ar["total"], "미지급잔액": ap["total"]},
            "인사": {"재직인원": int(headcount or 0), "월인건비": labor,
                     "인건비_대_수익_%": labor_ratio, "최근1년입사": int(joined_12m),
                     "최근1년퇴직": int(left_12m), "이직률_%": turnover},
            "진료": {"환자수": int(patients or 0), "당월접수": int(receptions or 0),
                     "예약": int(reservations or 0)},
            "보험": {"삭감손실": loss,
                     "삭감률_%": round(loss / revenue * 100, 1) if revenue else 0},
        }

    async def financial_ratios_cached(self, session: AsyncSession, *, year: int) -> dict:
        """재무비율 — RatiosAnnualMart 우선 서빙, 캐시미스 시 라이브 폴백(성능검토 H-3).

        대시보드 재무건전성 위젯·분석 화면이 호출하던 3회 전 원장 스캔(balance_sheet+income+operating)을
        마트에서 서빙(≤1일·마감월 동결). 마트는 analytics 소유라 캐싱은 analytics 계층(finance→analytics
        역류 금지·import-linter). 마감/공시용 라이브 원본은 `/finance/statements/financial-ratios` 유지."""
        from erp.analytics.mart_models import RatiosAnnualMart
        row = await session.get(RatiosAnnualMart, year)
        if row is not None:
            def _f(v):
                return float(v) if v is not None else None
            return {"year": year, "current_ratio": _f(row.current_ratio),
                    "debt_ratio": _f(row.debt_ratio), "equity_ratio": _f(row.equity_ratio),
                    "operating_margin": _f(row.operating_margin),
                    "labor_cost_ratio": _f(row.labor_cost_ratio),
                    "material_cost_ratio": _f(row.material_cost_ratio),
                    "basis": row.payload, "_source": "mart",
                    "_refreshed_at": row.refreshed_at.isoformat() if row.refreshed_at else None}
        live = await StatementService().financial_ratios(session, year=year)
        return {**live, "_source": "live"}

    async def executive_cached(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """경영진 대시보드 — BI 마트(ExecKpiMart.payload) 우선 서빙, 캐시미스 시 라이브 폴백(성능검토 H-1).

        `/executive`는 요청마다 원장 팬아웃 ~30집계(balance_sheet·income_statement 각 4회 전 원장 스캔)를
        재계산하던 핫패스. 야간 잡이 당월·전월 마트를 이미 적재(≤1일 신선도·마감월 동결·집계·PII 없음)하므로
        마트에서 서빙. 미적재(첫 실행 전·범위 밖) 시 라이브 계산(정확). 경영 KPI는 일 단위 신선도로 충분.
        """
        from erp.analytics.mart_models import ExecKpiMart
        row = await session.get(ExecKpiMart, yyyymm)
        if row is not None and row.payload:
            return {**row.payload, "_source": "mart",
                    "_refreshed_at": row.refreshed_at.isoformat() if row.refreshed_at else None}
        return {**await self.executive(session, yyyymm=yyyymm), "_source": "live"}

    async def executive(self, session: AsyncSession, *, yyyymm: str,
                        reconstruct: bool = False) -> dict:
        """경영진 종합 대시보드 — overview(재무·인사·진료·보험) + 자금·재고·자산·정합·재무비율·검진권.

        그동안 추가된 도메인 반영: 재무비율(P1), 재고회전율·장기재고, 검진권 운영현황.
        reconstruct는 overview(미수/미지급잔액 as_of 재구성)로 전달 — 마트 갱신 시 True.
        """
        from erp.finance.asset_service import AssetService
        from erp.scm.valuation_service import ValuationService
        base = await self.overview(session, yyyymm=yyyymm, reconstruct=reconstruct)
        since, until = _month_range(yyyymm)
        val_svc = ValuationService()
        fc = await self._settle.cash_forecast(session, as_of=until, months=6)
        val = await val_svc.valuation(session)
        sh = await val_svc.shortage(session)
        to = await val_svc.turnover(session, since=since, until=until)
        assets = [a for a in await AssetService().list_assets(session) if a["status"] == "IN_USE"]
        bs = await self._stmt.balance_sheet(session, as_of=until)
        ratios = await self._stmt.financial_ratios(session, year=since.year)
        vo = await self.voucher_overview(session, date_from=since, date_to=until)
        inc = await self._stmt.income_statement(session, since=since, until=until)

        def _pct(x):  # 비율(소수) → % (None 보존)
            return round(x * 100, 1) if x is not None else None

        rev = inc["total_revenue"]
        # 고정/변동 분류는 계정과목 설정(cost_type)을 SoT로 — 미설정 시 기본 코드 폴백
        from erp.finance.account_service import AccountService
        fixed_codes = await AccountService.fixed_cost_codes(session) or set(FIXED_COST_CODES)
        variable_codes = await AccountService.codes_by_cost_type(session, "VARIABLE") or {"5410"}
        fixed = sum(e["amount"] for e in inc["expenses"] if e["account_code"] in fixed_codes)
        variable = sum(e["amount"] for e in inc["expenses"] if e["account_code"] in variable_codes)
        labor = sum(e["amount"] for e in inc["expenses"] if e["account_code"] in LABOR_CODES)
        dep = sum(e["amount"] for e in inc["expenses"] if e["account_code"] in ("5210", "5640"))
        material = sum(e["amount"] for e in inc["expenses"] if e["account_code"] == "5410")

        base["자금"] = {"기초현금": fc["opening_cash"], "6개월_최저잔액": fc["min_balance"]}
        base["재고"] = {"재고자산평가": val["total_value"], "안전재고미달": sh["count"],
                        "장기재고건수": to["dead_stock_count"]}
        base["자산"] = {"사용중_자산수": len(assets),
                        "장부가총계": round(sum(a["book_value"] for a in assets))}
        base["재무비율"] = {"유동비율": ratios["current_ratio"],
                            "부채비율_%": _pct(ratios["debt_ratio"]),
                            "의료이익률_%": _pct(ratios["operating_margin"]),
                            "인건비율_%": _pct(ratios["labor_cost_ratio"])}
        cb = vo["settlement_callbacks"]
        base["검진권"] = {"발급액": vo["issued"]["amount"], "사용액": vo["redeemed"]["amount"],
                          "미사용잔액": vo["unredeemed_krw"],
                          "미완료콜백": int(sum(c for s, c in cb.items() if s != "SENT"))}
        base["고정비용"] = {"고정비합계": round(fixed), "인건비": round(labor), "감가상각": round(dep),
                            "고정비율_%": round(fixed / rev * 100, 1) if rev else 0}
        base["변동비용"] = {"변동비합계": round(variable), "재료비": round(material),
                            "변동비율_%": round(variable / rev * 100, 1) if rev else 0}
        base["정합"] = {"재무상태표_균형": bs["balanced"], "자본총계": bs["equity"]}
        return base

    async def department_pl(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """진료과별 비용 구조 — 인건비(부서)·삭감손실(과코드)."""
        return {
            "yyyymm": yyyymm,
            "labor_by_department": await self._cost.labor_by_department(session, yyyymm=yyyymm),
            "deduction_by_dept_code": await self._cost.deduction_loss_by_department(
                session, care_month=yyyymm),
        }

    async def hr_turnover(self, session: AsyncSession, *, yyyymm: str,
                          reconstruct: bool = False) -> dict:
        """이직률 분석 — 최근 12개월 월별 입사/퇴직 추세 + 진료과별 이직률(퇴직/재직).

        reconstruct=True(마트)면 진료과별 재직(active)을 as_of(월말) 시점 재구성 → 과거 월 안정.
        미지정(라이브 UI)이면 현재 재직(resigned_on IS NULL)."""
        from erp.common.models import Department
        _, until = _month_range(yyyymm)
        year_ago = date(until.year - 1, until.month, 1)
        # 월별 입사·퇴직 (최근 12개월) — 날짜 조회 후 Python 버킷팅(방언 무관)
        hdates = (await session.scalars(select(Employee.hired_on)
            .where(Employee.hired_on >= year_ago, Employee.hired_on <= until))).all()
        joined: dict[str, int] = {}
        for h in hdates:
            joined[h.strftime("%Y-%m")] = joined.get(h.strftime("%Y-%m"), 0) + 1
        rdates = (await session.scalars(select(Employee.resigned_on)
            .where(Employee.resigned_on >= year_ago, Employee.resigned_on <= until))).all()
        left: dict[str, int] = {}
        for r in rdates:
            left[r.strftime("%Y-%m")] = left.get(r.strftime("%Y-%m"), 0) + 1
        months = [f"{(year_ago.year + (year_ago.month - 1 + k) // 12)}-"
                  f"{((year_ago.month - 1 + k) % 12) + 1:02d}" for k in range(13)]
        monthly = [{"month": m, "joined": joined.get(m, 0), "left": left.get(m, 0)} for m in months]
        # 진료과별 이직률 — 부서 현재 재직 + 최근 12개월 퇴직
        depts = {d.id: d.name for d in (await session.scalars(select(Department))).all()}
        active_where = ((Employee.hired_on <= until,
                         (Employee.resigned_on.is_(None)) | (Employee.resigned_on > until))
                        if reconstruct else (Employee.resigned_on.is_(None),))
        active = (await session.execute(
            select(Employee.department_id, func.count())
            .where(*active_where).group_by(Employee.department_id))).all()
        leftby = dict((await session.execute(
            select(Employee.department_id, func.count())
            .where(Employee.resigned_on >= year_ago, Employee.resigned_on <= until)
            .group_by(Employee.department_id))).all())
        by_dept = []
        for did, act in active:
            lf = int(leftby.get(did, 0))
            if lf == 0:
                continue
            denom = int(act) + lf
            by_dept.append({"department": depts.get(did, "-"), "active": int(act), "left": lf,
                            "rate": round(lf / denom * 100, 1) if denom else 0})
        by_dept.sort(key=lambda x: -x["rate"])
        # 퇴직 사유별 분포 (최근 12개월)
        rs = (await session.execute(
            select(Employee.resign_reason, func.count())
            .where(Employee.resigned_on >= year_ago, Employee.resigned_on <= until)
            .group_by(Employee.resign_reason))).all()
        by_reason = sorted(
            [{"reason": r or "미기재", "count": int(c)} for r, c in rs],
            key=lambda x: -x["count"])
        return {"yyyymm": yyyymm, "monthly": monthly, "by_dept": by_dept, "by_reason": by_reason}

    async def department_lines(self, session: AsyncSession, *, yyyymm: str,
                               dept_code: str, limit: int = 200) -> dict:
        """진료과(dept_code) 라인 드릴다운(2단계) — costing 경유(경계 계약: analytics↛claims).

        명세서 일련번호·금액만 노출(차트번호 등 PII 제외) — 경영분석 집계 목적.
        """
        items = await self._cost.statement_lines_by_department(
            session, care_month=yyyymm, dept_code=dept_code, limit=limit)
        return {"yyyymm": yyyymm, "dept_code": dept_code, "count": len(items), "items": items}

    async def segment_report(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """영업부문 보고(K-IFRS 1108) — 진료과(부문)별 수익·손익·자산 + 보고부문 임계(10%)."""
        from erp.finance.models import FixedAsset
        pl = await CostSnapshotService().department_pl(session, yyyymm=yyyymm)
        arows = (await session.execute(
            select(FixedAsset.department_id, func.coalesce(func.sum(
                FixedAsset.acquisition_cost + FixedAsset.accumulated_revaluation
                - FixedAsset.accumulated_depreciation - FixedAsset.accumulated_impairment), 0))
            .where(FixedAsset.status != "DISPOSED")
            .group_by(FixedAsset.department_id))).all()
        asset_by_did = {did: float(v) for did, v in arows}
        segs = [{"dept_code": d["dept_code"], "department": d["department"],
                 "revenue": d["revenue"], "profit": d["profit"],
                 "assets": asset_by_did.get(d["department_id"], 0.0)} for d in pl]
        tr = sum(s["revenue"] for s in segs)
        tp = sum(s["profit"] for s in segs)
        ta = sum(s["assets"] for s in segs)
        # IFRS 8.13(b) 손익 임계 분모 = max(이익부문 이익합, |손실부문 손실합|).
        # 부호합(tp)을 쓰면 이익·손실 상쇄 시(예: +100/-100→0) 손익 기준이 통째 무력화된다.
        prof_den = max(sum(s["profit"] for s in segs if s["profit"] > 0),
                       -sum(s["profit"] for s in segs if s["profit"] < 0))
        for s in segs:                 # IFRS 8 정량 임계 — 수익·|손익|·자산 중 하나라도 10%+
            s["reportable"] = bool((tr and s["revenue"] >= 0.1 * tr)
                                   or (prof_den and abs(s["profit"]) >= 0.1 * prof_den)
                                   or (ta and s["assets"] >= 0.1 * ta))
        return {"yyyymm": yyyymm, "segments": sorted(segs, key=lambda x: -x["revenue"]),
                "total_revenue": round(tr), "total_profit": round(tp), "total_assets": round(ta),
                "reportable_count": sum(1 for s in segs if s["reportable"])}

    async def sim_dept_baseline(self, session: AsyncSession, *, yyyymm: str,
                                months: int = 6) -> dict:
        """진료과별 시뮬레이션 기준선 — costing 경유(경계 계약: analytics↛claims)."""
        items = await self._cost.department_baseline(session, yyyymm=yyyymm, months=months)
        return {"yyyymm": yyyymm, "months_window": months, "departments": items}

    async def sim_baseline(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """재무 시뮬레이션 기준선 — 최근 6개월 월평균 손익(고정/변동/감가/기타)·현금·AR/AP·부채/자본.

        프론트가 이 baseline에 가정(드라이버)을 적용해 향후 투영(발생↔현금 조정 포함).
        """
        from erp.finance.account_service import AccountService
        since_first, until = _month_range(yyyymm)
        # 6개월 창 시작월
        sm = since_first.month - 5
        sy = since_first.year + (sm - 1) // 12
        since = date(sy, ((sm - 1) % 12) + 1, 1)
        months = (until.year - since.year) * 12 + (until.month - since.month) + 1

        inc = await self._stmt.income_statement(session, since=since, until=until)
        fixed_codes = await AccountService.fixed_cost_codes(session) or set(FIXED_COST_CODES)
        var_codes = await AccountService.codes_by_cost_type(session, "VARIABLE") or {"5410"}
        exps = inc["expenses"]
        rev_w = inc["total_revenue"]
        fixed_w = sum(e["amount"] for e in exps if e["account_code"] in fixed_codes)
        var_w = sum(e["amount"] for e in exps if e["account_code"] in var_codes)
        dep_w = sum(e["amount"] for e in exps if e["account_code"] in ("5210", "5640"))
        other_w = inc["total_expense"] - fixed_w - var_w

        bs = await self._stmt.balance_sheet(session, as_of=until)
        fc = await self._settle.cash_forecast(session, as_of=until, months=1)
        ar = await self._settle.aging(session, as_of=until, kind="AR")
        ap = await self._settle.aging(session, as_of=until, kind="AP")
        tr = (await self.monthly_trend(session, year=until.year))["months"]
        ptr = (await self.monthly_trend(session, year=until.year - 1))["months"]
        revs = [m["revenue"] for m in tr if m["revenue"]]

        def _cagr(end: float, start: float, periods: int) -> float | None:
            """기간 복리성장률 — 양수 비율만 계산(음수 매출은 분수승 시 허수 → None 폴백)."""
            if start and start > 0 and end > 0 and periods >= 1:
                return round((end / start) ** (1 / periods) - 1, 4)
            return None

        # 추천 성장률(월) — 전년 동월 대비(YoY) 월환산: 계절성 상쇄.
        # 전년 동월 데이터 없으면 당해 추세 양끝(계절성 오염 가능) 폴백.
        prev_by_m = {m["month"][5:]: m["revenue"] for m in ptr}
        latest = [m for m in tr if m["revenue"]]
        g = None
        if latest and (ps := prev_by_m.get(latest[-1]["month"][5:])) and ps > 0:
            g = _cagr(latest[-1]["revenue"], ps, 12)
        if g is None and len(revs) >= 2:
            g = _cagr(revs[-1], revs[0], len(revs) - 1)
        if g is None:
            g = 0.0
        # 매출 변동계수(CV) — baseline 추세 신뢰도(낮을수록 안정)
        if len(revs) >= 2:
            mean = sum(revs) / len(revs)
            var = sum((r - mean) ** 2 for r in revs) / len(revs)
            cv = round((var ** 0.5) / mean, 3) if mean else 0.0
        else:
            cv = 0.0

        # 계절지수(전년 월별 매출 / 평균, 평균=1.0) — Jan..Dec
        prev_revs = [m["revenue"] for m in ptr]
        nz = [r for r in prev_revs if r]
        pmean = sum(nz) / len(nz) if nz else 0
        seasonality = [round(r / pmean, 3) if pmean and r else 1.0 for r in prev_revs]

        def _m(x):  # 월평균
            return round(x / months) if months else 0
        return {
            "yyyymm": yyyymm, "months_window": months,
            "monthly": {"revenue": _m(rev_w), "variable": _m(var_w), "fixed": _m(fixed_w),
                        "dep": _m(dep_w), "other": _m(other_w),
                        "net": _m(rev_w - inc["total_expense"])},
            "variable_ratio": round(var_w / rev_w, 4) if rev_w else 0,
            "suggested_growth": g, "revenue_cv": cv, "seasonality": seasonality,
            "start_month": (until.month % 12) + 1,
            "cash": fc["opening_cash"], "ar": ar["total"], "ap": ap["total"],
            "total_liabilities": bs["total_liabilities"], "equity": bs["equity"],
        }

    async def monthly_trend(self, session: AsyncSession, *, year: int) -> dict:
        """월별 수익·비용·순이익 추세 — 단일 쿼리 위임(StatementService.monthly_pl)."""
        return {"year": year, "months": await self._stmt.monthly_pl(session, year=year)}

    async def voucher_overview(self, session: AsyncSession, *,
                               date_from: date, date_to: date) -> dict:
        """검진권 운영 현황(집계) — his.voucher 전표 lineType별 발급/사용/환급/정산 + 선수금 잔액.

        개별 검진권 serial 단위 상태는 HIS 소유(ERP 미저장) — ERP는 회계 전표 집계만.
        거래금액=전표 차변 합, 건수=전표 수(source_ref=voucher:<lineType>:<id>).
        """
        from sqlalchemy import case

        from erp.finance.models import DealerSettlementCallback, Voucher, VoucherLine

        rows = (await session.execute(
            select(Voucher.source_ref, VoucherLine.side, VoucherLine.amount, Voucher.voucher_on)
            .join(VoucherLine, VoucherLine.voucher_id == Voucher.id)
            .where(Voucher.source_module == "his.voucher",
                   Voucher.voucher_on >= date_from, Voucher.voucher_on <= date_to))).all()
        agg: dict[str, dict] = {}
        monthly: dict[str, dict] = {}
        for source_ref, side, amount, von in rows:
            parts = (source_ref or "").split(":")
            lt = parts[1] if len(parts) >= 3 and parts[0] == "voucher" else "기타"
            a = agg.setdefault(lt, {"refs": set(), "amount": 0.0})
            a["refs"].add(source_ref)
            if side == "DEBIT":
                a["amount"] += float(amount)
                # 월별 추이 — 발급(purchase)·사용(redeem) 거래금액(차변)
                if lt in ("purchase", "redeem"):
                    m = monthly.setdefault(von.strftime("%Y-%m"), {"issued": 0.0, "redeemed": 0.0})
                    m["issued" if lt == "purchase" else "redeemed"] += float(amount)
        by_line = [{"line_type": lt, "count": len(d["refs"]), "amount": round(d["amount"])}
                   for lt, d in sorted(agg.items())]
        monthly_trend = [{"month": m, "issued": round(v["issued"]), "redeemed": round(v["redeemed"])}
                         for m, v in sorted(monthly.items())]

        # 선수금(2140) 잔액 = 발급(Cr) − 사용/환급(Dr) = 미사용 발급 잔액.
        # as_of(date_to) 시점 잔액 — voucher_on ≤ date_to 로 시점 고정(마트 재갱신 시 과거 월 안정).
        # 반전은 REVERSED+반전 POSTED 쌍이 반대 side로 자동 상계되므로 상태 무필터 순증감 유지.
        unredeemed = await session.scalar(
            select(func.coalesce(func.sum(case(
                (VoucherLine.side == "CREDIT", VoucherLine.amount),
                else_=-VoucherLine.amount)), 0))
            .select_from(VoucherLine).join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code == "2140", Voucher.voucher_on <= date_to))
        callbacks = {s: c for s, c in (await session.execute(
            select(DealerSettlementCallback.status, func.count())
            .group_by(DealerSettlementCallback.status))).all()}

        def _m(lt: str) -> dict:
            r = next((x for x in by_line if x["line_type"] == lt), None)
            return {"amount": r["amount"] if r else 0, "count": r["count"] if r else 0}

        return {
            "range": {"from": date_from.isoformat(), "to": date_to.isoformat()},
            "issued": _m("purchase"), "redeemed": _m("redeem"),
            "refunded": _m("refund"), "settled": _m("settlement"),
            "unredeemed_krw": int(unredeemed or 0),
            "by_line_type": by_line, "settlement_callbacks": callbacks,
            "monthly_trend": monthly_trend,
        }
