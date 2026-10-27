"""BI 마트 갱신 (경영진 KPI·진료과 손익) — 평탄화 매핑·멱등 upsert (기획서 BI P0)."""
from sqlalchemy import func, select

from erp.analytics.mart_models import (
    AgingMart, AuditFlagsMart, CashflowMart, DeptPlMart, ExecKpiMart, IncomeFunctionMart,
)
from erp.analytics.marts import (
    _flatten_exec, _is_closed_month, refresh_aging, refresh_audit_flags, refresh_cashflow,
    refresh_dept_pl, refresh_exec_kpi, refresh_hr_turnover, refresh_income_function,
    refresh_inventory, refresh_voucher,
)
from erp.analytics.service import AnalyticsService


async def test_is_closed_month_reflects_fiscal_status(session):
    """BI 동결 판정이 실제 회계마감(FiscalPeriod.status)을 우선 반영(G15)·기록 없으면 달력 폴백."""
    from erp.finance.models import FiscalPeriod
    # 기록 없는 과거월 → 달력 프록시로 동결(종전 동작 유지)
    assert await _is_closed_month(session, "2020-01") is True
    # OPEN 기록 → 과거월이어도 미동결(마감 안 됨·재개 포함)
    session.add(FiscalPeriod(yyyymm="2020-02", fiscal_year=2020, status="OPEN"))
    await session.flush()
    assert await _is_closed_month(session, "2020-02") is False
    # CLOSED 기록 → 동결
    session.add(FiscalPeriod(yyyymm="2020-03", fiscal_year=2020, status="CLOSED"))
    await session.flush()
    assert await _is_closed_month(session, "2020-03") is True


def test_flatten_exec_maps_nested_keys():
    """executive() 중첩 출력 → 평탄 컬럼 매핑(키 회귀 적발)."""
    ex = {
        "재무": {"수익": 1000, "당기순이익": 200, "자산총계": 5000, "부채총계": 3000,
                 "미수금잔액": 400},
        "인사": {"재직인원": 90, "월인건비": 600, "이직률_%": 4.5},
        "진료": {"환자수": 1200, "당월접수": 800},
        "보험": {"삭감손실": 50},
        "재무비율": {"유동비율": 1.8, "부채비율_%": 60.0, "의료이익률_%": 12.3},
        "재고": {"재고자산평가": 700}, "자산": {"장부가총계": 4200},
        "정합": {"자본총계": 2000, "재무상태표_균형": True},
    }
    f = _flatten_exec(ex)
    assert f["revenue"] == 1000 and f["net_income"] == 200 and f["ar_balance"] == 400
    assert f["headcount"] == 90 and f["turnover_pct"] == 4.5
    assert f["patients"] == 1200 and f["receptions"] == 800 and f["deduction_loss"] == 50
    assert f["current_ratio"] == 1.8 and f["operating_margin_pct"] == 12.3
    assert f["inventory_value"] == 700 and f["asset_book_value"] == 4200
    assert f["equity"] == 2000 and f["bs_balanced"] is True


def test_flatten_tolerates_missing_sections():
    """일부 섹션 결손 시 None 매핑(크래시 금지)."""
    f = _flatten_exec({"재무": {"수익": 10}})
    assert f["revenue"] == 10 and f["headcount"] is None and f["current_ratio"] is None


async def test_refresh_exec_kpi_creates_and_idempotent(session):
    """executive() 재사용 → 마트 1행 생성, 재실행 시 동일 PK upsert(중복 없음)."""
    out = await refresh_exec_kpi(session, yyyymm="2026-05")
    await session.flush()
    assert out == {"yyyymm": "2026-05", "refreshed": True}
    row = await session.get(ExecKpiMart, "2026-05")
    assert row is not None and row.payload is not None       # 전체 스냅샷 보존
    assert "재무" in row.payload                               # executive() 출력 구조
    # 재실행 — 동일 PK upsert(행 증가 없음)
    await refresh_exec_kpi(session, yyyymm="2026-05")
    await session.flush()
    cnt = await session.scalar(select(func.count()).select_from(ExecKpiMart))
    assert cnt == 1


