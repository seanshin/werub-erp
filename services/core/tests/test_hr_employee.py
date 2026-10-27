"""직원·발령·면허 플래그·급여 — FR-03/04/07/47."""
from datetime import date, timedelta

from sqlalchemy import select

from erp.common.models import User
from erp.common.security import hash_password
from erp.hr.employee_service import EmployeeService, LicenseService, PayrollService
from erp.hr.models import Appointment, PayrollLine

_emp_svc, _lic_svc, _pay_svc = EmployeeService(), LicenseService(), PayrollService()
TODAY = date(2026, 6, 7)


async def test_create_encrypts_phone_and_records_hire(session):
    emp = await _emp_svc.create(session, actor=1, employee_no="E001", name="홍길동",
                                hired_on=date(2026, 1, 1), phone="010-1234-5678")
    assert emp.phone_enc is not None and b"1234" not in emp.phone_enc  # 평문 미저장
    assert _emp_svc.decrypted_phone(emp) == "010-1234-5678"
    appt = (await session.execute(select(Appointment))).scalar_one()
    assert appt.appointment_type == "HIRE"


async def test_transfer_keeps_history_and_updates_current(session):
    emp = await _emp_svc.create(session, actor=1, employee_no="E002", name="이몽룡",
                                hired_on=date(2025, 1, 1), department_id=10)
    await _emp_svc.transfer(session, actor=1, employee_id=emp.id,
                            to_department_id=20, to_position="팀장",
                            effective_on=date(2026, 7, 1))
    assert emp.department_id == 20 and emp.position == "팀장"
    appts = (await session.execute(select(Appointment))).scalars().all()
    assert [a.appointment_type for a in appts] == ["HIRE", "TRANSFER"]
    assert appts[1].from_department_id == 10


async def test_license_expiry_targets_d30_d7(session):
    emp = await _emp_svc.create(session, actor=1, employee_no="E003", name="간호사A",
                                hired_on=date(2024, 1, 1))
    await _lic_svc.add(session, employee_id=emp.id, license_type="간호사",
                       license_no="RN-1", issued_on=None,
                       expires_on=TODAY + timedelta(days=30))
    await _lic_svc.add(session, employee_id=emp.id, license_type="BLS",
                       license_no="BLS-1", issued_on=None,
                       expires_on=TODAY + timedelta(days=15))   # D-30 밴드(7<15≤30) → catch-up 발화
    targets = {lic.license_no: m for lic, m in
               await _lic_svc.expiring_targets(session, today=TODAY)}
    assert targets == {"RN-1": 30, "BLS-1": 30}    # 둘 다 D-30 밀스톤 밴드


async def test_expired_license_blocks_flag_and_bumps_token(session):
    """FR-47 — 만료 면허 → license_flag 차단 + 토큰 즉시 무효화."""
    emp = await _emp_svc.create(session, actor=1, employee_no="E004", name="의사B",
                                hired_on=date(2020, 1, 1))
    user = User(email="doc@hospital.example.org", password_hash=hash_password("Secure!Pass12"),
                role="MANAGER", employee_id=emp.id)
    session.add(user)
    await session.flush()
    await _lic_svc.add(session, employee_id=emp.id, license_type="의사",
                       license_no="MD-1", issued_on=None,
                       expires_on=TODAY - timedelta(days=1))  # 만료
    tv = user.token_version
    n = await _lic_svc.sync_license_flags(session, today=TODAY)
    await session.refresh(user)
    assert n == 1 and user.license_flag_active is False
    assert user.token_version == tv + 1  # design §6.1 즉시 무효화


async def test_payroll_lines_and_confirm(session):
    emp = await _emp_svc.create(session, actor=1, employee_no="E005", name="직원C",
                                hired_on=date(2025, 1, 1))
    await _pay_svc.upsert_lines(session, actor=1, employee_id=emp.id, yyyymm="2026-06",
                                lines=[{"item_code": "BASE", "item_kind": "EARNING",
                                        "amount": 3_000_000},
                                       {"item_code": "TAX", "item_kind": "DEDUCTION",
                                        "amount": 150_000}])
    record = await _pay_svc.confirm(session, actor=1, employee_id=emp.id, yyyymm="2026-06")
    assert record.confirmed_at is not None
    lines = (await session.execute(select(PayrollLine))).scalars().all()
    assert len(lines) == 2


