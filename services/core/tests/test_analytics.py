"""경영분석 BI (FR-19 확장) — 경영 KPI 종합·월별 추세."""
from datetime import date

from erp.analytics.service import AnalyticsService
from erp.finance.models import Account
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.hr.models import Employee

svc = AnalyticsService()
vs = VoucherService()


async def _seed(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="4110", name="수익", kind="REVENUE"),
    ])
    await session.flush()
    await vs.create(session, source_module="manual", source_ref="rev1",
                    voucher_on=date(2026, 5, 10),
                    lines=[VoucherLineDTO("1110", "DEBIT", 100000),
                           VoucherLineDTO("4110", "CREDIT", 100000)])
    session.add_all([
        Employee(employee_no="E1", name="간호1", hired_on=date(2020, 1, 1),
                 department_id=1, job_group="NURSE"),
        Employee(employee_no="E2", name="간호2", hired_on=date(2020, 1, 1),
                 department_id=1, job_group="NURSE"),
    ])
    await session.flush()


async def test_overview_kpis(session):
    await _seed(session)
    o = await svc.overview(session, yyyymm="2026-05")
    assert o["재무"]["수익"] == 100000
    assert o["재무"]["당기순이익"] == 100000        # 비용 없음
    assert o["인사"]["재직인원"] == 2
    assert set(o.keys()) == {"yyyymm", "재무", "인사", "진료", "보험"}


async def test_overview_reconstruct_headcount_patients_as_of(session):
    """마트 as-of 안정 — reconstruct=True는 재직/환자를 월말 시점 재구성(이후 입·퇴사·신규환자
    가 과거 월 KPI를 소급 변경하지 않음). 미지정(라이브)은 현재 상태."""
    from erp.patient.models import Patient
    session.add_all([
        # 5월 이전 재직(2명) — 과거 월 KPI에 포함
        Employee(employee_no="H1", name="재직A", hired_on=date(2020, 1, 1), department_id=1),
        Employee(employee_no="H2", name="재직B", hired_on=date(2020, 1, 1), department_id=1),
        # 6월 입사 — 5월(월말) 시점엔 미재직
        Employee(employee_no="H3", name="6월입사", hired_on=date(2026, 6, 15), department_id=1),
        # 6월 퇴사 — 5월 시점엔 아직 재직
        Employee(employee_no="H4", name="6월퇴사", hired_on=date(2019, 1, 1),
                 resigned_on=date(2026, 6, 20), department_id=1),
    ])
    session.add_all([
        Patient(chart_no="P-1", name="5월환자", birth_date=date(1990, 1, 1)),
        Patient(chart_no="P-2", name="5월환자2", birth_date=date(1990, 1, 1)),
    ])
    await session.flush()
    # created_at은 server_default(now)=오늘(2026-07) — 6월 진료월 기준으론 5월말 이후 등록.
    # 6월 이후 등록 환자가 5월 KPI를 부풀리지 않는지 검증하려 created_at을 명시 조정.
    from sqlalchemy import update
    await session.execute(update(Patient).where(Patient.chart_no == "P-1")
                          .values(created_at=date(2026, 4, 1)))    # 5월 이전 등록
    await session.execute(update(Patient).where(Patient.chart_no == "P-2")
                          .values(created_at=date(2026, 6, 10)))   # 5월 이후 등록
    await session.flush()
    recon = await svc.overview(session, yyyymm="2026-05", reconstruct=True)
    assert recon["인사"]["재직인원"] == 3      # 재직A·B + 6월퇴사자(5월말 시점 재직)·6월입사 제외
    assert recon["진료"]["환자수"] == 1        # P-1만(P-2는 5월 이후 등록)
    live = await svc.overview(session, yyyymm="2026-05", reconstruct=False)
    assert live["인사"]["재직인원"] == 3       # 현재 재직(H1·H2·H3, H4는 퇴사) = 3
    assert live["진료"]["환자수"] == 2         # 현재 전체 환자


