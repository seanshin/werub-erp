"""BI 마트 갱신 — analytics/finance/scm 집계를 재사용해 reporting 마트 11종을 멱등 upsert.

기획서 docs/01-plan/features/bi-tool-integration.plan.md §8·bi-enhancements.plan.md §0. 집계 이원화를
피하려 기존 서비스 메서드(executive·segment_report·audit summary·settlement.cash_flow/aging/cash_forecast·
statements.income_statement/financial_ratios·valuation·voucher_overview·hr_turnover)를 그대로 호출해
평탄 컬럼으로 매핑(PII 없음). 정기업무 analytics.bi_marts(야간·refresh_all_marts)가 당월·전월을
갱신(스냅샷성 마트는 당월만). BI는 reporting 마트만 읽는다(운영 테이블 직결 금지).
"""
import logging

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.kst import kst_today

from erp.analytics.audit_service import AuditService
from erp.analytics.mart_models import (
    AgingMart, AuditFlagsMart, CashflowMart, CashForecastMart, DeptPlMart, ExecKpiMart,
    HrTurnoverMart, IncomeFunctionMart, InventoryMart, ItCostMart, RatiosAnnualMart, VoucherMart,
)
from erp.analytics.service import AnalyticsService, _month_range
from erp.finance.settlement_service import SettlementService
from erp.finance.statements import StatementService
from erp.scm.valuation_service import ValuationService

logger = logging.getLogger("erp.analytics.marts")

# 재구성 불가 현재-스냅샷 필드 — 재고평가·자산 장부가는 lot 이동·감가 이력 재구성 엔진이 없고,
# 재무비율은 연말 기준이라 as_of 재현 불가. 마감월(<당월)에는 최초 적재값으로 동결해 소급 드리프트 차단
# (InventoryMart·CashForecastMart의 '당월 스냅샷 전용'과 동일 성격·전체검토 마트 as-of 불안정).
_EXEC_FROZEN_FIELDS = ("inventory_value", "asset_book_value",
                       "current_ratio", "debt_ratio_pct", "operating_margin_pct")
_EXEC_FROZEN_PAYLOAD = ("재고", "자산", "재무비율", "자금")


async def _is_closed_month(session: AsyncSession, yyyymm: str) -> bool:
    """마감월(스냅샷 동결) 여부 — 실제 회계마감(FiscalPeriod.status=CLOSED) 우선(G15).

    종전엔 '달력월 < 당월'만 봐 BI 동결과 실제 회계마감이 어긋날 수 있었다(마감 안 된 과거월이
    동결되거나, 당월에 마감했는데 미동결·재개월이 계속 동결). FiscalPeriod 기록이 있으면 그 status를
    따르고(CLOSED만 동결·OPEN/재개는 라이브), 기록이 없으면(정식 월마감 미운영) 종전 달력 프록시로 폴백.

    ⚠ KST 기준 필수(폴백) — 야간 잡은 KST로 months=[당월,전월]을 계산하는데 여기서 naive date.today()
    (prod 컨테이너=UTC)를 쓰면, 매월 1일 03:50 KST 발화 시 UTC는 아직 전월이라 방금 마감된 월이
    "당월(열림)"으로 오판돼 동결 스냅샷이 라이브 값으로 덮어써진다(월 경계 오작동)."""
    from erp.finance.models import FiscalPeriod
    status = await session.scalar(select(FiscalPeriod.status).where(FiscalPeriod.yyyymm == yyyymm))
    if status is not None:
        return status == "CLOSED"
    return yyyymm < kst_today().strftime("%Y-%m")


def _flatten_exec(ex: dict) -> dict:
    """executive() 중첩 출력 → 평탄 컬럼.

    ⚠ `.get()` 관용 — 소스 키 개명 시 조용히 None(다른 마트의 직접 인덱싱과 달리 KeyError 안 남).
    현재 키는 executive() 출력과 전수 일치(테스트 test_flatten_exec_maps_nested_keys는 합성 dict라
    실호출 회귀는 못 잡음). 키 변경 시 여기 매핑을 반드시 함께 수정할 것."""
    fin, hr, care = ex.get("재무", {}), ex.get("인사", {}), ex.get("진료", {})
    ins, ratio = ex.get("보험", {}), ex.get("재무비율", {})
    inv, ast, fit = ex.get("재고", {}), ex.get("자산", {}), ex.get("정합", {})
    return {
        "revenue": fin.get("수익"), "net_income": fin.get("당기순이익"),
        "total_assets": fin.get("자산총계"), "total_liabilities": fin.get("부채총계"),
        "ar_balance": fin.get("미수금잔액"),
        "headcount": hr.get("재직인원"), "labor_cost": hr.get("월인건비"),
        "turnover_pct": hr.get("이직률_%"),
        "patients": care.get("환자수"), "receptions": care.get("당월접수"),
        "deduction_loss": ins.get("삭감손실"),
        "current_ratio": ratio.get("유동비율"), "debt_ratio_pct": ratio.get("부채비율_%"),
        "operating_margin_pct": ratio.get("의료이익률_%"),
        "inventory_value": inv.get("재고자산평가"), "asset_book_value": ast.get("장부가총계"),
        "equity": fit.get("자본총계"), "bs_balanced": fit.get("재무상태표_균형"),
    }


