"""W3.1 온보딩·ESS 연동 — 봉투 호환·출퇴근 수신·급여명세/연차잔여 조회."""
from datetime import date, datetime

import pytest
from sqlalchemy import select

from erp.config import get_settings
from erp.errors import ForbiddenError
from erp.hr.attendance import consume_attendance_staging
from erp.hr.models import (
    AttendanceSummary, Employee, GroupwareAttendanceStaging, LeaveGrant,
    LeaveRequest, PayrollLine, PayrollRecord,
)
from erp.integrations.ess_router import leave_balance, payslip, withholding_receipt
from erp.integrations.his_webhook_router import WebhookBody
from erp.integrations.inbound_consumer import _staff_attendance


def test_webhook_body_accepts_both_envelopes():
    # HIS W3 봉투 {event, ref, occurredAt}
    his = WebhookBody(**{"event": "staff.attendance_recorded", "source": "his",
                         "ref": "att:2026-0001:202606150901:CLOCK_IN",
                         "occurredAt": "2026-06-15T09:01:00+09:00", "payload": {"a": 1}})
    assert his.event_type == "staff.attendance_recorded"
    assert his.event_id == "att:2026-0001:202606150901:CLOCK_IN"
    # ERP 자체 표기 {event_type, event_id}
    erp = WebhookBody(event_type="billing.changed", event_id="E1")
    assert erp.event_type == "billing.changed" and erp.event_id == "E1"


def _emp(no: str, name: str, **kw) -> Employee:
    defaults = dict(employee_no=no, name=name, hired_on=date(2025, 1, 1),
                    job_group="STAFF", employment_type="REGULAR", source="HIS")
    defaults.update(kw)
    return Employee(**defaults)


async def test_attendance_clock_pairing_and_summary(session):
    emp = _emp("2026-0001", "김타임")
    session.add(emp)
    await session.flush()

    async def clock(at: str, type_: str, correction: str | None = None):
        await _staff_attendance(session, {
            "employeeNumber": "2026-0001", "at": at, "type": type_,
            "correctionOf": correction}, "staff.attendance_recorded", {})

    # 주간 근무: IN 09:01 → OUT 18:02 (동일 일자행 페어링)
    await clock("2026-06-15T09:01:00", "CLOCK_IN")
    await clock("2026-06-15T18:02:00", "CLOCK_OUT")
    # 야간 교대: 22:00 IN → 익일 06:00 OUT (전일 행에 페어링 — 자정 넘김)
    await clock("2026-06-16T22:00:00", "CLOCK_IN")
    await clock("2026-06-17T06:00:00", "CLOCK_OUT")

    rows = (await session.scalars(select(GroupwareAttendanceStaging)
                                  .order_by(GroupwareAttendanceStaging.work_on))).all()
    assert len(rows) == 2                                  # 일자행 2개 (17일 별도행 없음)
    # PG 레인은 진짜 UTC aware로 되읽힘 — raw .hour/.day 단언은 -9h 왜곡(KST 환산 후 판정·v1.259.1)
    from erp.common.kst import as_kst_wallclock as _kst
    assert _kst(rows[0].clock_out).hour == 18
    assert rows[1].work_on == date(2026, 6, 16) and _kst(rows[1].clock_out).day == 17

    # 정정(correctionOf) — 덮어쓰기
    await clock("2026-06-15T08:55:00", "CLOCK_IN", correction="att:orig")
    assert _kst(rows[0].clock_in).minute == 55

    # 월 재집계 — 근무 2일·야간 1회, P1-② 급여 자동산정의 입력이 됨
    out = await consume_attendance_staging(session)
    assert out["summaries"] >= 1
    summ = (await session.execute(
        select(AttendanceSummary).where(AttendanceSummary.employee_id == emp.id))).scalar_one()
    assert summ.work_days == 2 and summ.night_shift_count == 1


async def test_attendance_requires_fields(session):
    # 필수 필드 누락은 영구 실패 — 재시도 대상(ValueError)이 아닌 즉시 DEAD(ValidationError)로(시나리오 2회차)
    from erp.errors import ValidationError
    with pytest.raises(ValidationError):
        await _staff_attendance(session, {"employeeNumber": "X"}, "e", {})


