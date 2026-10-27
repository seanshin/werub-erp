"""경영진 분석 BI 마트 (reporting 스키마) — BI 도구 읽기전용 소스.

기획서 docs/01-plan/features/bi-tool-integration.plan.md §4·§5(P0). 운영 테이블 직결 대신
**PII 없는 집계 마트**에만 BI(읽기전용 롤 erp_bi)를 연결한다. analytics 서비스 출력을 평탄화한
헤드라인 컬럼 + 전체 스냅샷(payload). 갱신은 worker-batch 야간 잡(refresh_exec_kpi).
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from erp.db import Base


class ExecKpiMart(Base):
    """경영진 KPI 월간 마트 — analytics.executive() 평탄화 + payload 스냅샷(집계·PII 없음).

    yyyymm은 analytics 규약과 동일한 'YYYY-MM'(예 '2026-05'). BI는 평탄 컬럼으로 차트,
    payload(JSON)는 드릴/추후 평탄화용 전체 스냅샷.
    """
    __tablename__ = "mart_exec_kpi_monthly"
    __table_args__ = {"schema": "reporting"}

    yyyymm: Mapped[str] = mapped_column(String(7), primary_key=True)
    revenue: Mapped[float | None] = mapped_column(Numeric(18, 2))
    net_income: Mapped[float | None] = mapped_column(Numeric(18, 2))
    total_assets: Mapped[float | None] = mapped_column(Numeric(18, 2))
    total_liabilities: Mapped[float | None] = mapped_column(Numeric(18, 2))
    ar_balance: Mapped[float | None] = mapped_column(Numeric(18, 2))
    headcount: Mapped[int | None] = mapped_column(Integer)
    labor_cost: Mapped[float | None] = mapped_column(Numeric(18, 2))
    turnover_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))
    patients: Mapped[int | None] = mapped_column(Integer)
    receptions: Mapped[int | None] = mapped_column(Integer)
    deduction_loss: Mapped[float | None] = mapped_column(Numeric(18, 2))
    current_ratio: Mapped[float | None] = mapped_column(Numeric(10, 2))
    debt_ratio_pct: Mapped[float | None] = mapped_column(Numeric(8, 2))
    operating_margin_pct: Mapped[float | None] = mapped_column(Numeric(8, 2))
    inventory_value: Mapped[float | None] = mapped_column(Numeric(18, 2))
    asset_book_value: Mapped[float | None] = mapped_column(Numeric(18, 2))
    equity: Mapped[float | None] = mapped_column(Numeric(18, 2))
    bs_balanced: Mapped[bool | None] = mapped_column(Boolean)
    payload: Mapped[dict] = mapped_column(JSON)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class DeptPlMart(Base):
    """진료과별 손익 월간 마트 — analytics.segment_report() 기반(K-IFRS 부문: 수익·손익·자산).

    BI 부서 드릴다운용(exec KPI 마트는 월별 톱라인 스칼라, 본 마트는 부서 차원). 집계·PII 없음.
    월별 재집계는 (yyyymm) 전건 delete-then-insert(부서 구성 변동 흡수).
    """
    __tablename__ = "mart_dept_pl_monthly"
    __table_args__ = {"schema": "reporting"}

    yyyymm: Mapped[str] = mapped_column(String(7), primary_key=True)
    dept_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    department: Mapped[str | None] = mapped_column(String(80))
    revenue: Mapped[float | None] = mapped_column(Numeric(18, 2))
    profit: Mapped[float | None] = mapped_column(Numeric(18, 2))
    assets: Mapped[float | None] = mapped_column(Numeric(18, 2))
    reportable: Mapped[bool | None] = mapped_column(Boolean)   # K-IFRS 1108 보고부문 임계
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class AuditFlagsMart(Base):
    """월간 감사·이상탐지 마트 — audit_service.summary()(Benford·중복전표·비정상분개) 카운트.

    exec KPI/dept 마트에 없는 차원(감사·이상탐지). 집계·PII 없음(건수·점수·금액합만). BI '감사' 패널 소스.
    """
    __tablename__ = "mart_audit_flags_monthly"
    __table_args__ = {"schema": "reporting"}

    yyyymm: Mapped[str] = mapped_column(String(7), primary_key=True)
    benford_verdict: Mapped[str | None] = mapped_column(String(16))   # 부합도 판정
    benford_mad: Mapped[float | None] = mapped_column(Numeric(10, 5))  # 평균절대편차
    benford_sample_size: Mapped[int | None] = mapped_column(Integer)
    duplicate_clusters: Mapped[int | None] = mapped_column(Integer)   # 중복 후보 군집수
    duplicate_open: Mapped[int | None] = mapped_column(Integer)       # 미검토 중복
    duplicate_amount: Mapped[float | None] = mapped_column(Numeric(18, 2))
    abnormal_flagged: Mapped[int | None] = mapped_column(Integer)     # 비정상 분개 적발
    abnormal_open: Mapped[int | None] = mapped_column(Integer)        # 미검토 비정상
    open_total: Mapped[int | None] = mapped_column(Integer)           # 미검토 합(중복+비정상)
    payload: Mapped[dict] = mapped_column(JSON)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class CashflowMart(Base):
    """월간 자금수지 마트 — settlement.cash_flow(현금계정 유입·유출). 집계·PII 없음."""
    __tablename__ = "mart_cashflow_monthly"
    __table_args__ = {"schema": "reporting"}

    yyyymm: Mapped[str] = mapped_column(String(7), primary_key=True)
    inflow: Mapped[float | None] = mapped_column(Numeric(18, 2))      # 유입(현금 차변)
    outflow: Mapped[float | None] = mapped_column(Numeric(18, 2))     # 유출(현금 대변)
    net: Mapped[float | None] = mapped_column(Numeric(18, 2))         # 순현금
    payload: Mapped[dict] = mapped_column(JSON)                       # by_module 출처별
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class AgingMart(Base):
    """월말 채권·채무 연령분석 마트 — settlement.aging(AR/AP). (yyyymm,kind) 단위. 집계·PII 없음."""
    __tablename__ = "mart_aging_monthly"
    __table_args__ = {"schema": "reporting"}

    yyyymm: Mapped[str] = mapped_column(String(7), primary_key=True)
    kind: Mapped[str] = mapped_column(String(2), primary_key=True)    # AR(채권)·AP(채무)
    bucket_current: Mapped[float | None] = mapped_column(Numeric(18, 2))
    bucket_1_30: Mapped[float | None] = mapped_column(Numeric(18, 2))
    bucket_31_60: Mapped[float | None] = mapped_column(Numeric(18, 2))
    bucket_61_90: Mapped[float | None] = mapped_column(Numeric(18, 2))
    bucket_90plus: Mapped[float | None] = mapped_column(Numeric(18, 2))
    total: Mapped[float | None] = mapped_column(Numeric(18, 2))
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class IncomeFunctionMart(Base):
    """월간 기능별 손익 마트 — statements.income_statement.by_function(COGS·SGA·매출총이익·영업이익)."""
    __tablename__ = "mart_income_function_monthly"
    __table_args__ = {"schema": "reporting"}

    yyyymm: Mapped[str] = mapped_column(String(7), primary_key=True)
    total_revenue: Mapped[float | None] = mapped_column(Numeric(18, 2))
    total_expense: Mapped[float | None] = mapped_column(Numeric(18, 2))
    net_income: Mapped[float | None] = mapped_column(Numeric(18, 2))
    cogs: Mapped[float | None] = mapped_column(Numeric(18, 2))        # 매출원가
    sga: Mapped[float | None] = mapped_column(Numeric(18, 2))         # 판관비
    other: Mapped[float | None] = mapped_column(Numeric(18, 2))
    gross_profit: Mapped[float | None] = mapped_column(Numeric(18, 2))  # 매출총이익
    operating_income: Mapped[float | None] = mapped_column(Numeric(18, 2))  # 영업이익
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class VoucherMart(Base):
    """월간 검진권 운영 마트 — analytics.voucher_overview(발급·사용·환급·정산·미사용잔액). 집계·PII 없음."""
    __tablename__ = "mart_voucher_monthly"
    __table_args__ = {"schema": "reporting"}

    yyyymm: Mapped[str] = mapped_column(String(7), primary_key=True)
    issued_amount: Mapped[float | None] = mapped_column(Numeric(18, 2))
    redeemed_amount: Mapped[float | None] = mapped_column(Numeric(18, 2))
    refunded_amount: Mapped[float | None] = mapped_column(Numeric(18, 2))
    settled_amount: Mapped[float | None] = mapped_column(Numeric(18, 2))
    unredeemed_krw: Mapped[int | None] = mapped_column(Integer)       # 미사용 발급잔액(선수금)
    payload: Mapped[dict] = mapped_column(JSON)                       # by_line_type
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class HrTurnoverMart(Base):
    """월간 진료과별 이직 마트 — analytics.hr_turnover.by_dept(재직·퇴직·이직률). 집계·PII 없음."""
    __tablename__ = "mart_hr_turnover_monthly"
    __table_args__ = {"schema": "reporting"}

    yyyymm: Mapped[str] = mapped_column(String(7), primary_key=True)
    department: Mapped[str] = mapped_column(String(80), primary_key=True)
    active: Mapped[int | None] = mapped_column(Integer)              # 재직
    left: Mapped[int | None] = mapped_column(Integer)               # 최근12개월 퇴직
    rate: Mapped[float | None] = mapped_column(Numeric(6, 2))       # 이직률 %
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class CashForecastMart(Base):
    """자금수지 예측 스냅샷 마트 — settlement.cash_forecast(6개월, due_on 기반). 집계·PII 없음.

    예측은 현시점 미수/미지급 잔여로 산출되는 스냅샷 — 갱신 시점(yyyymm)별 캡처(과거 재현 불가,
    InventoryMart와 동일 성격). (yyyymm) 전건 delete-then-insert(버킷 수 고정이나 단순·정확).
    bucket_idx 0=연체·당월, 1..N=미래 월. opening_cash는 balance0-net0로 도출 가능해 미저장.
    """
    __tablename__ = "mart_cash_forecast"
    __table_args__ = {"schema": "reporting"}

    yyyymm: Mapped[str] = mapped_column(String(7), primary_key=True)
    bucket_idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str | None] = mapped_column(String(16))            # 연체·당월 | YYYY-MM
    inflow: Mapped[float | None] = mapped_column(Numeric(18, 2))     # 수금예정(AR due)
    outflow: Mapped[float | None] = mapped_column(Numeric(18, 2))    # 지급예정(AP due)
    net: Mapped[float | None] = mapped_column(Numeric(18, 2))
    balance: Mapped[float | None] = mapped_column(Numeric(18, 2))    # 누적 예상잔액
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class RatiosAnnualMart(Base):
    """연간 재무비율 마트 — statements.financial_ratios(유동·부채·자기자본·이익률·인건비율·재료비율).

    비율은 소수(0.xx) 그대로 저장(exec 마트의 _%와 달리 원 API 규약 유지). basis는 payload로 보존.
    분모 0이면 None(미산출) — BI에서 결측 처리.
    """
    __tablename__ = "mart_ratios_annual"
    __table_args__ = {"schema": "reporting"}

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    current_ratio: Mapped[float | None] = mapped_column(Numeric(10, 4))     # 유동비율
    debt_ratio: Mapped[float | None] = mapped_column(Numeric(10, 4))        # 부채비율
    equity_ratio: Mapped[float | None] = mapped_column(Numeric(10, 4))      # 자기자본비율
    operating_margin: Mapped[float | None] = mapped_column(Numeric(10, 4))  # 의료이익률(근사)
    labor_cost_ratio: Mapped[float | None] = mapped_column(Numeric(10, 4))  # 인건비율
    material_cost_ratio: Mapped[float | None] = mapped_column(Numeric(10, 4))  # 재료비율
    payload: Mapped[dict] = mapped_column(JSON)                             # basis(분자·분모 원값)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class InventoryMart(Base):
    """재고 운영 스냅샷 마트 — scm.valuation·shortage(현시점)·turnover(기간). 집계·PII 없음.

    valuation/shortage는 현시점 스냅샷이라 과거 재현 불가 → 갱신 시점(yyyymm)별 캡처(전진 누적).
    """
    __tablename__ = "mart_inventory_monthly"
    __table_args__ = {"schema": "reporting"}

    yyyymm: Mapped[str] = mapped_column(String(7), primary_key=True)
    total_value: Mapped[float | None] = mapped_column(Numeric(18, 2))   # 재고자산평가(현시점)
    shortage_count: Mapped[int | None] = mapped_column(Integer)         # 안전재고 미달 품목수
    dead_stock_count: Mapped[int | None] = mapped_column(Integer)       # 장기재고(기간)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class ItCostMart(Base):
    """IT 비용(ITAM) 월간 마트 — itam.cost_service 집계(OPEX·예산·GL 전기 현황). 집계·PII 없음.

    itam.service_costs/software_services/it_budgets 재사용(opex_summary·budget_variance·posting_status).
    payload에 유형·부서(코스트센터)·벤더별 분해 + 예산 대비 실적 드릴다운.
    """
    __tablename__ = "mart_it_cost_monthly"
    __table_args__ = {"schema": "reporting"}

    yyyymm: Mapped[str] = mapped_column(String(7), primary_key=True)
    total_krw: Mapped[float | None] = mapped_column(Numeric(18, 0))       # 당월 IT OPEX 실적
    budget_krw: Mapped[float | None] = mapped_column(Numeric(18, 0))      # 당월 총예산(미설정 None)
    over_budget: Mapped[bool | None] = mapped_column(Boolean)             # 총예산 초과
    posted_krw: Mapped[float | None] = mapped_column(Numeric(18, 0))      # GL 전기 완료 금액
    unposted_krw: Mapped[float | None] = mapped_column(Numeric(18, 0))    # 미전기 금액
    unposted_count: Mapped[int | None] = mapped_column(Integer)          # 미전기 건수
    service_count: Mapped[int | None] = mapped_column(Integer)           # 당월 비용 발생 서비스 수
    payload: Mapped[dict] = mapped_column(JSON)      # {by_type,by_cost_center,by_vendor,budget_rows}
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