async def refresh_exec_kpi(session: AsyncSession, *, yyyymm: str) -> dict:
    """경영진 KPI 마트 1개월 멱등 갱신 — executive() 재사용·평탄화 + 전체 payload upsert.

    reconstruct=True로 미수/미지급잔액을 as_of(월말) 시점 재구성 → 과거 월 재갱신해도 잔액 안정
    (aging과 동일·상태기반 소급 오염 해소, 전체검토 2026-07-03)."""
    ex = await AnalyticsService().executive(session, yyyymm=yyyymm, reconstruct=True)
    flat = _flatten_exec(ex)
    row = await session.get(ExecKpiMart, yyyymm)
    # 마감월 + 기존 행 → 재구성 불가 스냅샷 필드/서브블록은 최초 적재값 동결(라이브 재고·자산·비율
    # 소급 변경 차단). 재구성 가능 필드(수익·인원·환자·미수 등)는 계속 재집계(늦은 확정 반영).
    frozen, old_payload = {}, {}
    if row is not None and await _is_closed_month(session, yyyymm):
        frozen = {f: getattr(row, f) for f in _EXEC_FROZEN_FIELDS
                  if getattr(row, f) is not None}
        old_payload = row.payload or {}
    if row is None:
        row = ExecKpiMart(yyyymm=yyyymm)
        session.add(row)
    for k, v in flat.items():
        setattr(row, k, v)
    for f, v in frozen.items():          # 동결 필드 복원
        setattr(row, f, v)
    if frozen:                           # payload 스냅샷 서브블록도 기존값 유지
        for key in _EXEC_FROZEN_PAYLOAD:
            if key in old_payload:
                ex[key] = old_payload[key]
    row.payload = ex
    row.refreshed_at = func.now()       # 매 갱신 시각 DB 기준
    logger.info("exec_kpi 마트 갱신", extra={"ctx": {"yyyymm": yyyymm, "frozen": bool(frozen)}})
    return {"yyyymm": yyyymm, "refreshed": True}


async def refresh_dept_pl(session: AsyncSession, *, yyyymm: str) -> dict:
    """진료과별 손익 마트 1개월 갱신 — segment_report() 재사용·전건 delete-then-insert(멱등).

    부서 구성·집계가 월마다 달라질 수 있어 (yyyymm) 전건을 지우고 다시 적재한다(부분 upsert보다 단순·정확).
    """
    seg = await AnalyticsService().segment_report(session, yyyymm=yyyymm)
    # 마감월 → 자산(현재 NBV·재구성 불가)과 그로부터 파생된 보고부문 플래그(reportable)를 최초
    # 적재값으로 동결(감가·처분으로 과거 부문공시 자산·보고성이 소급 변경되던 것 차단). 수익·손익은
    # care_month 귀속이라 계속 재집계(늦은 확정 반영). 자산·reportable을 쌍으로 보존해 내부 정합 유지.
    frozen: dict[str, tuple] = {}
    if await _is_closed_month(session, yyyymm):
        existing = (await session.execute(
            select(DeptPlMart).where(DeptPlMart.yyyymm == yyyymm))).scalars().all()
        frozen = {r.dept_code: (r.assets, r.reportable) for r in existing if r.assets is not None}
    await session.execute(delete(DeptPlMart).where(DeptPlMart.yyyymm == yyyymm))
    for s in seg["segments"]:
        dc = s.get("dept_code") or "UNKNOWN"
        assets, reportable = s.get("assets"), s.get("reportable")
        if dc in frozen:
            assets, reportable = frozen[dc]
        session.add(DeptPlMart(
            yyyymm=yyyymm, dept_code=dc,
            department=s.get("department"), revenue=s.get("revenue"),
            profit=s.get("profit"), assets=assets, reportable=reportable))
    logger.info("dept_pl 마트 갱신", extra={"ctx": {"yyyymm": yyyymm, "n": len(seg["segments"])}})
    return {"yyyymm": yyyymm, "departments": len(seg["segments"])}