async def test_exec_kpi_freezes_snapshot_for_closed_month(session, monkeypatch):
    """마트 as-of 안정 — 마감월의 재구성 불가 스냅샷(재고평가·자산장부가·비율)은 최초값 동결,
    당월은 라이브 반영. 재집계 가능 필드(수익 등)는 마감월에도 계속 갱신."""

    state = {"inv": 700, "asset": 4200, "cur": 1.8, "rev": 1000}

    async def _fake_exec(self, session, *, yyyymm, reconstruct=False):
        return {"재무": {"수익": state["rev"]}, "인사": {"재직인원": 90},
                "재고": {"재고자산평가": state["inv"]},
                "자산": {"장부가총계": state["asset"]},
                "재무비율": {"유동비율": state["cur"], "부채비율_%": None, "의료이익률_%": None}}

    monkeypatch.setattr(AnalyticsService, "executive", _fake_exec)
    # 마감월(2020-01은 당월보다 확실히 과거) 최초 적재
    await refresh_exec_kpi(session, yyyymm="2020-01")
    await session.flush()
    row = await session.get(ExecKpiMart, "2020-01")
    assert (float(row.inventory_value), float(row.asset_book_value),
            float(row.current_ratio)) == (700, 4200, 1.8)
    # 라이브 재고·자산·비율 변동 후 재갱신 → 스냅샷 동결, payload 서브블록도 동결, 수익은 재집계
    state.update(inv=999, asset=1, cur=9.9, rev=1500)
    await refresh_exec_kpi(session, yyyymm="2020-01")
    await session.flush()
    await session.refresh(row)
    assert (float(row.inventory_value), float(row.asset_book_value),
            float(row.current_ratio)) == (700, 4200, 1.8)
    assert row.payload["재고"]["재고자산평가"] == 700     # payload 스냅샷 동결
    assert float(row.revenue) == 1500                     # 재집계 필드는 갱신
    # 당월은 동결하지 않음 — 라이브 스냅샷 계속 반영 (마감월 판정과 동일한 KST 기준)
    from erp.common.kst import kst_today
    cur_mm = kst_today().strftime("%Y-%m")
    state.update(inv=555)
    await refresh_exec_kpi(session, yyyymm=cur_mm)
    await session.flush()
    cur_row = await session.get(ExecKpiMart, cur_mm)
    assert float(cur_row.inventory_value) == 555
    state.update(inv=333)
    await refresh_exec_kpi(session, yyyymm=cur_mm)
    await session.flush()
    await session.refresh(cur_row)
    assert float(cur_row.inventory_value) == 333          # 당월은 계속 갱신


async def test_dept_pl_freezes_assets_for_closed_month(session, monkeypatch):
    """마트 as-of 안정 — 마감월 부문 자산(현재 NBV·재구성 불가)·보고성 플래그 동결, 수익은 재집계."""
    seg = {"segments": [{"dept_code": "IM", "department": "내과", "revenue": 1000,
                         "profit": 200, "assets": 5000, "reportable": True}]}

    async def _fake(self, session, *, yyyymm):
        return seg

    monkeypatch.setattr(AnalyticsService, "segment_report", _fake)
    await refresh_dept_pl(session, yyyymm="2020-01")          # 마감월
    await session.flush()
    r = (await session.execute(
        select(DeptPlMart).where(DeptPlMart.yyyymm == "2020-01"))).scalars().one()
    assert r.assets == 5000
    # 감가·처분으로 자산 변동 + 늦은 확정으로 수익 변동 후 재갱신
    seg["segments"][0].update(assets=100, revenue=1500)
    await refresh_dept_pl(session, yyyymm="2020-01")
    await session.flush()
    r2 = (await session.execute(
        select(DeptPlMart).where(DeptPlMart.yyyymm == "2020-01"))).scalars().one()
    assert r2.assets == 5000                                  # 자산 동결
    assert r2.revenue == 1500                                 # 수익 재집계


