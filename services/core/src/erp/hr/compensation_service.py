"""급여 기초정보(보수 프로필) + 온보딩 보완입력 대상 식별.

HIS/Clinic 동기화로 직원이 생성되어도 ERP 전용 정보(기본급·부양가족·계좌)가
없으면 급여 처리가 불가 — 이 서비스가 그 공백을 마스터로 영속화하고,
누락 항목을 직원별로 식별해 온보딩 체크리스트를 제공한다.
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common import crypto
from erp.errors import ValidationError
from erp.hr.models import CompensationProfile, Employee

_audit = AuditService()

_ACCOUNT_AAD = "hr.compensation_profiles.bank_account"
_RESIDENT_AAD = "hr.compensation_profiles.resident_id"


def _mask_account(account: str | None) -> str | None:
    if not account:
        return None
    tail = account[-4:] if len(account) >= 4 else account
    return f"***{tail}"


class CompensationService:
    async def get(self, session: AsyncSession, *, employee_id: int) -> CompensationProfile | None:
        return (await session.execute(
            select(CompensationProfile)
            .where(CompensationProfile.employee_id == employee_id))).scalar_one_or_none()

    async def upsert(self, session: AsyncSession, *, actor: int, employee_id: int,
                     base_pay: float, dependents: int = 1, bank_name: str | None = None,
                     bank_account: str | None = None, effective_from: date | None = None,
                     note: str | None = None, resident_id: str | None = None,
                     bank_code: str | None = None, account_holder: str | None = None
                     ) -> CompensationProfile:
        emp = await session.get(Employee, employee_id)
        if emp is None:
            raise ValidationError("존재하지 않는 직원입니다")
        if base_pay < 0 or dependents < 0:
            raise ValidationError("기본급·부양가족 수는 음수일 수 없습니다")

        profile = await self.get(session, employee_id=employee_id)
        before = None
        if profile is None:
            profile = CompensationProfile(employee_id=employee_id)
            session.add(profile)
            await session.flush()   # PK 확정 후 암호화 AAD 바인딩
        else:
            before = {"base_pay": float(profile.base_pay), "dependents": profile.dependents}

        profile.base_pay = base_pay
        profile.dependents = dependents
        profile.bank_name = bank_name
        profile.effective_from = effective_from
        profile.note = note
        profile.updated_by = actor
        if bank_account:
            profile.bank_account_enc = crypto.encrypt_column(
                bank_account, aad=crypto.column_aad(_ACCOUNT_AAD, profile.id))
        if resident_id:   # 주민번호 — 컬럼 암호화(미제공 시 기존 보존)
            profile.resident_id_enc = crypto.encrypt_column(
                resident_id, aad=crypto.column_aad(_RESIDENT_AAD, profile.id))
        if bank_code is not None:
            profile.bank_code = bank_code or None
        if account_holder is not None:
            profile.account_holder = account_holder or None

        await _audit.record_audit(
            session, actor=actor, action="UPDATE" if before else "CREATE",
            entity="hr.compensation_profiles", target_id=str(profile.id),
            before=before, after={"base_pay": base_pay, "dependents": dependents})
        return profile

    def masked_account(self, profile: CompensationProfile) -> str | None:
        """계좌는 마스킹만 노출 — 평문 조회 API 없음(이체파일 생성 시 내부 사용)."""
        if profile.bank_account_enc is None:
            return None
        plain = crypto.decrypt_column(
            profile.bank_account_enc, aad=crypto.column_aad(_ACCOUNT_AAD, profile.id))
        return _mask_account(plain)

    def masked_resident(self, profile: CompensationProfile) -> str | None:
        """주민번호 마스킹 — 생년월일 6자리 + 뒤 마스킹(YYMMDD-*******). 평문 조회 API 없음."""
        if profile.resident_id_enc is None:
            return None
        plain = crypto.decrypt_column(
            profile.resident_id_enc, aad=crypto.column_aad(_RESIDENT_AAD, profile.id))
        d = plain.replace("-", "")
        return f"{d[:6]}-*******" if len(d) >= 7 else "***"

    async def onboarding_pending(self, session: AsyncSession) -> list[dict]:
        """재직자 중 급여 처리에 필요한 정보가 누락된 직원 + 누락 항목 목록."""
        rows = (await session.execute(
            select(Employee, CompensationProfile)
            .outerjoin(CompensationProfile, CompensationProfile.employee_id == Employee.id)
            .where(Employee.resigned_on.is_(None))
            .order_by(Employee.employee_no))).all()

        pending = []
        for emp, profile in rows:
            missing: list[str] = []
            if emp.employment_type == "DAILY":
                if not emp.daily_wage:
                    missing.append("daily_wage")        # 일용직 일당
            else:
                if profile is None or float(profile.base_pay) <= 0:
                    missing.append("base_pay")          # 월 기본급
            if profile is None:
                missing.append("compensation_profile")  # 프로필 자체 미등록(부양가족·계좌)
            elif profile.bank_account_enc is None:
                missing.append("bank_account")
            if emp.employment_type == "CONTRACT" and emp.contract_end is None:
                missing.append("contract_end")          # 계약직 계약만료일
            if missing:
                pending.append({
                    "employee_id": emp.id, "employee_no": emp.employee_no,
                    "name": emp.name, "source": emp.source,
                    "employment_type": emp.employment_type,
                    "job_category": emp.job_category, "missing": missing})
        return pending

    async def onboarding_grid(self, session: AsyncSession, *, only_incomplete: bool = False
                              ) -> list[dict]:
        """일괄(스프레드시트) 편집용 — 재직 직원 전체 + 현재값(기본급·부양가족·은행·일당). 계좌 미포함(PII).

        only_incomplete=True면 기초정보 누락자만. 기본은 전원(완료분도 편집 가능·complete 플래그로 구분).
        """
        rows = (await session.execute(
            select(Employee, CompensationProfile)
            .outerjoin(CompensationProfile, CompensationProfile.employee_id == Employee.id)
            .where(Employee.resigned_on.is_(None))
            .order_by(Employee.employee_no))).all()
        grid = []
        for emp, profile in rows:
            incomplete = (
                (emp.employment_type == "DAILY" and not emp.daily_wage)
                or (emp.employment_type != "DAILY" and (profile is None or float(profile.base_pay) <= 0))
                or profile is None or profile.bank_account_enc is None)
            if only_incomplete and not incomplete:
                continue
            grid.append({
                "employee_id": emp.id, "employee_no": emp.employee_no, "name": emp.name,
                "employment_type": emp.employment_type, "job_category": emp.job_category,
                "base_pay": float(profile.base_pay) if profile else 0,
                "dependents": profile.dependents if profile else 1,
                "bank_name": (profile.bank_name if profile else None),
                "account_masked": (self.masked_account(profile) if profile else None),
                "daily_wage": float(emp.daily_wage or 0), "complete": not incomplete})
        return grid
