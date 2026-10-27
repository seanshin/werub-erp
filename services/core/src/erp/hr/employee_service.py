"""직원·발령·면허·급여·증명서 — FR-03/04/07/23/47."""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.months import require_ym
from erp.audit.service import AuditService
from erp.common import crypto
from erp.common.models import User
from erp.errors import ForbiddenError, ValidationError
from erp.hr.models import (
    Appointment, CertificateIssue, Employee, License, PayrollLine, PayrollRecord,
)

_audit = AuditService()
_MEDICAL_GROUPS = {"DOCTOR", "RESIDENT", "NURSE", "TECH"}


def _class_of(job_group: str) -> str:
    """job_group → 직군 휴리스틱 (의료직/일반직)."""
    return "MEDICAL" if job_group in _MEDICAL_GROUPS else "SUPPORT"


class EmployeeService:
    async def create(self, session: AsyncSession, *, actor: int, employee_no: str, name: str,
                     hired_on: date, department_id: int | None = None,
                     position: str | None = None, job_group: str = "STAFF",
                     birth_date: date | None = None, phone: str | None = None,
                     address: str | None = None, staff_class: str | None = None,
                     job_category: str | None = None, employment_type: str = "REGULAR",
                     contract_start: date | None = None, contract_end: date | None = None,
                     daily_wage: float | None = None) -> Employee:
        # ERP 생성 = ERP 관리 직원 (source=ERP). HIS 관리(의료진·정규직)는 webhook 미러 (SoT §3.5)
        emp = Employee(employee_no=employee_no, name=name, hired_on=hired_on,
                       department_id=department_id, position=position,
                       job_group=job_group, birth_date=birth_date, source="ERP",
                       staff_class=staff_class or _class_of(job_group),
                       job_category=job_category or job_group,
                       employment_type=employment_type, contract_start=contract_start,
                       contract_end=contract_end, daily_wage=daily_wage)
        session.add(emp)
        await session.flush()
        # 컬럼 암호화 — AAD에 PK 바인딩 (design §6.4): flush 후 id 확정 시 암호화
        if phone:
            emp.phone_enc = crypto.encrypt_column(
                phone, aad=crypto.column_aad("hr.employees.phone", emp.id))
        if address:
            emp.address_enc = crypto.encrypt_column(
                address, aad=crypto.column_aad("hr.employees.address", emp.id))
        session.add(Appointment(employee_id=emp.id, appointment_type="HIRE",
                                to_department_id=department_id, to_position=position,
                                effective_on=hired_on))
        # 4대보험 취득 신고 추적 (P1-③) — 지연 import(모듈 순환 방지)
        from erp.hr.insurance_report_service import InsuranceReportService
        await InsuranceReportService().record(session, employee_id=emp.id,
                                              event="ACQUIRE", basis_on=hired_on)
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="hr.employees", target_id=str(emp.id),
                                  after={"employee_no": employee_no, "name": name})
        return emp

    async def transfer(self, session: AsyncSession, *, actor: int, employee_id: int,
                       to_department_id: int | None, to_position: str | None,
                       effective_on: date, note: str | None = None) -> Appointment:
        """발령 (FR-03) — 이력 기록 + 현재 상태 갱신."""
        emp = await self._get(session, employee_id)
        if emp.source == "HIS":     # SoT 가드 — HIS 관리 직원 인사변경은 HIS에서 (§3.5)
            raise ForbiddenError("HIS 관리 직원(의료진·정규직)은 ERP에서 발령 불가 — HIS에서 변경")
        appt = Appointment(
            employee_id=employee_id, appointment_type="TRANSFER",
            from_department_id=emp.department_id, to_department_id=to_department_id,
            from_position=emp.position, to_position=to_position,
            effective_on=effective_on, note=note)
        session.add(appt)
        before = {"department_id": emp.department_id, "position": emp.position}
        emp.department_id, emp.position = to_department_id, to_position
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="hr.employees", target_id=str(employee_id),
                                  before=before,
                                  after={"department_id": to_department_id,
                                         "position": to_position})
        return appt

    def decrypted_phone(self, emp: Employee) -> str | None:
        if emp.phone_enc is None:
            return None
        return crypto.decrypt_column(
            emp.phone_enc, aad=crypto.column_aad("hr.employees.phone", emp.id))

    async def _get(self, session: AsyncSession, employee_id: int) -> Employee:
        emp = (await session.execute(
            select(Employee).where(Employee.id == employee_id))).scalar_one_or_none()
        if emp is None:
            raise ValidationError("존재하지 않는 직원입니다")
        return emp