async def test_payroll_idor_contracts_and_incentive(session):
    """P0-2 — hr.payroll read STAFF(SELF) 행검증: 타인 보수/성과급 차단·전체뷰 ALL만."""
    import pytest

    from erp.common.models import User
    from erp.common.router import CurrentUser
    from erp.errors import ForbiddenError
    from erp.hr.models import Employee
    from erp.hr.router import (calculate_incentive, incentive_dashboard,
                               list_incentive, list_medical_contracts)
    me = Employee(employee_no="E-ME", name="본인", hired_on=date(2025, 1, 1),
                  job_group="DOCTOR", employment_type="REGULAR", source="ERP")
    other = Employee(employee_no="E-OT", name="타인", hired_on=date(2025, 1, 1),
                     job_group="DOCTOR", employment_type="REGULAR", source="ERP")
    session.add_all([me, other])
    await session.flush()
    u = User(email="me@x.example", password_hash="x", role="STAFF", employee_id=me.id)
    session.add(u)
    await session.flush()
    staff = CurrentUser(user_id=u.id, role="STAFF", dept_id=None, license_flag=True)

    await list_medical_contracts(employee_id=me.id, user=staff, session=session)   # 본인 OK
    with pytest.raises(ForbiddenError):                                            # 타인 계약 차단
        await list_medical_contracts(employee_id=other.id, user=staff, session=session)
    with pytest.raises(ForbiddenError):                                            # 타인 성과급 차단
        await calculate_incentive(employee_id=other.id, period="2026-06", user=staff, session=session)
    with pytest.raises(ForbiddenError):                                            # 전체 대시보드 차단
        await incentive_dashboard(period="2026-06", user=staff, session=session)
    with pytest.raises(ForbiddenError):                                            # 전체 목록 차단
        await list_incentive(period="2026-06", user=staff, session=session)


def test_payslip_rows_sort_and_fallback():
    """P0 — _payslip_rows: 카탈로그 한글명·표시순 정렬·미등록 코드 원본 폴백."""
    from erp.hr.models import PayItemCatalog, PayrollLine
    from erp.hr.router import _payslip_rows
    cat = {"OT": PayItemCatalog(code="OT", name="연장근로수당", kind="EARNING",
                                taxable=True, display_order=20),
           "BASE": PayItemCatalog(code="BASE", name="기본급", kind="EARNING",
                                  taxable=True, display_order=10)}
    lines = [PayrollLine(item_code="OT", item_kind="EARNING", amount=100),
             PayrollLine(item_code="BASE", item_kind="EARNING", amount=200),
             PayrollLine(item_code="XYZ", item_kind="EARNING", amount=5),      # 미등록 → order 100
             PayrollLine(item_code="TAX", item_kind="DEDUCTION", amount=50)]
    rows = _payslip_rows(lines, cat, "EARNING")
    assert [r["item_code"] for r in rows] == ["BASE", "OT", "XYZ"]             # 10<20<100
    assert rows[0]["item_name"] == "기본급" and rows[2]["item_name"] == "XYZ"   # 폴백=원본코드
    assert all("_order" not in r for r in rows)                                # 내부키 제거