async def test_hr_turnover_reconstruct_active_as_of(session):
    """마트 as-of 안정 — hr_turnover reconstruct=True는 진료과 재직(active)을 월말 시점 재구성."""
    session.add_all([
        Employee(employee_no="R1", name="재직", hired_on=date(2020, 1, 1), department_id=1),
        Employee(employee_no="R2", name="12개월내퇴직", hired_on=date(2019, 1, 1),
                 resigned_on=date(2026, 3, 1), department_id=1),
        Employee(employee_no="R3", name="6월입사", hired_on=date(2026, 6, 15), department_id=1),
    ])
    await session.flush()
    from erp.common.models import Department
    session.add(Department(id=1, code="IM", name="내과"))
    await session.flush()
    t = await svc.hr_turnover(session, yyyymm="2026-05", reconstruct=True)
    im = next((d for d in t["by_dept"] if d["department"] == "내과"), None)
    assert im is not None and im["active"] == 1     # 5월말 재직=R1만(R3 6월입사 제외)


async def test_monthly_trend(session):
    await _seed(session)
    t = await svc.monthly_trend(session, year=2026)
    assert len(t["months"]) == 12
    may = next(m for m in t["months"] if m["month"] == "2026-05")
    assert may["revenue"] == 100000
    jan = next(m for m in t["months"] if m["month"] == "2026-01")
    assert jan["revenue"] == 0                       # 1월 전표 없음


async def test_department_pl_structure(session):
    await _seed(session)
    pl = await svc.department_pl(session, yyyymm="2026-05")
    assert "labor_by_department" in pl and "deduction_by_dept_code" in pl


async def test_sim_baseline(session):
    """시뮬레이션 기준선 — 월평균 손익·변동비율·현금/AR/AP·부채/자본 키."""
    await _seed(session)
    b = await svc.sim_baseline(session, yyyymm="2026-05")
    assert b["months_window"] == 6
    assert {"revenue", "variable", "fixed", "dep", "other", "net"} <= set(b["monthly"])
    assert {"variable_ratio", "suggested_growth", "revenue_cv", "cash", "ar", "ap",
            "total_liabilities", "equity", "seasonality", "start_month"} <= set(b)
    assert len(b["seasonality"]) == 12        # 전년 월별 계절지수(Jan..Dec)
    # 5월 수익 10만이 6개월 평균에 반영(>0)
    assert b["monthly"]["revenue"] > 0


async def test_sim_baseline_negative_latest_revenue_no_crash(session):
    """음수 매출(환불 초과)이 최신월일 때 분수승 허수 크래시 방지 — 성장률 0.0 폴백 (prod #500 회귀)."""
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="4110", name="수익", kind="REVENUE"),
    ])
    await session.flush()
    # 전년 동월(2025-05) 양수 매출 → YoY ps>0
    await vs.create(session, source_module="manual", source_ref="py-rev",
                    voucher_on=date(2025, 5, 10),
                    lines=[VoucherLineDTO("1110", "DEBIT", 100000),
                           VoucherLineDTO("4110", "CREDIT", 100000)])
    # 당해 최신월(2026-05) 환불 초과 → 순매출 음수(음수 base ** 1/12 = 허수였던 케이스)
    await vs.create(session, source_module="manual", source_ref="refund",
                    voucher_on=date(2026, 5, 10),
                    lines=[VoucherLineDTO("4110", "DEBIT", 50000),
                           VoucherLineDTO("1110", "CREDIT", 50000)])
    await session.flush()
    b = await svc.sim_baseline(session, yyyymm="2026-05")  # 크래시 없이 반환
    assert isinstance(b["suggested_growth"], float)
    assert b["suggested_growth"] == 0.0