class LicenseService:
    EXPIRY_ALERTS = (30, 7)  # D-30 / D-7 (FR-04)

    async def add(self, session: AsyncSession, *, employee_id: int, license_type: str,
                  license_no: str, issued_on: date | None,
                  expires_on: date | None) -> License:
        lic = License(employee_id=employee_id, license_type=license_type,
                      license_no=license_no, issued_on=issued_on, expires_on=expires_on)
        session.add(lic)
        return lic

    async def expiring_targets(self, session: AsyncSession, *,
                               today: date) -> list[tuple[License, int]]:
        """만료 임박 면허 — 밀스톤 밴드(catch-up). (License, 밀스톤일) 목록.

        정확 당일 등식(expires_on==today+d)은 스케줄러가 하루라도 거르면 그 날 대상 경보가 영구
        유실. 밴드로 전환: D-30 밀스톤=7<잔여≤30·D-7 밀스톤=0≤잔여≤7. 누락 시 다음 실행이 따라잡음.
        중복 발화는 잡의 알림 dedup(같은 밀스톤 이미 통지 시 skip)이 차단. 만료 경과(잔여<0)는
        sync_license_flags(권한 차단)가 담당하므로 제외."""
        alerts = sorted(self.EXPIRY_ALERTS, reverse=True)   # (30, 7)
        rows = (await session.execute(
            select(License).where(License.expires_on.is_not(None),
                                  License.expires_on >= today))).scalars().all()
        out: list[tuple[License, int]] = []
        for lic in rows:
            days = (lic.expires_on - today).days
            for i, d in enumerate(alerts):
                lower = alerts[i + 1] if i + 1 < len(alerts) else -1   # 다음(더 임박) 밀스톤 경계
                if lower < days <= d:
                    out.append((lic, d))
                    break
        return out

    # 보수교육(면허 신고) 주기 — 의료법 §25 면허신고 3년(보수교육 이수 전제). 이수일+3년=차기 기한.
    TRAINING_CYCLE_YEARS = 3
    TRAINING_ALERTS = (60, 30, 0)   # D-60/D-30/당일 — 교육 이수 준비기간 필요(만료 D-30/7보다 이름)

    async def training_due_targets(self, session: AsyncSession, *,
                                   today: date) -> list[tuple[License, date, int]]:
        """보수교육 기한 임박 면허 — 밀스톤 밴드(catch-up). (License, 차기기한, 밀스톤일) 목록.

        기한 = training_completed_on + 3년(의료법 면허신고 주기). 이수 기록 없는 면허(NULL)는 기한
        산정 불가라 제외. 정확 당일 등식은 스케줄러 누락 시 경보 유실 → 밴드로 전환: D-60=30<잔여≤60·
        D-30=0<잔여≤30·D-0=잔여≤0(경과 포함·한 번). 누락 시 다음 실행 따라잡음·중복은 잡 dedup 차단.
        """
        alerts = sorted(self.TRAINING_ALERTS, reverse=True)   # (60, 30, 0)
        rows = (await session.execute(
            select(License).where(License.training_completed_on.is_not(None)))).scalars().all()
        out: list[tuple[License, date, int]] = []
        for lic in rows:
            base = lic.training_completed_on
            try:
                due = base.replace(year=base.year + self.TRAINING_CYCLE_YEARS)
            except ValueError:                      # 2/29 → 2/28 (윤년 안전)
                due = base.replace(year=base.year + self.TRAINING_CYCLE_YEARS, day=28)
            days = (due - today).days
            for i, d in enumerate(alerts):
                if d == 0:
                    if days <= 0:                   # 당일·경과(dedup으로 1회)
                        out.append((lic, due, 0))
                    break
                lower = alerts[i + 1]               # 다음(더 임박) 밀스톤 경계
                if lower < days <= d:
                    out.append((lic, due, d))
                    break
        return out

    async def record_training(self, session: AsyncSession, *, license_id: int,
                              completed_on: date) -> License:
        """보수교육 이수 기록 — 차기 기한(+3년) 재기산."""
        from erp.errors import ValidationError
        lic = await session.get(License, license_id)
        if lic is None:
            raise ValidationError("존재하지 않는 면허입니다", {"license_id": license_id})
        lic.training_completed_on = completed_on
        return lic

    async def sync_license_flags(self, session: AsyncSession, *, today: date) -> int:
        """만료 면허 보유 직원의 결재 권한 플래그 차단 (FR-47)."""
        expired_emp_ids = (await session.execute(
            select(License.employee_id).where(License.expires_on < today)
        )).scalars().all()
        if not expired_emp_ids:
            return 0
        result = await session.execute(
            update(User)
            .where(User.employee_id.in_(expired_emp_ids),
                   User.license_flag_active == True)  # noqa: E712
            .values(license_flag_active=False,
                    token_version=User.token_version + 1)  # 즉시 무효화 (design §6.1)
        )
        return result.rowcount

    async def run_expiry_alerts(self, session: AsyncSession, *, today: date) -> dict:
        """만료 D-30/D-7·보수교육 D-60/30/0 인앱 경보 적재 + 만료자 권한 차단 — 잡·정기업무 공용.

        이메일은 (수신, 제목, 본문) 목록으로 반환 — **커밋 후 발송은 호출자 책임**(롤백 시 미발송
        보장). 반환={"blocked", "notified", "emails"}.
        """
        from erp.common.models import Notification
        emails: list[tuple[str, str, str]] = []
        expiring = await self.expiring_targets(session, today=today)
        training = await self.training_due_targets(session, today=today)
        # 직원→사용자 매핑 일괄 조회(N+1 제거·성수기 수백 라이선스 대비)
        emp_ids = ({lic.employee_id for lic, _ in expiring}
                   | {lic.employee_id for lic, _, _ in training})
        user_map: dict = {}
        if emp_ids:
            user_map = {e: (uid, em) for uid, em, e in (await session.execute(
                select(User.id, User.email, User.employee_id)
                .where(User.employee_id.in_(emp_ids)))).all()}
        # 밀스톤 밴드는 여러 날 반복 대상이라 이미 통지한 (user, body)는 skip(catch-up 중복 방지).
        # body는 (면허·기한·밀스톤) 결정적이라 dedup 키로 사용. 후보 사용자분만 일괄 선조회.
        cand_uids = {row[0] for row in (user_map.get(e) for e in emp_ids) if row}
        sent_keys: set = set()
        if cand_uids:
            sent_keys = {(uid, b) for uid, b in (await session.execute(
                select(Notification.user_id, Notification.body)
                .where(Notification.user_id.in_(cand_uids),
                       Notification.type.in_(("LICENSE_EXPIRY", "LICENSE_TRAINING_DUE"))))).all()}
        notified = 0

        def _emit(user_id, email, ntype, title, body):
            nonlocal notified
            if (user_id, body) in sent_keys:   # 이미 통지한 밀스톤 — 중복 방지
                return
            sent_keys.add((user_id, body))
            session.add(Notification(user_id=user_id, type=ntype, title=title, body=body))
            notified += 1
            if email:
                emails.append((email, title, body))

        for lic, milestone in expiring:
            row = user_map.get(lic.employee_id)
            if row:
                user_id, email = row
                _emit(user_id, email, "LICENSE_EXPIRY",
                      f"{lic.license_type} 면허 만료 임박(D-{milestone})",
                      f"{lic.license_no} — 만료일 {lic.expires_on} (D-{milestone})")
        # 보수교육(면허 신고 3년 주기) 기한 경보 — D-60/D-30/D-0 (내부운영 갭 감사 Tier2:
        # 보수교육 미이수=면허정지 사유를 만료알림만으로 못 잡던 갭·training_completed_on 배선)
        for lic, due, milestone in training:
            row = user_map.get(lic.employee_id)
            if row:
                user_id, email = row
                _emit(user_id, email, "LICENSE_TRAINING_DUE",
                      f"{lic.license_type} 보수교육(면허신고) 기한 임박(D-{milestone})",
                      f"{lic.license_no} — 차기 기한 {due} "
                      f"(최근 이수 {lic.training_completed_on}) (D-{milestone})")
        blocked = await self.sync_license_flags(session, today=today)
        return {"blocked": blocked, "notified": notified, "emails": emails}