async def test_payslip_catalog_names_and_paid_on(session):
    """P0 — confirm이 paid_on 기록·my_payroll이 카탈로그 한글명 병기·미등록 폴백."""
    from erp.common.models import User
    from erp.common.router import CurrentUser
    from erp.hr.models import PayItemCatalog
    from erp.hr.router import confirm_payroll, my_payroll
    emp = await _emp_svc.create(session, actor=1, employee_no="E0PS", name="명세직원",
                                hired_on=date(2025, 1, 1))
    user = User(email="ps@hospital.example.org", password_hash=hash_password("Secure!Pass12"),
                role="ADMIN", employee_id=emp.id)
    session.add_all([
        user,
        PayItemCatalog(code="BASE", name="기본급", kind="EARNING", taxable=True, display_order=10),
        PayItemCatalog(code="TAX", name="소득세", kind="DEDUCTION", taxable=False, display_order=60),
    ])
    await session.flush()
    await _pay_svc.upsert_lines(session, actor=1, employee_id=emp.id, yyyymm="2026-06",
                                lines=[{"item_code": "BASE", "item_kind": "EARNING", "amount": 3_000_000},
                                       {"item_code": "ZZZ", "item_kind": "EARNING", "amount": 50_000},
                                       {"item_code": "TAX", "item_kind": "DEDUCTION", "amount": 150_000}])
    cu = CurrentUser(user_id=user.id, role="ADMIN", dept_id=None, license_flag=True)
    out = await confirm_payroll(yyyymm="2026-06", employee_id=emp.id,
                                paid_on=date(2026, 6, 25), user=cu, session=session)
    assert out["paid_on"] == date(2026, 6, 25)
    res = await my_payroll(yyyymm="2026-06", user=cu, session=session)
    names = {ln["item_code"]: ln["item_name"] for ln in res["lines"]}
    assert names["BASE"] == "기본급" and names["TAX"] == "소득세"
    assert names["ZZZ"] == "ZZZ"                              # 미등록 코드 폴백
    assert res["paid_on"] == date(2026, 6, 25)


async def test_regenerate_preserves_incentive_line(session):
    """H2 — 급여 재생성(upsert_lines)이 승인된 INCENTIVE 라인을 보존(삭제 방지)."""
    from sqlalchemy import select as _sel

    from erp.hr.models import PayrollLine, PayrollRecord
    emp = await _emp_svc.create(session, actor=1, employee_no="E0INC", name="성과직원",
                                hired_on=date(2025, 1, 1))
    await _pay_svc.upsert_lines(session, actor=1, employee_id=emp.id, yyyymm="2026-06",
                                lines=[{"item_code": "BASE", "item_kind": "EARNING", "amount": 3_000_000}])
    rec = await session.scalar(_sel(PayrollRecord).where(PayrollRecord.employee_id == emp.id))
    session.add(PayrollLine(record_id=rec.id, item_code="INCENTIVE",
                            item_kind="EARNING", amount=500_000))   # 외부(성과급 승인) 라인
    await session.flush()
    await _pay_svc.upsert_lines(session, actor=1, employee_id=emp.id, yyyymm="2026-06",
                                lines=[{"item_code": "BASE", "item_kind": "EARNING", "amount": 3_200_000}])
    lines = (await session.execute(_sel(PayrollLine).where(
        PayrollLine.record_id == rec.id))).scalars().all()
    codes = {ln.item_code: float(ln.amount) for ln in lines}
    assert codes["BASE"] == 3_200_000              # 계산 라인 교체됨
    assert codes.get("INCENTIVE") == 500_000       # 성과급 보존(H2)


async def test_incentive_detail_renders_snapshot(session):
    """P2 — _incentive_detail이 승인 성과급 snapshot을 실적 배분 블록으로 재현(재계산 아님)."""
    from erp.hr.models import IncentiveCalculation
    from erp.hr.router import _incentive_detail
    emp = await _emp_svc.create(session, actor=1, employee_no="E0DOC", name="의사D",
                                hired_on=date(2024, 1, 1), job_category="DOCTOR",
                                staff_class="MEDICAL")
    session.add(IncentiveCalculation(
        employee_id=emp.id, period="2026-06", net_revenue=34_900_000, amount=5_235_000,
        status="APPROVED",
        snapshot={"incentive_type": "NET_COLLECTION", "rate_pct": 15,
                  "net_revenue": 34_900_000, "revenue": 45_000_000, "raw_amount": 5_235_000}))
    await session.flush()
    d = await _incentive_detail(session, emp.id, "2026-06")
    assert d["incentive_type"] == "NET_COLLECTION" and d["rate_pct"] == 15
    assert d["revenue"] == 45_000_000 and d["net_revenue"] == 34_900_000
    assert d["amount"] == 5_235_000
    assert "34,900,000" in d["formula"] and "15%" in d["formula"]
    assert await _incentive_detail(session, emp.id, "2026-07") is None    # 다른 월=None