async def refresh_audit_flags(session: AsyncSession, *, yyyymm: str) -> dict:
    """감사·이상탐지 마트 1개월 멱등 갱신 — audit_service.summary() 재사용(월 범위)."""
    since, until = _month_range(yyyymm)
    s = await AuditService().summary(session, date_from=since, date_to=until)
    bf, du, ab = s["benford"], s["duplicates"], s["abnormal"]
    row = await session.get(AuditFlagsMart, yyyymm)
    if row is None:
        row = AuditFlagsMart(yyyymm=yyyymm)
        session.add(row)
    row.benford_verdict = bf["verdict"]
    row.benford_mad = bf["mad"]
    row.benford_sample_size = bf["sample_size"]
    row.duplicate_clusters = du["cluster_count"]
    row.duplicate_open = du["open_count"]
    row.duplicate_amount = du["duplicate_amount"]
    row.abnormal_flagged = ab["flagged_count"]
    row.abnormal_open = ab["open_count"]
    row.open_total = s["open_total"]
    row.payload = s
    row.refreshed_at = func.now()
    logger.info("audit_flags 마트 갱신", extra={"ctx": {"yyyymm": yyyymm, "open": s["open_total"]}})
    return {"yyyymm": yyyymm, "open_total": s["open_total"]}


async def refresh_cashflow(session: AsyncSession, *, yyyymm: str) -> dict:
    """월간 자금수지 마트 갱신 — settlement.cash_flow() 재사용·멱등 upsert."""
    since, until = _month_range(yyyymm)
    cf = await SettlementService().cash_flow(session, since=since, until=until)
    row = await session.get(CashflowMart, yyyymm)
    if row is None:
        row = CashflowMart(yyyymm=yyyymm)
        session.add(row)
    row.inflow, row.outflow, row.net = cf["inflow"], cf["outflow"], cf["net"]
    row.payload = cf.get("by_module", {})
    row.refreshed_at = func.now()
    return {"yyyymm": yyyymm, "net": cf["net"]}


async def refresh_aging(session: AsyncSession, *, yyyymm: str) -> dict:
    """채권·채무 연령분석 마트 갱신 — settlement.aging(AR·AP, reconstruct) 멱등 upsert((yyyymm,kind)).

    reconstruct=True로 **as_of(월말) 시점 재구성** — 발생일 ≤ 월말·수금/지급도 월말 이하 전표만 반영
    (GL 저널 복원). 과거 월을 나중에 재갱신해도 그 달 말 실잔액이 안정적 → BI 추세가 '월말 실잔액'을
    정확히 표시(전체검토 2026-07-03 상태기반 소급 오염 해소)."""
    _since, until = _month_range(yyyymm)
    settle = SettlementService()
    for kind in ("AR", "AP"):
        a = await settle.aging(session, as_of=until, kind=kind, reconstruct=True)
        b = a["buckets"]
        row = await session.get(AgingMart, (yyyymm, kind))
        if row is None:
            row = AgingMart(yyyymm=yyyymm, kind=kind)
            session.add(row)
        row.bucket_current, row.bucket_1_30 = b["current"], b["1-30"]
        row.bucket_31_60, row.bucket_61_90, row.bucket_90plus = b["31-60"], b["61-90"], b["90+"]
        row.total = a["total"]
        row.refreshed_at = func.now()
    return {"yyyymm": yyyymm, "kinds": ["AR", "AP"]}


async def refresh_income_function(session: AsyncSession, *, yyyymm: str) -> dict:
    """월간 기능별 손익 마트 갱신 — statements.income_statement().by_function 멱등 upsert."""
    since, until = _month_range(yyyymm)
    inc = await StatementService().income_statement(session, since=since, until=until)
    bf = inc["by_function"]
    row = await session.get(IncomeFunctionMart, yyyymm)
    if row is None:
        row = IncomeFunctionMart(yyyymm=yyyymm)
        session.add(row)
    row.total_revenue, row.total_expense = inc["total_revenue"], inc["total_expense"]
    row.net_income = inc["net_income"]
    row.cogs, row.sga, row.other = bf["cogs"], bf["sga"], bf["other"]
    row.gross_profit, row.operating_income = bf["gross_profit"], bf["operating_income"]
    row.refreshed_at = func.now()
    return {"yyyymm": yyyymm, "operating_income": bf["operating_income"]}