async def _with_key(key, fn):
    s = get_settings()
    old = s.his_webhook_key
    s.his_webhook_key = key
    try:
        return await fn()
    finally:
        s.his_webhook_key = old


async def test_payslip_confirmed_only(session):
    emp = _emp("2026-0002", "박명세")
    session.add(emp)
    await session.flush()
    rec = PayrollRecord(employee_id=emp.id, yyyymm="2026-06",
                        confirmed_at=datetime(2026, 7, 1, 9, 0), confirmed_by=1)
    session.add(rec)
    await session.flush()
    session.add_all([
        PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING", amount=3_000_000),
        PayrollLine(record_id=rec.id, item_code="TAX", item_kind="DEDUCTION", amount=84_850),
    ])
    await session.flush()

    async def run():
        out = await payslip(employeeNumber="2026-0002", period="202606",
                            x_integration_key="ik", session=session)
        assert out["issued"] and out["gross"] == 3_000_000
        assert out["net"] == 3_000_000 - 84_850
        # 미확정 월 → issued=false
        miss = await payslip(employeeNumber="2026-0002", period="2026-07",
                             x_integration_key="ik", session=session)
        assert miss["issued"] is False
    await _with_key("ik", run)

    with pytest.raises(ForbiddenError):                    # 키 불일치
        await payslip(employeeNumber="2026-0002", period="202606",
                      x_integration_key="bad", session=session)


async def test_withholding_receipt_annual_aggregate(session):
    emp = _emp("2026-0004", "정영수")
    session.add(emp)
    await session.flush()
    for ym in ("2026-05", "2026-06"):
        rec = PayrollRecord(employee_id=emp.id, yyyymm=ym,
                            confirmed_at=datetime(2026, 7, 1), confirmed_by=1)
        session.add(rec)
        await session.flush()
        session.add_all([
            PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING",
                        amount=3_000_000),
            PayrollLine(record_id=rec.id, item_code="TAX", item_kind="DEDUCTION",
                        amount=80_000),
            PayrollLine(record_id=rec.id, item_code="NP", item_kind="DEDUCTION",
                        amount=135_000),
        ])
    await session.flush()

    async def run():
        out = await withholding_receipt(employeeNumber="2026-0004", year=2026,
                                        x_integration_key="ik", session=session)
        assert out["issued"] and out["totalGross"] == 6_000_000
        assert out["withheld"]["incomeTax"] == 160_000
        assert out["insurance"]["pension"] == 270_000
        assert len(out["months"]) == 2
        assert out["settlement"]["prepaidTax"] == 160_000
        # 데이터 없는 연도 → issued=false
        empty = await withholding_receipt(employeeNumber="2026-0004", year=2024,
                                          x_integration_key="ik", session=session)
        assert empty["issued"] is False
    await _with_key("ik", run)


async def test_leave_balance(session):
    emp = _emp("2026-0003", "최연차")
    session.add(emp)
    await session.flush()
    session.add(LeaveGrant(employee_id=emp.id, grant_year=2026, granted_days=15,
                           basis="근속"))
    session.add(LeaveRequest(employee_id=emp.id, leave_type="ANNUAL",
                             start_on=date(2026, 5, 4), end_on=date(2026, 5, 6),
                             days=3, status="APPROVED"))
    await session.flush()

    async def run():
        out = await leave_balance(employeeNumber="2026-0003", year=2026,
                                  x_integration_key="ik", session=session)
        item = out["items"][0]
        assert (item["granted"], item["used"], item["remaining"]) == (15.0, 3.0, 12.0)
    await _with_key("ik", run)


async def test_attendance_short_type_alias(session):
    """HIS 실송신 IN/OUT 축약형 수용 (2026-06-11 실측 편차)."""
    emp = _emp("DR-020", "닥터20")
    session.add(emp)
    await session.flush()
    await _staff_attendance(session, {
        "employeeNumber": "DR-020", "at": "2026-06-11T11:40:20", "type": "IN",
        "method": "BIO"}, "staff.attendance_recorded", {})
    await _staff_attendance(session, {
        "employeeNumber": "DR-020", "at": "2026-06-11T20:10:00", "type": "OUT"},
        "staff.attendance_recorded", {})
    row = (await session.scalars(select(GroupwareAttendanceStaging).where(
        GroupwareAttendanceStaging.employee_no == "DR-020"))).one()
    from erp.common.kst import as_kst_wallclock as _kst
    assert _kst(row.clock_in).hour == 11 and _kst(row.clock_out).hour == 20   # PG=UTC aware 정규화