async def test_hr_turnover(session):
    """이직률 분석 — 월별 입사/퇴직 + 진료과별 이직률."""
    from datetime import date as _d
    session.add_all([
        Employee(employee_no="T1", name="재직", hired_on=_d(2025, 8, 1), department_id=5, job_group="DOCTOR"),
        Employee(employee_no="T2", name="퇴직", hired_on=_d(2020, 1, 1), resigned_on=_d(2026, 3, 10),
                 department_id=5, job_group="NURSE", resign_reason="자발-이직"),
    ])
    await session.flush()
    out = await svc.hr_turnover(session, yyyymm="2026-05")
    assert len(out["monthly"]) == 13
    assert out["by_reason"] == [{"reason": "자발-이직", "count": 1}]
    assert next(m for m in out["monthly"] if m["month"] == "2026-03")["left"] == 1
    assert next(m for m in out["monthly"] if m["month"] == "2025-08")["joined"] == 1
    # 진료과별 — dept 5에 퇴직 1·재직 1 → 50%
    d5 = next(x for x in out["by_dept"] if x["active"] == 1 and x["left"] == 1)
    assert d5["rate"] == 50.0


async def test_sim_dept_baseline(session):
    """진료과별 시뮬 기준선 — 윈도우 월평균 수익·손익(dept_code별, 수익순)."""
    from erp.claims.models import ClaimBatch, ClaimStatement
    from erp.common.models import Department
    session.add(Department(id=11, name="내과", code="IM"))
    batch = ClaimBatch(care_month="2026-05", receipt_no="RB-1", notice_arrived_on=date(2026, 6, 1))
    session.add(batch)
    await session.flush()
    session.add_all([
        ClaimStatement(batch_id=batch.id, stmt_serial="001", dept_code="IM",
                       claimed_amt=600000, decided_amt=540000, adjusted_amt=60000),
        ClaimStatement(batch_id=batch.id, stmt_serial="002", dept_code="OS",
                       claimed_amt=300000, decided_amt=300000, adjusted_amt=0),
    ])
    await session.flush()
    out = await svc.sim_dept_baseline(session, yyyymm="2026-05")
    assert out["months_window"] == 6 and len(out["departments"]) == 2
    im = next(d for d in out["departments"] if d["dept_code"] == "IM")
    assert im["department"] == "내과"
    assert im["avg_revenue"] == round(540000 / 6)          # 윈도우(6개월) 월평균
    assert im["avg_deduction"] == round(60000 / 6)
    # 수익순 — IM(9만) 먼저, OS(5만)
    assert out["departments"][0]["dept_code"] == "IM"


async def test_sim_dept_baseline_cost_divisor(session):
    """원가 분모 보정 — 윈도우(6개월) 중 스냅샷 2개월만 있으면 월평균 원가는 합/2."""
    from erp.claims.models import ClaimBatch, ClaimStatement
    from erp.common.models import Department
    from erp.finance.models import CostAllocationSnapshot
    session.add(Department(id=21, name="내과", code="IM"))
    batch = ClaimBatch(care_month="2026-05", receipt_no="RC-1", notice_arrived_on=date(2026, 6, 1))
    session.add(batch)
    await session.flush()
    session.add(ClaimStatement(batch_id=batch.id, stmt_serial="001", dept_code="IM",
                               claimed_amt=600000, decided_amt=600000, adjusted_amt=0))
    # 윈도우(2025-12~2026-05) 중 2개월만 원가 스냅샷 적재(60만씩) → 월평균 원가=120만/2=60만
    session.add_all([
        CostAllocationSnapshot(period="2026-05", department_id=21, total_cost=600000),
        CostAllocationSnapshot(period="2026-04", department_id=21, total_cost=600000),
    ])
    await session.flush()
    out = await svc.sim_dept_baseline(session, yyyymm="2026-05")
    im = next(d for d in out["departments"] if d["dept_code"] == "IM")
    assert im["avg_cost"] == 600000          # 120만/2(스냅샷 개월) — 고정 6으로 나누면 20만(오답)


