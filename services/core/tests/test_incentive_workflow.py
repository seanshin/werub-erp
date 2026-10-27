"""성과급 등록·승인 워크플로 (P4-4) — DRAFT→REVIEWED→APPROVED(급여·전표)→취소(claw-back)."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.models import Voucher
from erp.hr.contract_service import MedicalContractService
from erp.hr.employee_service import EmployeeService
from erp.hr.incentive_service import IncentiveService
from erp.hr.models import PayrollLine, PayrollRecord
from erp.patient.models import Patient, Payment, Reception

emp_svc = EmployeeService()
contracts = MedicalContractService()
svc = IncentiveService()


async def _doctor_with_revenue(session, no, amount):
    d = await emp_svc.create(session, actor=1, employee_no=no, name="의사",
                             hired_on=date(2025, 1, 1), job_group="DOCTOR")
    await contracts.create(session, actor=1, employee_id=d.id, valid_from=date(2026, 1, 1),
                           base_pay=9_000_000, incentive_type="NET_COLLECTION", incentive_rate=20)
    pt = Patient(chart_no=f"P{no}", name="환자", birth_date=date(1990, 1, 1))
    session.add(pt)
    await session.flush()
    rc = Reception(patient_id=pt.id, dept_code="IM", doctor_id=d.id,
                   received_on=date(2026, 6, 5), source="HIS")
    session.add(rc)
    await session.flush()
    session.add(Payment(reception_id=rc.id, amount=amount, method="CARD",
                        paid_on=date(2026, 6, 5), status="PAID"))
    await session.flush()
    return d


async def test_full_workflow_to_approve(session):
    d = await _doctor_with_revenue(session, "DR-W1", 3_000_000)
    ic = await svc.register(session, actor=1, employee_id=d.id, period="2026-06")
    assert ic.status == "DRAFT" and float(ic.amount) == 600_000   # 3M×20%
    await svc.review(session, actor=2, calc_id=ic.id)
    assert ic.status == "REVIEWED"
    await svc.approve(session, actor=3, calc_id=ic.id)
    assert ic.status == "APPROVED" and ic.voucher_ref == f"incentive-{ic.id}-1"
    # PayrollLine(INCENTIVE) 반영
    rec = await session.scalar(select(PayrollRecord).where(
        PayrollRecord.employee_id == d.id, PayrollRecord.yyyymm == "2026-06"))
    line = await session.scalar(select(PayrollLine).where(
        PayrollLine.record_id == rec.id, PayrollLine.item_code == "INCENTIVE"))
    assert line and float(line.amount) == 600_000
    # 미지급 전표
    v = await session.scalar(select(Voucher).where(
        Voucher.source_module == "hr.incentive", Voucher.source_ref == ic.voucher_ref))
    assert v is not None


async def test_approve_requires_review(session):
    d = await _doctor_with_revenue(session, "DR-W2", 1_000_000)
    ic = await svc.register(session, actor=1, employee_id=d.id, period="2026-06")
    with pytest.raises(ValidationError, match="검토 선행"):
        await svc.approve(session, actor=1, calc_id=ic.id)


async def test_cancel_clawback_removes_line_and_reverses(session):
    d = await _doctor_with_revenue(session, "DR-W3", 2_000_000)
    ic = await svc.register(session, actor=1, employee_id=d.id, period="2026-06")
    await svc.review(session, actor=2, calc_id=ic.id)
    await svc.approve(session, actor=3, calc_id=ic.id)
    await svc.cancel(session, actor=3, calc_id=ic.id, reason="삭감 소급")
    assert ic.status == "CANCELED"
    rec = await session.scalar(select(PayrollRecord).where(
        PayrollRecord.employee_id == d.id, PayrollRecord.yyyymm == "2026-06"))
    line = await session.scalar(select(PayrollLine).where(
        PayrollLine.record_id == rec.id, PayrollLine.item_code == "INCENTIVE"))
    assert line is None                                  # 라인 제거
    orig = await session.scalar(select(Voucher).where(
        Voucher.source_module == "hr.incentive", Voucher.source_ref == ic.voucher_ref))
    assert orig is not None and orig.status == "REVERSED"   # 원 전표 역분개 처리
    rev = await session.scalar(select(Voucher).where(Voucher.reverses == orig.id))
    assert rev is not None                               # 역분개 전표(원 라인 정확 반대)


async def test_register_blocked_after_approve(session):
    d = await _doctor_with_revenue(session, "DR-W4", 1_000_000)
    ic = await svc.register(session, actor=1, employee_id=d.id, period="2026-06")
    await svc.review(session, actor=2, calc_id=ic.id)
    await svc.approve(session, actor=3, calc_id=ic.id)
    with pytest.raises(ValidationError, match="이미 승인"):
        await svc.register(session, actor=1, employee_id=d.id, period="2026-06")


async def test_dashboard_aggregates(session):
    d = await _doctor_with_revenue(session, "DR-DASH", 4_000_000)
    out = await svc.dashboard(session, period="2026-06")
    row = next(r for r in out["rows"] if r["employee_id"] == d.id)
    assert row["net_revenue"] == 4_000_000 and row["preview_amount"] == 800_000  # 20%
    assert row["calc_status"] is None                      # 아직 미등록
    assert out["totals"]["doctors"] >= 1 and out["totals"]["preview"] >= 800_000
    # 등록·승인 후 상태 반영
    ic = await svc.register(session, actor=1, employee_id=d.id, period="2026-06")
    await svc.review(session, actor=2, calc_id=ic.id)
    await svc.approve(session, actor=3, calc_id=ic.id)
    out2 = await svc.dashboard(session, period="2026-06")
    row2 = next(r for r in out2["rows"] if r["employee_id"] == d.id)
    assert row2["calc_status"] == "APPROVED" and row2["calc_amount"] == 800_000
    assert out2["totals"]["approved"] >= 800_000


async def test_incentive_flat_withholding_when_configured(session):
    """H1 — 급여 정책 FLAT 설정 시 성과급 원천징수(INCTAX 라인 + GL 예수금 CR). NONE이 기본."""
    from erp.finance.models import VoucherLine
    from erp.hr.payroll_settings import PayrollSettingsService
    await PayrollSettingsService().upsert(session, actor=1,
                                          incentive_withholding_method="FLAT",
                                          incentive_withholding_rate=0.033)
    d = await _doctor_with_revenue(session, "DR-WH", 3_000_000)      # 성과급 600,000
    ic = await svc.register(session, actor=1, employee_id=d.id, period="2026-06")
    await svc.review(session, actor=2, calc_id=ic.id)
    await svc.approve(session, actor=3, calc_id=ic.id)
    rec = await session.scalar(select(PayrollRecord).where(
        PayrollRecord.employee_id == d.id, PayrollRecord.yyyymm == "2026-06"))
    inctax = await session.scalar(select(PayrollLine).where(
        PayrollLine.record_id == rec.id, PayrollLine.item_code == "INCTAX"))
    assert inctax and float(inctax.amount) == round(600_000 * 0.033)   # 19,800
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == ic.voucher_ref))
    by_acc = {(ln.account_code, ln.side): float(ln.amount) for ln in
              (await session.execute(select(VoucherLine).where(
                  VoucherLine.voucher_id == v.id))).scalars()}
    assert by_acc[("5310", "DEBIT")] == 600_000        # 급여비용
    assert by_acc[("2150", "CREDIT")] == 19_800        # 예수금(원천징수)
    assert by_acc[("2110", "CREDIT")] == 580_200       # 미지급(순지급)


async def test_payroll_settings_validation(session):
    """급여 정책 설정 — FLAT 정률 범위(0~1) 검증·미설정 기본 NONE."""
    from erp.hr.payroll_settings import PayrollSettingsService
    svc_s = PayrollSettingsService()
    assert (await svc_s.effective(session))["incentive_withholding_method"] == "NONE"   # 기본
    with pytest.raises(ValidationError):
        await svc_s.upsert(session, actor=1, incentive_withholding_method="FLAT",
                           incentive_withholding_rate=1.5)                                # 범위 밖
    row = await svc_s.upsert(session, actor=1, incentive_withholding_method="FLAT",
                             incentive_withholding_rate=0.033)
    assert row.incentive_withholding_method == "FLAT"


async def test_reapprove_after_cancel_posts_fresh_gl(session):
    """심층 재검토 HIGH — 취소→재승인 시 신규 전표가 실제 전기되어야(멱등 충돌로 미전기되던 결함).

    posting_seq로 승인마다 source_ref가 달라져, 재승인 시 급여명세와 GL이 함께 반영된다.
    """
    from erp.finance.models import VoucherLine
    d = await _doctor_with_revenue(session, "DR-RE", 3_000_000)      # 성과급 600,000
    ic = await svc.register(session, actor=1, employee_id=d.id, period="2026-06")
    await svc.review(session, actor=2, calc_id=ic.id)
    await svc.approve(session, actor=3, calc_id=ic.id)
    assert ic.voucher_ref == f"incentive-{ic.id}-1"
    await svc.cancel(session, actor=3, calc_id=ic.id, reason="정정")
    # 재산정(같은 행 재사용)→검토→재승인
    ic2 = await svc.register(session, actor=1, employee_id=d.id, period="2026-06")
    assert ic2.id == ic.id and ic2.status == "DRAFT"
    await svc.review(session, actor=2, calc_id=ic.id)
    await svc.approve(session, actor=3, calc_id=ic.id)
    assert ic.voucher_ref == f"incentive-{ic.id}-2"                  # 신규 시퀀스
    v2 = await session.scalar(select(Voucher).where(
        Voucher.source_module == "hr.incentive", Voucher.source_ref == ic.voucher_ref))
    assert v2 is not None and v2.status != "REVERSED"               # 재승인 전표 실제 전기
    lines = {(ln.account_code, ln.side): float(ln.amount) for ln in
             (await session.execute(select(VoucherLine).where(
                 VoucherLine.voucher_id == v2.id))).scalars()}
    assert lines[("5310", "DEBIT")] == 600_000 and lines[("2110", "CREDIT")] == 600_000
    # 급여명세 INCENTIVE 라인도 존재(명세↔GL 정합)
    rec = await session.scalar(select(PayrollRecord).where(
        PayrollRecord.employee_id == d.id, PayrollRecord.yyyymm == "2026-06"))
    line = await session.scalar(select(PayrollLine).where(
        PayrollLine.record_id == rec.id, PayrollLine.item_code == "INCENTIVE"))
    assert line and float(line.amount) == 600_000


async def test_cancel_reverses_withholding_symmetrically(session):
    """심층 재검토 HIGH — 원천징수(2150) 포함 전표 취소 시 3계정 모두 정확 상쇄(유령잔액 없음)."""
    from erp.finance.models import VoucherLine
    from erp.hr.payroll_settings import PayrollSettingsService
    await PayrollSettingsService().upsert(session, actor=1,
                                          incentive_withholding_method="FLAT",
                                          incentive_withholding_rate=0.033)
    d = await _doctor_with_revenue(session, "DR-WHR", 3_000_000)     # 성과급 600,000·원천 19,800
    ic = await svc.register(session, actor=1, employee_id=d.id, period="2026-06")
    await svc.review(session, actor=2, calc_id=ic.id)
    await svc.approve(session, actor=3, calc_id=ic.id)
    await svc.cancel(session, actor=3, calc_id=ic.id, reason="claw-back")
    # 원 전표 + 역분개 전표 합산 → 계정별 순액 0(급여비용·예수금·미지급 모두)
    orig = await session.scalar(select(Voucher).where(
        Voucher.source_module == "hr.incentive", Voucher.source_ref == ic.voucher_ref))
    rev = await session.scalar(select(Voucher).where(Voucher.reverses == orig.id))
    net: dict[str, float] = {}
    for vid in (orig.id, rev.id):
        for ln in (await session.execute(select(VoucherLine).where(
                VoucherLine.voucher_id == vid))).scalars():
            sign = 1 if ln.side == "DEBIT" else -1
            net[ln.account_code] = net.get(ln.account_code, 0) + sign * float(ln.amount)
    assert net.get("5310", 0) == 0 and net.get("2150", 0) == 0 and net.get("2110", 0) == 0


async def test_incentive_combined_withholding(session):
    """H1 COMBINED — 상여 간이세액(당월 정규급여 합산) 원천징수."""
    from erp.hr.employee_service import PayrollService
    from erp.hr.payroll_settings import PayrollSettingsService
    await PayrollSettingsService().upsert(session, actor=1,
                                          incentive_withholding_method="COMBINED", combined_months=1)
    d = await _doctor_with_revenue(session, "DR-CM", 5_000_000)          # 성과급 1,000,000(20%)
    await PayrollService().upsert_lines(session, actor=1, employee_id=d.id, yyyymm="2026-06",
                                        lines=[{"item_code": "BASE", "item_kind": "EARNING",
                                                "amount": 4_000_000}])   # 당월 정규급여
    ic = await svc.register(session, actor=1, employee_id=d.id, period="2026-06")
    await svc.review(session, actor=2, calc_id=ic.id)
    await svc.approve(session, actor=3, calc_id=ic.id)
    rec = await session.scalar(select(PayrollRecord).where(
        PayrollRecord.employee_id == d.id, PayrollRecord.yyyymm == "2026-06"))
    inctax = await session.scalar(select(PayrollLine).where(
        PayrollLine.record_id == rec.id, PayrollLine.item_code == "INCTAX"))
    assert inctax is not None and float(inctax.amount) > 0               # 상여 간이세액 > 0