# build_payroll 밖에서 소유·관리되는 라인(급여 재생성 시 보존) — 성과급·성과급원천징수는 승인 워크플로가 관리.
# INCLOCALTAX(성과급 지방소득세) — 2026-10-09 전체 흐름 검토 C: COMBINED 방식이 소득세+지방소득세를
# INCTAX 한 줄에 합쳐 원천징수이행상황의 '소득세'가 지방세만큼 부풀던 결함을 분리하며 추가.
_EXTERNAL_LINE_CODES = ("INCENTIVE", "INCTAX", "INCLOCALTAX")

# 급여 GL 계정 — 확정 시 인건비·원천징수 예수금·미지급 전기(재무제표 반영)
_ACC_SALARY_EXP = "5310"    # 급여비용
_ACC_WITHHOLDING = "2150"   # 예수금(4대보험 근로자부담·소득세 원천징수)
_ACC_PAYABLE = "2110"       # 미지급금(순지급 대기)
_ACC_WELFARE = "5330"       # 복리후생비(사업주 4대보험 부담금)


class PayrollService:
    async def upsert_lines(self, session: AsyncSession, *, actor: int, employee_id: int,
                           yyyymm: str, lines: list[dict]) -> PayrollRecord:
        """급여 항목 입력 (FR-07a) — 확정분은 잠금(수정·재생성 거부·2026-10-09 검토 C)."""
        yyyymm = require_ym(yyyymm)
        record = (await session.execute(
            select(PayrollRecord).where(PayrollRecord.employee_id == employee_id,
                                        PayrollRecord.yyyymm == yyyymm)
        )).scalar_one_or_none()
        if record is None:
            record = PayrollRecord(employee_id=employee_id, yyyymm=yyyymm)
            session.add(record)
            await session.flush()
        elif record.confirmed_at is not None:
            # 2026-10-09 전체 흐름 검토 C — 확정 급여 잠금. 종전엔 감사만 남기고 라인을 교체했는데
            # 급여 전표(source_ref=payroll:{id})는 멱등이라 최초 확정분 그대로 남아 명세↔GL이 영구히
            # 어긋났다(재생성·PUT 모두). 전표 역분개+재전기 정정 경로가 생기기 전까지는 거부가 안전하다.
            raise ValidationError(
                f"{yyyymm} 급여는 이미 확정되어 전표가 전기됐습니다 — 확정분은 수정·재생성할 수 없습니다. "
                "정정이 필요하면 재무 담당자가 해당 급여 전표를 역분개한 뒤 인사 관리자가 차액을 "
                "다음 달 급여의 정산 항목으로 반영하세요")
        from sqlalchemy import delete
        # build_payroll이 소유하는 계산 라인만 교체 — INCENTIVE 등 외부(성과급 승인)가 관리하는 라인은
        # 보존(H2: 급여 재생성이 승인된 성과급 라인을 삭제해 전표만 고아로 남기던 결함 방지).
        await session.execute(delete(PayrollLine).where(
            PayrollLine.record_id == record.id,
            PayrollLine.item_code.notin_(_EXTERNAL_LINE_CODES)))
        from erp.hr import rate_settlement
        for line in lines:
            if line["item_code"] in rate_settlement.SETTLEMENT_LINE_CODES:
                continue                  # 소급정산 공제 줄은 정산 실행이 소유 — 입력값 대신 아래에서 다시 붙인다
            session.add(PayrollLine(record_id=record.id, item_code=line["item_code"],
                                    item_kind=line["item_kind"], amount=line["amount"],
                                    calc_basis=line.get("calc_basis")))
        await session.flush()
        await rate_settlement.attach_lines(session, record)   # 요율 차액 '다음 급여 정산'(v1.295.0)
        return record

    async def confirm(self, session: AsyncSession, *, actor: int, employee_id: int,
                      yyyymm: str, paid_on: date | None = None) -> PayrollRecord:
        yyyymm = require_ym(yyyymm)
        record = (await session.execute(
            select(PayrollRecord).where(PayrollRecord.employee_id == employee_id,
                                        PayrollRecord.yyyymm == yyyymm)
        )).scalar_one_or_none()
        if record is None:
            raise ValidationError("급여 데이터가 없습니다")
        from erp.hr import rate_settlement
        if record.confirmed_at is None:   # 확정 직전 정산 공제 줄 재확인(실행 후 재생성 없이 확정해도 빠지지 않게)
            await rate_settlement.attach_lines(session, record)
            await session.flush()
        record.confirmed_at = datetime.now(timezone.utc)
        record.confirmed_by = actor
        if paid_on is not None:                 # 임금 지급일(법정)·미입력 허용(추후 정정)
            record.paid_on = paid_on
        await rate_settlement.mark_applied(session, record)
        await self._post_payroll_gl(session, record, actor)
        await self._post_employer_burden(session, record, actor)
        return record

    async def _post_employer_burden(self, session: AsyncSession, record: PayrollRecord,
                                    actor: int) -> None:
        """사업주 4대보험 부담금 GL 전기 — 설정 ON일 때. DR 복리후생비 / CR 미지급금(공단 납부 대기). 멱등.

        요율(고용보험 사업주·산재)은 기관 설정. **성과급(INCENTIVE) 사업주부담은 의도적 제외** — 4대보험은
        월 보수월액 기준 부과·연간 보수총액 정산 체계라 상여별 월 전기가 회계기준상 부적절(갭 아님·연간
        보수총액 정산에 반영). gross는 정규 급여(EXTERNAL 라인 제외) 기준. 기본 OFF.
        """
        from erp.hr.payroll_settings import PayrollSettingsService
        cfg = await PayrollSettingsService().effective(session)
        if not cfg["employer_burden_posting"]:
            return
        from erp.finance.service import VoucherLineDTO, VoucherService
        from erp.hr import statutory
        lines = (await session.execute(
            select(PayrollLine).where(PayrollLine.record_id == record.id))).scalars().all()
        from erp.hr.payroll_rules import load_nontax, nontax_exempt
        regular = [ln for ln in lines if ln.item_kind == "EARNING" and ln.item_code not in _EXTERNAL_LINE_CODES]
        # 보수월액 = 비과세 제외(근로자 공제 줄과 같은 기준 — v1.303·사업주 국민연금·건강보험은 근로자와 동액이어야 한다)
        gross = sum(int(ln.amount) for ln in regular) - int(nontax_exempt(regular, await load_nontax(session)))
        if gross <= 0:
            return
        emp = await session.get(Employee, record.employee_id)
        ei_rate, wc_rate, _src = statutory.employer_rates(   # 그 해 등록값 > 급여 설정(v1.297)
            int(record.yyyymm[:4]), fallback_ei=cfg["employer_ei_rate"], fallback_wc=cfg["employer_wc_rate"])
        burden = statutory.employer_burden(
            gross, year=int(record.yyyymm[:4]), month=statutory.month_of(record.yyyymm),
            employment_type=(emp.employment_type if emp else "REGULAR"),
            ei_rate=ei_rate, wc_rate=wc_rate)
        total = int(burden["합계"])
        if total <= 0:
            return
        y, m = int(record.yyyymm[:4]), int(record.yyyymm[5:7])
        period_end = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
        await VoucherService().create(
            session, source_module="hr.payroll", source_ref=f"payroll-employer:{record.id}",
            voucher_on=(record.paid_on or period_end), created_by=actor,
            memo=f"사업주 법정부담금 {record.yyyymm} (직원 {record.employee_id})",
            lines=[VoucherLineDTO(_ACC_WELFARE, "DEBIT", total),
                   VoucherLineDTO(_ACC_PAYABLE, "CREDIT", total)])

    async def _post_payroll_gl(self, session: AsyncSession, record: PayrollRecord,
                               actor: int) -> None:
        """급여 확정 GL 전기 — DR 급여비용 / CR 예수금(원천징수) / CR 미지급금(순지급). 멱등.

        INCENTIVE(성과급)는 승인 워크플로가 별도 전표(급여비용/미지급)로 전기하므로 제외(이중계상 방지).
        4대보험 사업주부담·법정부담금은 이 전표 범위 밖(별도 처리·H1/후속). 멱등 source_ref=payroll:{id}
        — 확정 후 라인 변경 시 재전기는 후속(현 전표는 최초 확정분·감사로 변경 가시화).
        """
        from erp.finance.service import VoucherLineDTO, VoucherService
        lines = (await session.execute(
            select(PayrollLine).where(PayrollLine.record_id == record.id))).scalars().all()
        gross = sum(int(ln.amount) for ln in lines
                    if ln.item_kind == "EARNING" and ln.item_code not in _EXTERNAL_LINE_CODES)
        # 2026-10-09 전체 흐름 검토 C — 공제도 gross와 같은 기준으로 외부 라인(INCTAX 등) 제외.
        # 성과급 원천징수는 승인 전표가 이미 2150에 대기하므로 여기서 또 넣으면 예수금이 이중계상되고
        # 순지급(2110)이 그만큼 과소 — reconcile도 영구 불일치였다.
        withheld = sum(int(ln.amount) for ln in lines
                       if ln.item_kind == "DEDUCTION" and ln.item_code not in _EXTERNAL_LINE_CODES)
        y, m = int(record.yyyymm[:4]), int(record.yyyymm[5:7])
        period_end = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
        if gross <= 0:
            if withheld > 0:
                # 정규 지급 없이 공제만 있는 달(성과급만 있는 달의 요율 차액 소급 공제 등) — 종전엔 전표가 없어 공제액이 장부에서 빠졌다.
                # 지급할 돈(성과급 승인 전표의 미지급금)에서 떼어 예수금으로: DR 미지급금 / CR 예수금(v1.305)
                await VoucherService().create(
                    session, source_module="hr.payroll", source_ref=f"payroll:{record.id}",
                    voucher_on=(record.paid_on or period_end), created_by=actor,
                    memo=f"급여 {record.yyyymm} 공제만(직원 {record.employee_id}) — 지급분에서 공제",
                    lines=[VoucherLineDTO(_ACC_PAYABLE, "DEBIT", withheld),
                           VoucherLineDTO(_ACC_WITHHOLDING, "CREDIT", withheld)])
            return
        net = gross - withheld
        from erp.hr.cost_center import employee_cost_center
        cc = await employee_cost_center(session, record.employee_id)   # G9 부서별 인건비
        vlines = [VoucherLineDTO(_ACC_SALARY_EXP, "DEBIT", gross, cost_center=cc)]
        if withheld > 0:
            vlines.append(VoucherLineDTO(_ACC_WITHHOLDING, "CREDIT", withheld))
        if net > 0:
            vlines.append(VoucherLineDTO(_ACC_PAYABLE, "CREDIT", net))
        elif net < 0:                            # 이례(공제>지급) — 차대 균형 유지
            vlines.append(VoucherLineDTO(_ACC_PAYABLE, "DEBIT", -net))
        await VoucherService().create(
            session, source_module="hr.payroll", source_ref=f"payroll:{record.id}",
            voucher_on=(record.paid_on or period_end), created_by=actor,
            memo=f"급여 {record.yyyymm} (직원 {record.employee_id})", lines=vlines)

    async def reconcile(self, session: AsyncSession, *, record: PayrollRecord) -> dict:
        """명세 합계 ↔ 급여 GL 대사(H3) — 명세 지급/공제가 급여+성과급 전표와 정합하는지 검산.

        오탐 방지: 급여비용(5310)은 급여 전표 + 당월 승인 성과급 전표 합산으로, 공제는 근로자부담
        예수금(2150·급여 전표)으로만 매칭(사업주부담·미지급 제외). 불일치=발행 게이트 경고 근거.
        """
        from sqlalchemy import func

        from erp.finance.models import Voucher, VoucherLine
        from erp.hr.models import IncentiveCalculation
        lines = (await session.execute(
            select(PayrollLine).where(PayrollLine.record_id == record.id))).scalars().all()
        payslip_earning = sum(int(ln.amount) for ln in lines if ln.item_kind == "EARNING")
        payslip_deduction = sum(int(ln.amount) for ln in lines if ln.item_kind == "DEDUCTION")
        refs = [f"payroll:{record.id}"]
        ic = await session.scalar(select(IncentiveCalculation).where(
            IncentiveCalculation.employee_id == record.employee_id,
            IncentiveCalculation.period == record.yyyymm,
            IncentiveCalculation.status == "APPROVED"))
        if ic is not None and ic.voucher_ref:
            refs.append(ic.voucher_ref)
        gl_salary = int(await session.scalar(
            select(func.coalesce(func.sum(VoucherLine.amount), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code == _ACC_SALARY_EXP, VoucherLine.side == "DEBIT",
                   Voucher.source_ref.in_(refs))) or 0)
        gl_withholding = int(await session.scalar(   # 급여 예수금 + 성과급 원천징수(FLAT)
            select(func.coalesce(func.sum(VoucherLine.amount), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code == _ACC_WITHHOLDING, VoucherLine.side == "CREDIT",
                   Voucher.source_ref.in_(refs))) or 0)
        earning_ok = payslip_earning == gl_salary
        deduction_ok = payslip_deduction == gl_withholding
        return {"yyyymm": record.yyyymm, "employee_id": record.employee_id,
                "payslip_earning": payslip_earning, "gl_salary_exp": gl_salary,
                "payslip_deduction": payslip_deduction, "gl_withholding": gl_withholding,
                "earning_matched": earning_ok, "deduction_matched": deduction_ok,
                "balanced": earning_ok and deduction_ok}


class CertificateService:
    async def issue(self, session: AsyncSession, *, employee_id: int, cert_type: str,
                    issued_to: int, purpose: str | None = None) -> CertificateIssue:
        """발급 이력 기록 (FR-23) — PDF 렌더링은 WeasyPrint 도입 시 연결 (§6.7 url_fetcher)."""
        issue = CertificateIssue(employee_id=employee_id, cert_type=cert_type,
                                 issued_to=issued_to, purpose=purpose)
        session.add(issue)
        return issue


async def user_labels(session, ids) -> dict[int, str]:
    """user_id → 표시명 — 감사·검토자 컬럼이 내부 PK만 찍던 화면들의 공용 해소기.

    `common.User.employee_id`를 경유해 직원명을 찾고, 연결이 없으면 이메일 로컬파트로 대체한다
    (이메일 전문은 노출하지 않는다). 처리자 이름은 각 EP의 스코프 안에서만 반환되므로
    노출 범위가 넓어지지 않는다.
    """
    from sqlalchemy import select as _select

    from erp.common.models import User as _User
    from erp.hr.models import Employee as _Emp
    ids = {int(i) for i in ids if i is not None}
    if not ids:
        return {}
    users = (await session.execute(
        _select(_User.id, _User.employee_id, _User.email).where(_User.id.in_(ids)))).all()
    emp_ids = {u.employee_id for u in users if u.employee_id is not None}
    names: dict[int, str] = {}
    if emp_ids:
        names = dict((await session.execute(
            _select(_Emp.id, _Emp.name).where(_Emp.id.in_(emp_ids)))).all())
    out: dict[int, str] = {}
    for u in users:
        out[u.id] = (names.get(u.employee_id) if u.employee_id is not None else None) \
            or (u.email.split("@")[0] if u.email else f"#{u.id}")
    return out


async def employee_labels(session, ids) -> dict[int, dict]:
    """employee_id → {name, employee_no} — 표에 원시 정수만 찍히던 화면들의 공용 해소기.

    근태·퇴직충당·근로계약 만기표가 `#12` 같은 내부 PK를 그대로 노출해 대상자를 특정할 수 없었다.
    각 EP의 행 스코프는 이미 서버가 강제하므로 이름 병기로 노출 범위가 넓어지지 않는다.
    """
    from sqlalchemy import select as _select

    from erp.hr.models import Employee as _Emp
    ids = {int(i) for i in ids if i is not None}
    if not ids:
        return {}
    rows = (await session.execute(
        _select(_Emp.id, _Emp.name, _Emp.employee_no).where(_Emp.id.in_(ids)))).all()
    return {i: {"name": n, "employee_no": no} for i, n, no in rows}