async def test_sim_baseline_seasonality_reflects_prior_year(session):
    """계절지수 — 전년 월별 매출 편차를 평균=1.0 기준으로 반영(고매출월>1, 저매출월<1)."""
    from erp.finance.service import VoucherLineDTO
    session.add_all([Account(code="1110", name="현금", kind="ASSET"),
                     Account(code="4110", name="수익", kind="REVENUE")])
    await session.flush()
    # 전년(2025): 3월 고매출, 9월 저매출
    for mon, amt in [(3, 300000), (9, 100000)]:
        await vs.create(session, source_module="manual", source_ref=f"py{mon}",
                        voucher_on=date(2025, mon, 10),
                        lines=[VoucherLineDTO("1110", "DEBIT", amt),
                               VoucherLineDTO("4110", "CREDIT", amt)])
    b = await svc.sim_baseline(session, yyyymm="2026-05")
    s = b["seasonality"]
    assert s[2] > 1.0 and s[8] < 1.0          # 3월(idx2) 고·9월(idx8) 저
    assert s[0] == 1.0                         # 데이터 없는 달은 중립(1.0)


async def test_sim_baseline_growth_yoy(session):
    """추천 성장률 — 전년 동월 대비(YoY) 월환산(계절성 상쇄)."""
    from erp.finance.service import VoucherLineDTO
    session.add_all([Account(code="1110", name="현금", kind="ASSET"),
                     Account(code="4110", name="수익", kind="REVENUE")])
    await session.flush()
    # 2025-05: 1억, 2026-05: 약 2.6배(전년 동월) → 월환산 g≈(2.6)^(1/12)-1>0
    await vs.create(session, source_module="manual", source_ref="r25",
                    voucher_on=date(2025, 5, 10),
                    lines=[VoucherLineDTO("1110", "DEBIT", 100000000),
                           VoucherLineDTO("4110", "CREDIT", 100000000)])
    await vs.create(session, source_module="manual", source_ref="r26",
                    voucher_on=date(2026, 5, 10),
                    lines=[VoucherLineDTO("1110", "DEBIT", 260000000),
                           VoucherLineDTO("4110", "CREDIT", 260000000)])
    b = await svc.sim_baseline(session, yyyymm="2026-05")
    assert b["suggested_growth"] > 0          # 전년 동월比 증가 → 양수
    assert b["suggested_growth"] < 0.1        # 월환산이라 과대하지 않음


async def test_department_lines_drilldown(session):
    """2단계 드릴 — 진료과(dept_code)·월 명세서별 청구/결정/삭감(삭감순)."""
    from erp.claims.models import ClaimBatch, ClaimStatement
    batch = ClaimBatch(care_month="2026-05", receipt_no="R-1", notice_arrived_on=date(2026, 6, 1))
    session.add(batch)
    await session.flush()
    session.add_all([
        ClaimStatement(batch_id=batch.id, stmt_serial="001", dept_code="IM",
                       claimed_amt=100000, decided_amt=90000, adjusted_amt=10000),
        ClaimStatement(batch_id=batch.id, stmt_serial="002", dept_code="IM",
                       claimed_amt=50000, decided_amt=20000, adjusted_amt=30000),
        ClaimStatement(batch_id=batch.id, stmt_serial="003", dept_code="OS",
                       claimed_amt=70000, decided_amt=70000, adjusted_amt=0),
    ])
    await session.flush()
    out = await svc.department_lines(session, yyyymm="2026-05", dept_code="IM")
    assert out["count"] == 2 and out["dept_code"] == "IM"
    # 삭감순 — 002(3만) 먼저, 001(1만)
    assert [r["stmt_serial"] for r in out["items"]] == ["002", "001"]
    assert out["items"][0]["deduction"] == 30000
    # 타 진료과(OS) 제외
    assert await svc.department_lines(session, yyyymm="2026-05", dept_code="OS")  # 1건
    assert (await svc.department_lines(session, yyyymm="2026-04", dept_code="IM"))["count"] == 0