async def refresh_voucher(session: AsyncSession, *, yyyymm: str) -> dict:
    """월간 검진권 운영 마트 갱신 — analytics.voucher_overview() 재사용·멱등 upsert."""
    since, until = _month_range(yyyymm)
    vo = await AnalyticsService().voucher_overview(session, date_from=since, date_to=until)
    row = await session.get(VoucherMart, yyyymm)
    if row is None:
        row = VoucherMart(yyyymm=yyyymm)
        session.add(row)
    row.issued_amount = vo["issued"]["amount"]
    row.redeemed_amount = vo["redeemed"]["amount"]
    row.refunded_amount = vo["refunded"]["amount"]
    row.settled_amount = vo["settled"]["amount"]
    row.unredeemed_krw = vo["unredeemed_krw"]
    row.payload = {"by_line_type": vo.get("by_line_type", [])}
    row.refreshed_at = func.now()
    return {"yyyymm": yyyymm, "unredeemed_krw": vo["unredeemed_krw"]}


async def refresh_hr_turnover(session: AsyncSession, *, yyyymm: str) -> dict:
    """월간 진료과별 이직 마트 갱신 — analytics.hr_turnover().by_dept 전건 delete-then-insert(멱등)."""
    t = await AnalyticsService().hr_turnover(session, yyyymm=yyyymm, reconstruct=True)
    await session.execute(delete(HrTurnoverMart).where(HrTurnoverMart.yyyymm == yyyymm))
    for d in t["by_dept"]:
        session.add(HrTurnoverMart(
            yyyymm=yyyymm, department=(d.get("department") or "-"),
            active=d.get("active"), left=d.get("left"), rate=d.get("rate")))
    logger.info("hr_turnover 마트 갱신", extra={"ctx": {"yyyymm": yyyymm, "n": len(t["by_dept"])}})
    return {"yyyymm": yyyymm, "departments": len(t["by_dept"])}


async def refresh_cash_forecast(session: AsyncSession, *, yyyymm: str, months: int = 6) -> dict:
    """자금수지 예측 스냅샷 마트 갱신 — settlement.cash_forecast 재사용·(yyyymm) delete-then-insert.

    ⚠ **혼합 기준 스냅샷 — 백필 금지**: opening(현금잔액)은 as_of 시점으로 정확 재현되나, inflow/outflow는
    현재 열린 AR/AP(status!=COLLECTED/PAID)의 due_on 기준이라 '현시점' 값. 과거 yyyymm으로 as_of를 넣으면
    이미 수금/지급된 건이 양쪽에서 빠져 balance/min_balance가 체계적으로 편향 → **당월 스냅샷 전용**
    (야간 잡도 months[0]만 호출). 과거 재현 불가·InventoryMart와 동일 성격.
    """
    _since, until = _month_range(yyyymm)
    fc = await SettlementService().cash_forecast(session, as_of=until, months=months)
    await session.execute(delete(CashForecastMart).where(CashForecastMart.yyyymm == yyyymm))
    for i, p in enumerate(fc["projection"]):
        session.add(CashForecastMart(
            yyyymm=yyyymm, bucket_idx=i, label=p["label"],
            inflow=p["inflow"], outflow=p["outflow"],
            net=p["net"], balance=p["balance"]))
    logger.info("cash_forecast 마트 갱신", extra={"ctx": {
        "yyyymm": yyyymm, "buckets": len(fc["projection"]), "min_balance": fc["min_balance"]}})
    return {"yyyymm": yyyymm, "buckets": len(fc["projection"]), "min_balance": fc["min_balance"]}


async def refresh_ratios_annual(session: AsyncSession, *, year: int) -> dict:
    """연간 재무비율 마트 갱신 — statements.financial_ratios 재사용·멱등 upsert(year)."""
    r = await StatementService().financial_ratios(session, year=year)
    row = await session.get(RatiosAnnualMart, year)
    if row is None:
        row = RatiosAnnualMart(year=year)
        session.add(row)
    for k in ("current_ratio", "debt_ratio", "equity_ratio", "operating_margin",
              "labor_cost_ratio", "material_cost_ratio"):
        setattr(row, k, r[k])
    row.payload = r["basis"]
    row.refreshed_at = func.now()
    logger.info("ratios_annual 마트 갱신", extra={"ctx": {"year": year}})
    return {"year": year, "refreshed": True}