async def test_payroll_identity_intake_readback_and_codes(session):
    """HIS 연동 R-1(수신·HMAC)·R-4(리드백 마스킹)·R-5(공제코드). 계좌·주민번호 암호화·기본급 보존."""
    import hashlib
    import hmac as _hmac
    import json as _json
    from datetime import timezone

    from erp.integrations.ess_router import (deduction_codes, payroll_identity_intake,
                                             payroll_identity_readback)

    emp = _emp("2026-0009", "김직원")
    session.add(emp)
    await session.flush()

    class _Req:
        def __init__(self, raw):
            self._raw = raw

        async def body(self):
            return self._raw

    async def run():
        s = get_settings()
        _orig_secret = s.inbound_webhook_secret
        s.inbound_webhook_secret = "bodysecret"      # 민감 인입은 전용 본문서명 키 필수(§4)
        codes = await deduction_codes(x_integration_key="ik")           # R-5
        assert any(c["code"] == "SPOUSE" for c in codes["items"])
        payload = {"employeeCode": "2026-0009", "residentId": "900101-1234567",
                   "bankName": "국민", "bankCode": "004", "bankAccount": "123-45-6789",
                   "holder": "김직원", "dependents": 3}
        raw = _json.dumps(payload).encode()
        ts = str(int(datetime.now(timezone.utc).timestamp()))
        sig = _hmac.new(b"bodysecret", ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
        out = await payroll_identity_intake(request=_Req(raw), x_integration_key="ik",   # R-1
                                            x_signature=sig, x_timestamp=ts, session=session)
        assert out["accepted"] and out["dependents"] == 3
        assert out["accountMasked"] == "***6789" and out["residentMasked"] == "900101-*******"
        with pytest.raises(ForbiddenError):                              # 잘못된 서명 거부
            await payroll_identity_intake(request=_Req(raw), x_integration_key="ik",
                                          x_signature="bad", x_timestamp=ts, session=session)
        # §4 fail-closed — 전용 서명키 미설정 시 정적키 폴백 없이 거부
        s.inbound_webhook_secret = None
        try:
            with pytest.raises(ForbiddenError, match="미설정"):
                await payroll_identity_intake(request=_Req(raw), x_integration_key="ik",
                                              x_signature=sig, x_timestamp=ts, session=session)
        finally:
            s.inbound_webhook_secret = "bodysecret"
        rb = await payroll_identity_readback(employeeNumber="2026-0009",  # R-4
                                             x_integration_key="ik", session=session)
        assert rb["registered"] and rb["accountMasked"] == "***6789"
        assert rb["residentMasked"] == "900101-*******" and rb["bankCode"] == "004"
        s.inbound_webhook_secret = _orig_secret     # 원복(교차 오염 방지)

    await _with_key("ik", run)


async def test_ess_duties_certificates_severance_performance(session):
    """개인포털 2차 R-8(당직)·R-10(증명서)·R-11(퇴직금)·R-9(성과급) ESS 노출."""
    from erp.hr.models import (CertificateIssue, DutyAssignment, DutySchedule,
                               MedicalStaffContract, PayrollLine)
    from erp.hr.models import PayrollRecord as PR
    from erp.integrations.ess_router import certificates, duties, performance, severance

    emp = _emp("2026-0055", "의사E", hired_on=date(2023, 1, 1),
               staff_class="MEDICAL", job_group="DOCTOR")
    session.add(emp)
    await session.flush()
    sched = DutySchedule(department_id=1, yyyymm="2026-07", status="PUBLISHED")
    session.add(sched)
    await session.flush()
    rec = PR(employee_id=emp.id, yyyymm="2026-07",
             confirmed_at=datetime(2026, 8, 1, 9, 0), confirmed_by=1)
    session.add_all([
        DutyAssignment(schedule_id=sched.id, employee_id=emp.id, duty_on=date(2026, 7, 5), shift="OC"),
        CertificateIssue(employee_id=emp.id, cert_type="EMPLOYMENT", issued_to=1, purpose="은행제출"),
        MedicalStaffContract(employee_id=emp.id, valid_from=date(2026, 1, 1),
                             effective_on=date(2026, 1, 1), base_pay=8_000_000,
                             incentive_type="NET_COLLECTION", incentive_rate=15,
                             status="ACTIVE", created_by=1),
        rec])
    await session.flush()
    session.add(PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING", amount=8_000_000))
    await session.flush()

    async def run():
        d = await duties(employeeNumber="2026-0055", from_=date(2026, 7, 1), to=date(2026, 7, 31),
                         x_integration_key="ik", session=session)
        assert len(d["items"]) == 1 and d["items"][0]["shift"] == "OC"        # R-8
        c = await certificates(employeeNumber="2026-0055", x_integration_key="ik", session=session)
        assert len(c["items"]) == 1 and c["items"][0]["certType"] == "EMPLOYMENT"  # R-10
        s = await severance(employeeNumber="2026-0055", x_integration_key="ik", session=session)
        # R-11 — 법정 평균임금 = 3개월 임금총액 ÷ 총일수 × 30(시나리오 S-C). 급여 record는 7월뿐 → 800만÷31×30
        assert s["avgMonthlyWage"] == round(8_000_000 / 31 * 30) and s["지급대상"] is True
        p = await performance(employeeNumber="2026-0055", period="202607",
                              x_integration_key="ik", session=session)
        assert p["contract"]["incentiveType"] == "NET_COLLECTION"             # R-9
        assert p["contract"]["incentiveRate"] == 15

    await _with_key("ik", run)


async def test_ess_target_token_authz(session):
    """#4 — X-Target-Token: 대상 일치 통과·불일치/만료 거부·enforce ON이면 필수."""
    import time as _t

    import jwt as _jwt

    from erp.config import get_settings
    from erp.integrations.ess_router import leave_balance
    s = get_settings()
    emp = _emp("T-100", "대상직원")
    session.add(emp)
    await session.flush()
    s.his_webhook_key = "ik"
    s.ess_target_token_secret = "targetsecret"
    _orig_enf = s.ess_target_token_enforce

    def _tok(sub, exp_delta=120, purpose="ess-target"):
        return _jwt.encode({"sub": sub, "purpose": purpose,
                            "exp": int(_t.time()) + exp_delta}, "targetsecret", algorithm="HS256")
    try:
        # 관측 모드(enforce OFF): 토큰 없어도 통과(정적키만)
        s.ess_target_token_enforce = False
        out = await leave_balance(employeeNumber="T-100", year=2026, x_integration_key="ik",
                                  x_target_token=None, session=session)
        assert out["employeeNumber"] == "T-100"
        # 대상 일치 토큰 통과
        out2 = await leave_balance(employeeNumber="T-100", year=2026, x_integration_key="ik",
                                   x_target_token=_tok("T-100"), session=session)
        assert out2["employeeNumber"] == "T-100"
        # 대상 불일치(다른 직원 토큰) 거부
        with pytest.raises(ForbiddenError, match="불일치"):
            await leave_balance(employeeNumber="T-100", year=2026, x_integration_key="ik",
                                x_target_token=_tok("T-999"), session=session)
        # 만료 토큰 거부
        with pytest.raises(ForbiddenError, match="검증 실패"):
            await leave_balance(employeeNumber="T-100", year=2026, x_integration_key="ik",
                                x_target_token=_tok("T-100", exp_delta=-10), session=session)
        # enforce ON: 토큰 없으면 거부
        s.ess_target_token_enforce = True
        with pytest.raises(ForbiddenError, match="필수"):
            await leave_balance(employeeNumber="T-100", year=2026, x_integration_key="ik",
                                x_target_token=None, session=session)
    finally:
        s.ess_target_token_enforce = _orig_enf
        s.ess_target_token_secret = None