async def test_refresh_dept_pl_upserts_and_idempotent(session, monkeypatch):
    """segment_report() 재사용 → 부서별 행, 동일 월 재실행 시 delete-then-insert(중복 없음)."""
    seg = {"segments": [
        {"dept_code": "IM", "department": "내과", "revenue": 1000, "profit": 200,
         "assets": 5000, "reportable": True},
        {"dept_code": "GS", "department": "외과", "revenue": 800, "profit": -50,
         "assets": 3000, "reportable": False},
    ]}

    async def _fake(self, session, *, yyyymm):
        return seg

    monkeypatch.setattr(AnalyticsService, "segment_report", _fake)
    out = await refresh_dept_pl(session, yyyymm="2026-05")
    await session.flush()
    assert out == {"yyyymm": "2026-05", "departments": 2}
    rows = (await session.execute(
        select(DeptPlMart).order_by(DeptPlMart.dept_code))).scalars().all()
    assert [r.dept_code for r in rows] == ["GS", "IM"]
    assert rows[1].revenue == 1000 and rows[1].reportable is True and rows[0].profit == -50
    # 재실행 — 전건 delete-then-insert(중복 없음)
    await refresh_dept_pl(session, yyyymm="2026-05")
    await session.flush()
    cnt = await session.scalar(
        select(func.count()).select_from(DeptPlMart).where(DeptPlMart.yyyymm == "2026-05"))
    assert cnt == 2


async def test_refresh_dept_pl_empty_no_rows(session):
    """부서 데이터 없으면 0행(크래시 없음)."""
    out = await refresh_dept_pl(session, yyyymm="2026-05")
    await session.flush()
    assert out["departments"] == 0
    assert await session.scalar(select(func.count()).select_from(DeptPlMart)) == 0


async def test_refresh_audit_flags_maps_summary(session, monkeypatch):
    """audit summary() 재사용 → 평탄 카운트 매핑·멱등 upsert."""
    from erp.analytics.audit_service import AuditService
    summary = {
        "range": {"from": "2026-05-01", "to": "2026-05-31"},
        "benford": {"verdict": "ACCEPTABLE", "mad": 0.012, "chi_square_exceeds": False,
                    "sample_size": 320},
        "duplicates": {"cluster_count": 3, "open_count": 2, "duplicate_amount": 150000},
        "abnormal": {"flagged_count": 5, "open_count": 4, "by_reason": {"weekend": 2}},
        "open_total": 6,
    }

    async def _fake(self, session, *, date_from, date_to, threshold=None):
        return summary

    monkeypatch.setattr(AuditService, "summary", _fake)
    out = await refresh_audit_flags(session, yyyymm="2026-05")
    await session.flush()
    assert out == {"yyyymm": "2026-05", "open_total": 6}
    row = await session.get(AuditFlagsMart, "2026-05")
    assert row.benford_verdict == "ACCEPTABLE" and row.benford_sample_size == 320
    assert row.duplicate_clusters == 3 and row.duplicate_open == 2
    assert row.abnormal_flagged == 5 and row.open_total == 6
    assert row.payload["abnormal"]["by_reason"] == {"weekend": 2}
    # 멱등 재실행
    await refresh_audit_flags(session, yyyymm="2026-05")
    await session.flush()
    assert await session.scalar(select(func.count()).select_from(AuditFlagsMart)) == 1


async def test_refresh_audit_flags_empty(session):
    """전표 없으면 크래시 없이 1행(카운트 0류)."""
    out = await refresh_audit_flags(session, yyyymm="2026-05")
    await session.flush()
    assert out["yyyymm"] == "2026-05"
    assert await session.get(AuditFlagsMart, "2026-05") is not None


# ── P0-d 확장 마트 (자금수지·aging·기능별손익) ──────────────
async def test_refresh_cashflow_maps_and_idempotent(session, monkeypatch):
    from erp.analytics.marts import SettlementService
    cf = {"since": "2026-05-01", "until": "2026-05-31", "inflow": 5000.0,
          "outflow": 3000.0, "net": 2000.0, "by_module": {"patient": {"inflow": 5000.0, "outflow": 0.0}}}

    async def _fake(self, session, *, since, until):
        return cf

    monkeypatch.setattr(SettlementService, "cash_flow", _fake)
    out = await refresh_cashflow(session, yyyymm="2026-05")
    await session.flush()
    assert out == {"yyyymm": "2026-05", "net": 2000.0}
    row = await session.get(CashflowMart, "2026-05")
    assert float(row.inflow) == 5000 and float(row.net) == 2000
    assert row.payload["patient"]["inflow"] == 5000.0
    await refresh_cashflow(session, yyyymm="2026-05")
    await session.flush()
    assert await session.scalar(select(func.count()).select_from(CashflowMart)) == 1