async def refresh_it_cost(session: AsyncSession, *, yyyymm: str) -> dict:
    """IT 비용(ITAM) 월간 마트 갱신 — cost_service 집계(OPEX·예산·GL 전기) 재사용·멱등 upsert.

    당월 실적/예산/전기현황은 현시점 상태 반영(전기 진행에 따라 변동) — 재갱신 시 최신값으로 덮어씀.
    """
    from erp.itam.cost_service import CostService
    cs = CostService()
    opex = await cs.opex_summary(session, yyyymm)
    var = await cs.budget_variance(session, yyyymm)
    post = await cs.posting_status(session, yyyymm)
    ym = opex["period"]
    row = await session.get(ItCostMart, ym)
    if row is None:
        row = ItCostMart(yyyymm=ym)
        session.add(row)
    row.total_krw = opex["total_krw"]
    row.budget_krw = var["total_budget_krw"]
    row.over_budget = var["total_over"]
    row.posted_krw = post["posted_krw"]
    row.unposted_krw = post["unposted_krw"]
    row.unposted_count = post["unposted"]
    row.service_count = post["posted"] + post["unposted"]
    row.payload = {"by_type": opex["by_type"], "by_cost_center": opex["by_cost_center"],
                   "by_vendor": opex["by_vendor"], "budget_rows": var["rows"]}
    row.refreshed_at = func.now()
    logger.info("it_cost 마트 갱신", extra={"ctx": {"yyyymm": ym, "total_krw": opex["total_krw"]}})
    return {"yyyymm": ym, "total_krw": opex["total_krw"]}


async def refresh_inventory(session: AsyncSession, *, yyyymm: str) -> dict:
    """재고 운영 스냅샷 마트 갱신 — valuation·shortage(현시점)·turnover(기간) 멱등 upsert.

    ⚠ valuation/shortage는 현시점 스냅샷 — 과거 yyyymm 백필 시에도 '현재' 값(과거 재현 불가).
    """
    since, until = _month_range(yyyymm)
    val_svc = ValuationService()
    val = await val_svc.valuation(session)
    sh = await val_svc.shortage(session)
    to = await val_svc.turnover(session, since=since, until=until)
    row = await session.get(InventoryMart, yyyymm)
    if row is None:
        row = InventoryMart(yyyymm=yyyymm)
        session.add(row)
    row.total_value = val["total_value"]
    row.shortage_count = sh["count"]
    row.dead_stock_count = to["dead_stock_count"]
    row.refreshed_at = func.now()
    return {"yyyymm": yyyymm, "total_value": val["total_value"]}


async def refresh_all_marts(session: AsyncSession, *, today=None) -> dict:
    """마트 12종 일괄 갱신 — 당월·전월 멱등 upsert(마감 후 재집계 반영·스냅샷성 마트는 당월만).

    ⚠ **마트별 세이브포인트 격리**(전체검토 2026-07-03): 한 마트 실패가 나머지를 롤백하지 않도록
    각 (refresh, yyyymm)을 begin_nested()로 감싸 성공분은 유지·실패분만 로그(무거운 집계 예외·
    Numeric 오버플로 1건으로 전량 24h stale 되던 폭발반경 제거). **커밋은 호출자 책임**
    (정기업무 러너의 잡별 커밋). 월경계는 KST 기준.
    """
    from datetime import date
    today = today or kst_today()
    prev = date(today.year - (today.month == 1), 12 if today.month == 1 else today.month - 1, 1)
    months = [today.strftime("%Y-%m"), prev.strftime("%Y-%m")]
    years = sorted({int(m[:4]) for m in months})   # 연초엔 당년+전년 비율 동시 갱신

    # 월별 마트 — 당월·전월 모두 재갱신(마감 후 재집계 반영).
    tasks: list[tuple[str, object]] = []
    for m in months:
        for fn in (refresh_exec_kpi, refresh_dept_pl, refresh_audit_flags, refresh_cashflow,
                   refresh_aging, refresh_income_function, refresh_voucher, refresh_hr_turnover,
                   refresh_it_cost):
            tasks.append((f"{fn.__name__}({m})", lambda s, fn=fn, m=m: fn(s, yyyymm=m)))
    # 스냅샷성 마트 — 당월만(현시점 값이라 전월 재갱신 시 과거 행이 '현재'로 덮여 추세 왜곡).
    tasks.append((f"refresh_inventory({months[0]})",
                  lambda s: refresh_inventory(s, yyyymm=months[0])))
    tasks.append((f"refresh_cash_forecast({months[0]})",
                  lambda s: refresh_cash_forecast(s, yyyymm=months[0])))
    for y in years:
        tasks.append((f"refresh_ratios_annual({y})", lambda s, y=y: refresh_ratios_annual(s, year=y)))

    ok, failed = 0, []
    for label, run in tasks:
        try:
            async with session.begin_nested():   # 마트별 세이브포인트 — 실패 격리
                await run(session)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed.append(label)
            logger.error("마트 갱신 실패 — 격리·계속",
                         extra={"ctx": {"mart": label, "err": str(exc)[:120]}})
    return {"ok": ok, "failed": failed}
