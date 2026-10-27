"""2026-10-09 전체 흐름 검토 2차 C/E — 자산 AP 옵트인·성과급 원천세 집계·INCLOCALTAX 카탈로그·
퇴직 정산 원천징수(A21·영수증)·만료/해지 계약 최종월 위탁료 회귀.

각 테스트는 수정 전 코드에서 실패함을 확인했다(되돌려 재현).
"""
import importlib.util
import pathlib
from datetime import date, datetime, timezone

from sqlalchemy import select, text

from erp.config import get_settings
from erp.finance.asset_service import AssetService
from erp.finance.models import Payable
from erp.hr.absence_service import AbsenceService
from erp.hr.compensation_service import CompensationService
from erp.hr.leave_service import LeaveService
from erp.hr.models import Employee, OffboardingSettlement, PayrollLine, PayrollRecord
from erp.hr.offboarding_service import OffboardingService
from erp.tax.service import TaxService

_CONF = datetime(2026, 5, 25, tzinfo=timezone.utc)


def _emp(no: str, **kw) -> Employee:
    d = dict(employee_no=no, name=f"직원{no}", hired_on=date(2024, 1, 1), job_group="STAFF",
             employment_type="REGULAR", source="ERP")
    d.update(kw)
    return Employee(**d)


# ── #1 HIS 장비 취득(기본 호출)은 Payable을 만들지 않는다 — 수기 엔드포인트만 옵트인 ─────────
async def test_asset_register_default_does_not_create_payable(session):
    # inbound_consumer(HIS equipment.acquired)와 같은 호출 형태: payment_method=PAYABLE, 옵트인 없음
    await AssetService().register(session, asset_no="HIS-EQ-1", name="이관 장비", category="MEDICAL",
                                  acquired_on=date(2021, 3, 1), acquisition_cost=50_000_000,
                                  useful_life_years=8, payment_method="PAYABLE")
    assert await session.scalar(select(Payable).where(
        Payable.source_ref == "asset-acq-HIS-EQ-1")) is None
    # 옵트인 시에는 생성(수기 취득 경로)
    await AssetService().register(session, asset_no="MAN-EQ-1", name="수기 장비", category="MEDICAL",
                                  acquired_on=date(2026, 6, 1), acquisition_cost=10_000_000,
                                  useful_life_years=5, create_payable=True)
    assert await session.scalar(select(Payable).where(
        Payable.source_ref == "asset-acq-MAN-EQ-1")) is not None


async def test_manual_asset_endpoint_opts_into_payable(session, monkeypatch):
    from erp.finance import router as fin_router
    captured: dict = {}

    class _A:
        id, asset_no = 1, "X"

    async def fake_register(_session, **kw):
        captured.update(kw)
        return _A()

    monkeypatch.setattr(fin_router._assets, "register", fake_register)
    monkeypatch.setattr(fin_router, "_require", lambda *a, **k: None)

    class _U:
        user_id = 1

    body = fin_router.AssetBody(asset_no="X", name="n", acquired_on=date(2026, 6, 1),
                                acquisition_cost=1_000_000, useful_life_years=5)
    await fin_router.register_asset(body, user=_U(), session=session)
    assert captured.get("create_payable") is True


# ── #2 정규 급여 미확정 직원의 성과급 원천세(외부 라인)도 신고 집계 ───────────────────────
async def test_withholding_counts_external_incentive_lines_without_confirmation(session):
    reg, inc_only = _emp("R2-W1"), _emp("R2-W2")
    session.add_all([reg, inc_only])
    await session.flush()
    r1 = PayrollRecord(employee_id=reg.id, yyyymm="2026-05", confirmed_at=_CONF)
    r2 = PayrollRecord(employee_id=inc_only.id, yyyymm="2026-05")   # 정규 급여 없음(미확정)
    session.add_all([r1, r2])
    await session.flush()
    session.add_all([
        PayrollLine(record_id=r1.id, item_code="BASE", item_kind="EARNING", amount=3_000_000),
        PayrollLine(record_id=r1.id, item_code="TAX", item_kind="DEDUCTION", amount=100_000),
        PayrollLine(record_id=r2.id, item_code="INCENTIVE", item_kind="EARNING", amount=1_000_000),
        PayrollLine(record_id=r2.id, item_code="INCTAX", item_kind="DEDUCTION", amount=30_000),
        PayrollLine(record_id=r2.id, item_code="INCLOCALTAX", item_kind="DEDUCTION", amount=3_000),
        # 미확정 레코드의 정규 TAX는 여전히 제외
        PayrollLine(record_id=r2.id, item_code="TAX", item_kind="DEDUCTION", amount=999_000)])
    await session.flush()
    w = await TaxService().withholding(session, yyyymm="2026-05")
    assert w["원천징수세액"] == 130_000          # 100,000 + 성과급 30,000 (미확정 TAX 999,000 제외)
    assert w["지방소득세"] == 3_000
    assert w["총지급액"] == 4_000_000
    assert w["인원"] == 2


