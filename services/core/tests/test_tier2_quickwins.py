"""내부운영 갭 감사 Tier2 quick win — 자금일보·면허 보수교육 추적·(예산초과 경보는 잡 로직 검증)."""
from datetime import date


from erp.finance.service import VoucherLineDTO, VoucherService
from erp.finance.settlement_service import SettlementService
from erp.hr.employee_service import LicenseService
from erp.hr.models import Employee

_vs = VoucherService()
_settle = SettlementService()
_lic = LicenseService()


# ── 자금일보 ───────────────────────────────────────────────
async def test_daily_cash_opening_flow_closing(session):
    """기초(전일까지)→당일 입출→기말 산식·출처별 병기."""
    # 전일 이전: 현금 100,000 유입(기초)
    await _vs.create(session, source_module="patient", source_ref="dc-1",
                     voucher_on=date(2026, 7, 18),
                     lines=[VoucherLineDTO("1110", "DEBIT", 100_000),
                            VoucherLineDTO("4110", "CREDIT", 100_000)])
    # 당일: 유입 30,000(수납) + 유출 20,000(지급)
    await _vs.create(session, source_module="patient", source_ref="dc-2",
                     voucher_on=date(2026, 7, 20),
                     lines=[VoucherLineDTO("1110", "DEBIT", 30_000),
                            VoucherLineDTO("4110", "CREDIT", 30_000)])
    await _vs.create(session, source_module="scm", source_ref="dc-3",
                     voucher_on=date(2026, 7, 20),
                     lines=[VoucherLineDTO("2110", "DEBIT", 20_000),
                            VoucherLineDTO("1110", "CREDIT", 20_000)])
    out = await _settle.daily_cash(session, on=date(2026, 7, 20))
    assert out["opening"] == 100_000
    assert out["inflow"] == 30_000 and out["outflow"] == 20_000
    assert out["closing"] == 110_000
    acc = next(a for a in out["accounts"] if a["account_code"] == "1110")
    assert acc["closing"] == 110_000
    assert out["by_module"]["patient"]["inflow"] == 30_000
    assert out["by_module"]["scm"]["outflow"] == 20_000


# ── 면허 보수교육 ──────────────────────────────────────────
async def _emp_with_license(session, *, trained_on):
    e = Employee(employee_no="D001", name="김의사", hired_on=date(2020, 1, 1))
    session.add(e)
    await session.flush()
    lic = await _lic.add(session, employee_id=e.id, license_type="의사",
                         license_no="MD-123", issued_on=date(2020, 1, 1), expires_on=None)
    await session.flush()
    if trained_on:
        await _lic.record_training(session, license_id=lic.id, completed_on=trained_on)
    return lic


async def test_training_due_targets_milestone_bands(session):
    """보수교육 기한 = 이수일+3년 — 밀스톤 밴드(catch-up): D-60=(30,60]·D-30=(0,30]·D-0=경과. NULL 제외."""
    lic = await _emp_with_license(session, trained_on=date(2023, 9, 18))
    due = date(2026, 9, 18)

    async def _hit(today):
        hits = await _lic.training_due_targets(session, today=today)
        return [(h[0].id, h[1], h[2]) for h in hits]

    assert await _hit(date(2026, 7, 20)) == [(lic.id, due, 60)]    # 잔여 60 → D-60
    assert await _hit(date(2026, 7, 21)) == [(lic.id, due, 60)]    # 잔여 59 → catch-up(구:미발화)
    assert await _hit(date(2026, 8, 19)) == [(lic.id, due, 30)]    # 잔여 30 → D-30
    assert await _hit(date(2026, 9, 1)) == [(lic.id, due, 30)]     # 잔여 17 → D-30 밴드
    assert await _hit(due) == [(lic.id, due, 0)]                   # 당일 → D-0
    assert await _hit(date(2026, 10, 1)) == [(lic.id, due, 0)]     # 경과 → D-0(잡 dedup으로 1회)
    assert await _hit(date(2026, 6, 1)) == []                      # 잔여 109 > 60 → 밴드 밖


async def test_expiring_targets_milestone_bands(session):
    """면허 만료 — 밀스톤 밴드: D-30=(7,30]·D-7=[0,7]·경과 제외."""
    lic = await _emp_with_license(session, trained_on=None)
    lic.expires_on = date(2026, 9, 1)
    await session.flush()

    async def _hit(today):
        return [(h[0].id, h[1]) for h in await _lic.expiring_targets(session, today=today)]

    assert await _hit(date(2026, 8, 2)) == [(lic.id, 30)]     # 잔여 30
    assert await _hit(date(2026, 8, 10)) == [(lic.id, 30)]    # 잔여 22 → catch-up
    assert await _hit(date(2026, 8, 25)) == [(lic.id, 7)]     # 잔여 7 → D-7
    assert await _hit(date(2026, 8, 30)) == [(lic.id, 7)]     # 잔여 2 → D-7 밴드
    assert await _hit(date(2026, 9, 2)) == []                 # 경과(잔여<0) → 제외


async def test_training_null_excluded_and_rerecord_rebases(session):
    """이수 기록 없는 면허 제외·재이수 시 기한 재기산."""
    lic = await _emp_with_license(session, trained_on=None)
    assert await _lic.training_due_targets(session, today=date(2026, 7, 20)) == []
    await _lic.record_training(session, license_id=lic.id, completed_on=date(2026, 7, 1))
    hits = await _lic.training_due_targets(session, today=date(2029, 5, 2))   # 잔여 60 → D-60
    assert hits and hits[0][1] == date(2029, 7, 1) and hits[0][2] == 60


# ── 예산초과 경보(잡 핵심 로직 = budget_execution status) ──────────────
async def test_budget_over_status_detected(session):
    """OVER 판정 — 집행+약정 > 예산이면 available<0·status=OVER(경보 잡 입력)."""
    from erp.finance.models import Budget
    from erp.finance.report_service import FinanceReportService
    session.add(Budget(department_id=1, fiscal_year=2026, account_code="5310", amount=50_000))
    await session.flush()
    await _vs.create(session, source_module="hr", source_ref="bo-1",
                     voucher_on=date(2026, 7, 1),
                     lines=[VoucherLineDTO("5310", "DEBIT", 80_000, department_id=1),
                            VoucherLineDTO("1110", "CREDIT", 80_000)])
    rows = await FinanceReportService().budget_execution(session, fiscal_year=2026)
    over = [r for r in rows if r["status"] == "OVER"]
    assert len(over) == 1 and over[0]["available"] == -30_000