async def test_confirm_posts_payroll_gl_and_reconciles(session):
    """급여 GL 전기(DR 5310/CR 2150 예수금/CR 2110 미지급·INCENTIVE 제외) + H3 대사·멱등."""
    from sqlalchemy import select as _sel

    from erp.finance.models import Voucher, VoucherLine
    emp = await _emp_svc.create(session, actor=1, employee_no="E0GL", name="GL직원",
                                hired_on=date(2025, 1, 1))
    await _pay_svc.upsert_lines(session, actor=1, employee_id=emp.id, yyyymm="2026-06",
                                lines=[{"item_code": "BASE", "item_kind": "EARNING", "amount": 3_000_000},
                                       {"item_code": "NP", "item_kind": "DEDUCTION", "amount": 135_000},
                                       {"item_code": "TAX", "item_kind": "DEDUCTION", "amount": 65_000}])
    rec = await _pay_svc.confirm(session, actor=1, employee_id=emp.id, yyyymm="2026-06")
    v = await session.scalar(_sel(Voucher).where(Voucher.source_ref == f"payroll:{rec.id}"))
    assert v is not None
    by_acc = {(ln.account_code, ln.side): float(ln.amount) for ln in
              (await session.execute(_sel(VoucherLine).where(
                  VoucherLine.voucher_id == v.id))).scalars()}
    assert by_acc[("5310", "DEBIT")] == 3_000_000        # 급여비용 = gross
    assert by_acc[("2150", "CREDIT")] == 200_000         # 예수금 = 공제(원천징수)
    assert by_acc[("2110", "CREDIT")] == 2_800_000       # 미지급 = 순지급
    r = await _pay_svc.reconcile(session, record=rec)    # H3 대사
    assert r["balanced"] and r["gl_salary_exp"] == 3_000_000 and r["gl_withholding"] == 200_000
    await _pay_svc.confirm(session, actor=1, employee_id=emp.id, yyyymm="2026-06")   # 재확정
    n = len((await session.execute(_sel(Voucher).where(
        Voucher.source_ref == f"payroll:{rec.id}"))).scalars().all())
    assert n == 1                                        # 멱등 — 전표 증식 없음


async def test_employer_burden_posting_when_enabled(session):
    """사업주 4대보험 부담금 GL 전기(설정 ON) — DR 복리후생비(5330)/CR 미지급금(2110)·차대 균형."""
    from sqlalchemy import select as _sel

    from erp.finance.models import Voucher, VoucherLine
    from erp.hr.payroll_settings import PayrollSettingsService
    await PayrollSettingsService().upsert(session, actor=1, incentive_withholding_method="NONE",
                                          employer_burden_posting=True,
                                          employer_ei_rate=0.0115, employer_wc_rate=0.007)
    emp = await _emp_svc.create(session, actor=1, employee_no="E0EB", name="부담직원",
                                hired_on=date(2025, 1, 1))
    await _pay_svc.upsert_lines(session, actor=1, employee_id=emp.id, yyyymm="2026-06",
                                lines=[{"item_code": "BASE", "item_kind": "EARNING", "amount": 3_000_000}])
    rec = await _pay_svc.confirm(session, actor=1, employee_id=emp.id, yyyymm="2026-06")
    v = await session.scalar(_sel(Voucher).where(Voucher.source_ref == f"payroll-employer:{rec.id}"))
    assert v is not None
    by_acc = {(ln.account_code, ln.side): float(ln.amount) for ln in
              (await session.execute(_sel(VoucherLine).where(
                  VoucherLine.voucher_id == v.id))).scalars()}
    assert by_acc[("5330", "DEBIT")] == by_acc[("2110", "CREDIT")] > 0   # 사업주부담·차대 균형