# ── #3 0253: INCLOCALTAX 카탈로그 시드(멱등) ─────────────────────────────────────
async def test_migration_0253_seeds_inclocaltax_catalog(session):
    path = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"
    files = sorted(path.glob("0253_*.py"))
    assert files, "0253 마이그레이션 없음"
    spec = importlib.util.spec_from_file_location("mig0253", files[0])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0253" and mod.down_revision == "0252"

    conn = await session.connection()

    def _run(sync_conn):
        class _Op:   # add_column은 sqlite create_all이 이미 반영 — INSERT만 실제 실행
            def add_column(self, *a, **k):
                pass

            def execute(self, sql):
                sync_conn.execute(text(sql))
        mod.op = _Op()
        mod.upgrade()
        mod.upgrade()   # 멱등(ON CONFLICT DO NOTHING)

    await conn.run_sync(_run)
    rows = (await session.execute(text(
        "SELECT name, kind, display_order FROM hr.pay_item_catalog WHERE code='INCLOCALTAX'"))).all()
    assert rows == [("성과급 지방소득세", "DEDUCTION", 63)]


# ── #4 퇴직 정산 원천징수 보관 → A21 집계·ESS 영수증 ─────────────────────────────
async def _settled(session) -> Employee:
    emp = _emp("R2-OB", hired_on=date(2020, 1, 1))
    session.add(emp)
    await session.flush()
    await CompensationService().upsert(session, actor=1, employee_id=emp.id, base_pay=4_180_000)
    await AbsenceService().resign(session, actor=1, employee_id=emp.id,
                                  resigned_on=date(2026, 6, 30))
    await LeaveService().grant_annual(session, employee_id=emp.id, year=2026)
    await session.flush()
    await OffboardingService().settle(session, actor=1, employee_id=emp.id)
    return emp


async def test_offboarding_settle_stores_tax_and_withholding_has_a21(session):
    emp = await _settled(session)
    st = await session.scalar(select(OffboardingSettlement).where(
        OffboardingSettlement.employee_id == emp.id))
    assert float(st.retirement_income_tax) > 0 and float(st.retirement_local_tax) > 0
    assert st.leave_allowance_tax is not None and st.leave_allowance_local_tax is not None
    w = await TaxService().withholding(session, yyyymm="2026-06")
    a21 = next(p for p in w["구분"] if p["소득구분"] == "퇴직소득(A21)")
    assert a21["인원"] == 1 and a21["총지급액"] == float(st.severance_amount)
    assert a21["원천징수세액"] == float(st.retirement_income_tax)
    assert a21["지방소득세"] == float(st.retirement_local_tax)
    assert w["합계"]["원천징수세액"] >= float(st.retirement_income_tax)
    # 다른 달엔 없음
    w7 = await TaxService().withholding(session, yyyymm="2026-07")
    assert next(p for p in w7["구분"] if p["소득구분"] == "퇴직소득(A21)")["인원"] == 0


async def test_ess_withholding_receipt_includes_offboarding(session):
    from erp.integrations.ess_router import withholding_receipt
    emp = await _settled(session)
    st = await session.scalar(select(OffboardingSettlement).where(
        OffboardingSettlement.employee_id == emp.id))
    s = get_settings()
    old = s.his_webhook_key
    s.his_webhook_key = "ik"
    try:   # 확정 급여 없이 퇴직 정산만 있어도 발급
        out = await withholding_receipt(employeeNumber=emp.employee_no, year=2026,
                                        x_integration_key="ik", session=session)
    finally:
        s.his_webhook_key = old
    assert out["issued"] is True
    assert out["totalGross"] == float(st.leave_allowance)          # 연차수당=근로소득
    assert out["withheld"]["incomeTax"] == float(st.leave_allowance_tax or 0)
    assert out["retirement"]["severance"] == float(st.severance_amount)
    assert out["retirement"]["incomeTax"] == float(st.retirement_income_tax)


# ── #5 만료·해지 계약의 최종월 위탁료 발생 ─────────────────────────────────────
async def test_month_end_accrues_final_month_of_expired_and_terminated(session):
    from erp.app.month_end_service import MonthEndService
    from erp.scm.contract_service import OutsourcingContractService
    oc = OutsourcingContractService()
    expired = await oc.register(session, actor=1, vendor_name="급식", category="MEAL",
                                title="급식", monthly_amount=8_000_000,
                                start_on=date(2026, 1, 1), end_on=date(2026, 5, 31))
    term = await oc.register(session, actor=1, vendor_name="세탁", category="LAUNDRY",
                             title="세탁", monthly_amount=2_000_000,
                             start_on=date(2026, 1, 1), end_on=date(2026, 12, 31))
    expired.status = "EXPIRED"                                     # 일 배치가 만료 처리
    term.status, term.terminated_on = "TERMINATED", date(2026, 5, 15)   # 월중 해지
    await session.flush()
    out = await MonthEndService().run(session, yyyymm="2026-05")
    assert out["contract_accrued"] == 2
    refs = set((await session.scalars(select(Payable.source_ref).where(
        Payable.source_module == "contract"))).all())
    assert refs == {f"contract:{expired.id}:2026-05", f"contract:{term.id}:2026-05"}
    # 해지 후 그 이전/이후 달은 발생하지 않음(월마감이 실패하지도 않음)
    out4 = await MonthEndService().run(session, yyyymm="2026-04")
    out6 = await MonthEndService().run(session, yyyymm="2026-06")
    # 4월(계약기간 안)은 늦은 월마감이어도 발생 — 3차 검토 E(해지가 월마감보다 먼저면 전달분 유실 방지).
    # 6월(종료·해지 이후)은 발생 없음.
    assert out4["contract_accrued"] == 2 and out6["contract_accrued"] == 0