async def test_refresh_aging_ar_ap(session, monkeypatch):
    from erp.analytics.marts import SettlementService
    data = {"AR": {"buckets": {"current": 100.0, "1-30": 50.0, "31-60": 0.0, "61-90": 0.0, "90+": 10.0}, "total": 160.0},
            "AP": {"buckets": {"current": 200.0, "1-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}, "total": 200.0}}

    async def _fake(self, session, *, as_of, kind, source_module=None, reconstruct=False):
        assert reconstruct is True          # 마트는 as_of 재구성으로 호출
        return data[kind]

    monkeypatch.setattr(SettlementService, "aging", _fake)
    await refresh_aging(session, yyyymm="2026-05")
    await session.flush()
    rows = (await session.execute(select(AgingMart).order_by(AgingMart.kind))).scalars().all()
    assert [r.kind for r in rows] == ["AP", "AR"]
    ar = next(r for r in rows if r.kind == "AR")
    assert float(ar.bucket_current) == 100 and float(ar.bucket_90plus) == 10 and float(ar.total) == 160
    # 멱등(AR+AP 2행 유지)
    await refresh_aging(session, yyyymm="2026-05")
    await session.flush()
    assert await session.scalar(select(func.count()).select_from(AgingMart)) == 2


async def test_refresh_income_function(session, monkeypatch):
    from erp.analytics.marts import StatementService
    inc = {"since": "x", "until": "y", "total_revenue": 1000, "total_expense": 700, "net_income": 300,
           "revenues": [], "expenses": [],
           "by_function": {"cogs": 400, "sga": 200, "other": 100, "gross_profit": 600, "operating_income": 400}}

    async def _fake(self, session, *, since, until):
        return inc

    monkeypatch.setattr(StatementService, "income_statement", _fake)
    out = await refresh_income_function(session, yyyymm="2026-05")
    await session.flush()
    assert out == {"yyyymm": "2026-05", "operating_income": 400}
    row = await session.get(IncomeFunctionMart, "2026-05")
    assert float(row.cogs) == 400 and float(row.gross_profit) == 600 and float(row.operating_income) == 400


# ── P0-d(2) 확장 마트 (검진권·HR이직·재고) ──────────────────
async def test_refresh_voucher_maps(session, monkeypatch):
    vo = {"range": {}, "issued": {"amount": 1000, "count": 5}, "redeemed": {"amount": 600, "count": 3},
          "refunded": {"amount": 50, "count": 1}, "settled": {"amount": 400, "count": 2},
          "unredeemed_krw": 350, "by_line_type": [{"line_type": "purchase", "amount": 1000}]}

    async def _fake(self, session, *, date_from, date_to):
        return vo

    monkeypatch.setattr(AnalyticsService, "voucher_overview", _fake)
    out = await refresh_voucher(session, yyyymm="2026-05")
    await session.flush()
    assert out == {"yyyymm": "2026-05", "unredeemed_krw": 350}
    from erp.analytics.mart_models import VoucherMart
    row = await session.get(VoucherMart, "2026-05")
    assert float(row.issued_amount) == 1000 and float(row.redeemed_amount) == 600 and row.unredeemed_krw == 350
    assert row.payload["by_line_type"][0]["line_type"] == "purchase"


async def test_refresh_hr_turnover_dept(session, monkeypatch):
    t = {"yyyymm": "2026-05", "monthly": [],
         "by_dept": [{"department": "내과", "active": 10, "left": 2, "rate": 16.7},
                     {"department": "외과", "active": 8, "left": 1, "rate": 11.1}], "by_reason": []}

    async def _fake(self, session, *, yyyymm, reconstruct=False):
        return t

    monkeypatch.setattr(AnalyticsService, "hr_turnover", _fake)
    out = await refresh_hr_turnover(session, yyyymm="2026-05")
    await session.flush()
    assert out == {"yyyymm": "2026-05", "departments": 2}
    from erp.analytics.mart_models import HrTurnoverMart
    rows = (await session.execute(select(HrTurnoverMart).order_by(HrTurnoverMart.department))).scalars().all()
    assert [r.department for r in rows] == ["내과", "외과"]
    assert rows[0].active == 10 and float(rows[0].rate) == 16.7
    await refresh_hr_turnover(session, yyyymm="2026-05")  # 멱등
    await session.flush()
    assert await session.scalar(select(func.count()).select_from(HrTurnoverMart)) == 2


async def test_refresh_inventory_snapshot(session, monkeypatch):
    from erp.analytics.marts import ValuationService

    async def _to(self, session, *, since, until):
        return {"since": "x", "until": "y", "days": 30, "dead_stock_count": 2, "items": []}

    async def _val(self, s): return {"items": [], "total_value": 7000.0}
    async def _sh(self, s): return {"count": 3, "items": []}
    monkeypatch.setattr(ValuationService, "valuation", _val)
    monkeypatch.setattr(ValuationService, "shortage", _sh)
    monkeypatch.setattr(ValuationService, "turnover", _to)
    out = await refresh_inventory(session, yyyymm="2026-05")
    await session.flush()
    assert out == {"yyyymm": "2026-05", "total_value": 7000.0}
    from erp.analytics.mart_models import InventoryMart
    row = await session.get(InventoryMart, "2026-05")
    assert float(row.total_value) == 7000 and row.shortage_count == 3 and row.dead_stock_count == 2


async def test_refresh_cash_forecast_snapshot_and_idempotent(session, monkeypatch):
    """예측 버킷 delete-then-insert — 재실행 시 (yyyymm) 전건 교체(멱등)."""
    from erp.analytics.marts import SettlementService, refresh_cash_forecast
    fc = {"as_of": "2026-06-30", "opening_cash": 1000.0, "min_balance": 800.0,
          "projection": [
              {"label": "연체·당월", "inflow": 500.0, "outflow": 700.0, "net": -200.0, "balance": 800.0},
              {"label": "2026-07", "inflow": 900.0, "outflow": 300.0, "net": 600.0, "balance": 1400.0},
          ]}

    async def _fake(self, session, *, as_of, months=6):
        assert str(as_of) == "2026-06-30"   # as_of=해당 월말
        return fc

    monkeypatch.setattr(SettlementService, "cash_forecast", _fake)
    out = await refresh_cash_forecast(session, yyyymm="2026-06")
    await session.flush()
    assert out == {"yyyymm": "2026-06", "buckets": 2, "min_balance": 800.0}
    from erp.analytics.mart_models import CashForecastMart
    row0 = await session.get(CashForecastMart, ("2026-06", 0))
    row1 = await session.get(CashForecastMart, ("2026-06", 1))
    assert row0.label == "연체·당월" and float(row0.balance) == 800
    assert row1.label == "2026-07" and float(row1.net) == 600
    await refresh_cash_forecast(session, yyyymm="2026-06")   # 멱등
    await session.flush()
    assert await session.scalar(select(func.count()).select_from(CashForecastMart)) == 2


async def test_refresh_ratios_annual_maps_and_idempotent(session, monkeypatch):
    """연간 비율 6종 + basis payload 멱등 upsert. None(분모 0)도 그대로 저장."""
    from erp.analytics.marts import StatementService, refresh_ratios_annual
    r = {"year": 2026, "current_ratio": 1.85, "debt_ratio": 0.6, "equity_ratio": 0.62,
         "operating_margin": 0.123, "labor_cost_ratio": None, "material_cost_ratio": 0.08,
         "basis": {"revenue": 100000.0, "net_income": 12300.0, "equity": 62000.0}}

    async def _fake(self, session, *, year):
        assert year == 2026
        return r

    monkeypatch.setattr(StatementService, "financial_ratios", _fake)
    out = await refresh_ratios_annual(session, year=2026)
    await session.flush()
    assert out == {"year": 2026, "refreshed": True}
    from erp.analytics.mart_models import RatiosAnnualMart
    row = await session.get(RatiosAnnualMart, 2026)
    assert float(row.current_ratio) == 1.85 and float(row.operating_margin) == 0.123
    assert row.labor_cost_ratio is None                       # 분모 0 미산출 보존
    assert row.payload["revenue"] == 100000.0
    await refresh_ratios_annual(session, year=2026)           # 멱등
    await session.flush()
    assert await session.scalar(select(func.count()).select_from(RatiosAnnualMart)) == 1


async def test_refresh_it_cost_mart(session):
    """IT 비용 마트 — OPEX·예산·GL 전기 현황 평탄화 + 분해 payload·멱등."""
    from erp.analytics.mart_models import ItCostMart
    from erp.analytics.marts import refresh_it_cost
    from erp.common.router import CurrentUser
    from erp.itam.cost_service import CostService
    from erp.itam.router import ServiceBody, create_service

    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    cs = CostService()
    s1 = await create_service(ServiceBody(name="AWS", vendor="AWS", cost_center="영상의학",
                                          billing_model="USAGE_METERED"), user=admin, session=session)
    s2 = await create_service(ServiceBody(name="MS365", vendor="MS", cost_center="원무",
                                          billing_model="SEAT_RECURRING"), user=admin, session=session)
    await cs.record_cost(session, actor=1, service_id=s1["id"], period="2026-07", amount=300000)
    await cs.record_cost(session, actor=1, service_id=s2["id"], period="2026-07", amount=200000)
    await cs.upsert_budget(session, actor=1, period="2026-07", cost_center=None, amount_krw=400000)
    await cs.post_period(session, actor=1, period="2026-07")   # s1·s2 전기

    out = await refresh_it_cost(session, yyyymm="2026-07")
    assert out["total_krw"] == 500000
    row = await session.get(ItCostMart, "2026-07")
    assert row.total_krw == 500000 and row.budget_krw == 400000 and row.over_budget is True
    assert row.posted_krw == 500000 and row.unposted_krw == 0 and row.service_count == 2
    assert row.payload["by_vendor"]["AWS"] == 300000
    assert row.payload["by_cost_center"]["영상의학"] == 300000
    assert any(r["cost_center"] == "영상의학" for r in row.payload["budget_rows"])
    # 멱등 — 재갱신 시 단일 행
    await refresh_it_cost(session, yyyymm="2026-07")
    from sqlalchemy import func as _f, select as _s
    n = await session.scalar(_s(_f.count()).select_from(ItCostMart).where(ItCostMart.yyyymm == "2026-07"))
    assert n == 1


async def test_executive_cached_serves_from_mart_else_live(session):
    """성능검토 H-1 — executive_cached는 마트 적재분을 서빙(_source=mart), 미적재면 라이브 폴백."""
    svc = AnalyticsService()
    # 마트 미적재 → 라이브 계산
    live = await svc.executive_cached(session, yyyymm="2026-05")
    assert live["_source"] == "live" and "재무" in live
    # 마트 적재 후 → 마트 서빙(payload 동일 구조 + _source=mart)
    await refresh_exec_kpi(session, yyyymm="2026-05")
    await session.flush()
    cached = await svc.executive_cached(session, yyyymm="2026-05")
    assert cached["_source"] == "mart" and "재무" in cached
    assert cached.get("_refreshed_at") is not None


async def test_financial_ratios_cached_serves_from_mart(session):
    """성능검토 H-3 — analytics.financial_ratios_cached는 RatiosAnnualMart 서빙, 미적재면 라이브 폴백."""
    from erp.analytics.marts import refresh_ratios_annual
    svc = AnalyticsService()
    live = await svc.financial_ratios_cached(session, year=2026)
    assert live["_source"] == "live" and "current_ratio" in live and "basis" in live
    await refresh_ratios_annual(session, year=2026)
    await session.flush()
    cached = await svc.financial_ratios_cached(session, year=2026)
    assert cached["_source"] == "mart" and cached["year"] == 2026
    # 마트 서빙이 라이브와 동일 키 계약 유지(FE 회귀 방지)
    assert set(live) - {"_source", "_refreshed_at"} == set(cached) - {"_source", "_refreshed_at"}
