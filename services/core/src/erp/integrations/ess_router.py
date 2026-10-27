"""HIS ESS(직원 셀프서비스) 조회 API — W3.1 요청2·3.

HIS가 ESS 화면 표시용으로 호출하는 서버간 read API.
경로는 W3.1 요청서 표기 그대로(/integration/hr/*), 인증은 webhook과 동일 X-Integration-Key.
급여명세는 확정분만 노출(미확정 명세는 ESS 미표시 — 본인 화면과 동일 정책).
"""
import hmac
import json
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy import or_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.months import canonical_ym
from erp.config import get_settings
from erp.db import get_session
from erp.errors import ForbiddenError, ValidationError
from erp.hr.compensation_service import CompensationService
from erp.hr.models import (
    Employee, LeaveGrant, LeaveRequest, PayrollLine, PayrollRecord,
)
from erp.integrations.his_webhook_router import _inbound_nonce_fresh, inbound_signature_reason

router = APIRouter(prefix="/integration/hr", tags=["integrations"])
_audit = AuditService()
_comp = CompensationService()

# 인적공제 코드표(R-5) — ERP 기준. HIS 부양가족/공제 폼이 이 코드로 수집. 금액은 소득세법 기본(운영 갱신).
_DEDUCTION_CODES = [
    {"code": "SELF", "name": "본인 기본공제", "kind": "income", "amount": 1_500_000},
    {"code": "SPOUSE", "name": "배우자 공제", "kind": "income", "amount": 1_500_000},
    {"code": "DEPENDENT", "name": "부양가족 공제", "kind": "income", "amount": 1_500_000},
    {"code": "DISABLED", "name": "장애인 추가공제", "kind": "income", "amount": 2_000_000},
    {"code": "ELDERLY", "name": "경로우대(70세 이상) 추가공제", "kind": "income", "amount": 1_000_000},
    {"code": "WOMAN", "name": "부녀자 추가공제", "kind": "income", "amount": 500_000},
    {"code": "SINGLE_PARENT", "name": "한부모 추가공제", "kind": "income", "amount": 1_000_000},
    {"code": "CHILD_TAX", "name": "자녀세액공제", "kind": "tax_credit", "amount": None},
]
_SYSTEM_ACTOR = 0   # 서버간 호출(HIS) — 열거 시도 추적용 접속기록 주체 (H-1)


def _auth(x_integration_key: str | None) -> None:
    s = get_settings()
    if not s.his_webhook_key:
        raise ForbiddenError("연동 게이트웨이 미설정")
    if not x_integration_key or not hmac.compare_digest(x_integration_key, s.his_webhook_key):
        raise ForbiddenError("연동 인증 실패")


def _verify_target(employee_number: str, token: str | None) -> None:
    """급여 조회 대상 인가(#4) — HIS 발급 단명 토큰의 대상(sub)이 조회 대상과 일치하는지 검증.

    단일 정적키만으로 임의 직원 급여 열거 가능하던 것을 대상별 토큰으로 축소(적대검토). 계약:
    JWT HS256, `{ "sub": "<employeeNumber>", "purpose": "ess-target", "iat", "exp" }`,
    `ESS_TARGET_TOKEN_SECRET`(HIS 공유·짧은 만료). enforce=OFF(관측): 토큰 있으면 검증·없으면 정적키만
    통과(HIS 발급 배포 전 무중단). enforce=ON: 토큰 필수.
    """
    import jwt
    s = get_settings()
    if not isinstance(token, str):   # 직접호출 시 Header 기본(FieldInfo) 정규화
        token = None
    if not token:
        if s.ess_target_token_enforce:
            raise ForbiddenError("대상 토큰(X-Target-Token) 필수 — 급여 조회 대상 인가")
        return
    if not s.ess_target_token_secret:
        raise ForbiddenError("대상 토큰 검증키(ESS_TARGET_TOKEN_SECRET) 미설정")
    try:
        d = jwt.decode(token, s.ess_target_token_secret, algorithms=["HS256"],
                       options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        raise ForbiddenError("대상 토큰 검증 실패(서명·만료)") from None
    if d.get("purpose") != "ess-target" or str(d.get("sub")) != str(employee_number):
        raise ForbiddenError("대상 토큰 불일치 — 조회 대상과 토큰 대상 상이")


async def _employee_by_no(session: AsyncSession, employee_no: str) -> Employee:
    emp = await session.scalar(select(Employee).where(Employee.employee_no == employee_no))
    if emp is None:
        raise ValidationError(f"직원 미상: {employee_no}")
    return emp


def _ym_dash(period: str) -> str:
    try:
        return canonical_ym(period)
    except ValueError:
        raise ValidationError("period는 YYYYMM 형식") from None


@router.get("/payslip")
async def payslip(employeeNumber: str, period: str,  # noqa: N803 — W3.1 계약 표기
                  x_integration_key: str | None = Header(default=None),
                  x_target_token: str | None = Header(default=None, alias="X-Target-Token"),
                  session: AsyncSession = Depends(get_session)):
    """급여명세 조회 (W3.1 요청2-A) — 확정분만. 미확정/미생성은 issued=false."""
    _auth(x_integration_key)
    _verify_target(employeeNumber, x_target_token)  # #4 대상 인가
    await _audit.record_access(session, actor=_SYSTEM_ACTOR, action="VIEW",
                               entity="hr.payroll(ESS)", target_id=employeeNumber,
                               route="/integration/hr/payslip")
    emp = await _employee_by_no(session, employeeNumber)
    ym = _ym_dash(period)
    record = await session.scalar(
        select(PayrollRecord).where(PayrollRecord.employee_id == emp.id,
                                    PayrollRecord.yyyymm == ym,
                                    PayrollRecord.confirmed_at.isnot(None)))
    if record is None:
        return {"employeeNumber": employeeNumber, "period": period, "issued": False}
    lines = (await session.scalars(
        select(PayrollLine).where(PayrollLine.record_id == record.id)
        .order_by(PayrollLine.id))).all()
    items = [{"code": ln.item_code, "kind": ln.item_kind, "amount": float(ln.amount)}
             for ln in lines]
    gross = sum(i["amount"] for i in items if i["kind"] == "EARNING")
    deductions = [i for i in items if i["kind"] == "DEDUCTION"]
    ded_total = sum(i["amount"] for i in deductions)
    return {"employeeNumber": employeeNumber, "period": period, "issued": True,
            "gross": gross, "deductions": deductions, "net": gross - ded_total,
            "items": items, "issuedAt": record.confirmed_at}


@router.get("/withholding-receipt")
async def withholding_receipt(employeeNumber: str, year: int,  # noqa: N803 — W3.1 계약 표기
                              x_integration_key: str | None = Header(default=None),
                              x_target_token: str | None = Header(default=None, alias="X-Target-Token"),
                              session: AsyncSession = Depends(get_session)):
    """근로소득 원천징수영수증(연 단위) — W3.1 요청2 후속(회신서 약속분).

    확정 급여 라인 연집계: 총급여·기납부 소득세/지방세·4대보험 부담분 +
    연말정산(간이) 결정세액·정산액. 월별 내역 포함.
    """
    from erp.hr import statutory
    _auth(x_integration_key)
    _verify_target(employeeNumber, x_target_token)  # #4 대상 인가
    await _audit.record_access(session, actor=_SYSTEM_ACTOR, action="VIEW",
                               entity="hr.withholding(ESS)", target_id=employeeNumber,
                               route="/integration/hr/withholding-receipt")
    emp = await _employee_by_no(session, employeeNumber)
    rows = (await session.execute(
        select(PayrollRecord.yyyymm, PayrollLine.item_code, PayrollLine.item_kind,
               func.sum(PayrollLine.amount))
        .join(PayrollLine, PayrollLine.record_id == PayrollRecord.id)
        .where(PayrollRecord.employee_id == emp.id,
               # 확정 급여 + 성과급 외부 라인(성과급 승인 시 이미 전기·신고 집계와 같은 기준 — 3차 검토 C:
               # 급여 없는 달의 성과급(퇴사자)이 영수증에서만 빠져 신고와 어긋났다)
               or_(PayrollRecord.confirmed_at.isnot(None),
                   PayrollLine.item_code.in_(("INCENTIVE", "INCTAX", "INCLOCALTAX"))),
               PayrollRecord.yyyymm.like(f"{year}-%"))
        .group_by(PayrollRecord.yyyymm, PayrollLine.item_code, PayrollLine.item_kind)
        .order_by(PayrollRecord.yyyymm))).all()
    # 2026-10-09 전체 흐름 검토 2차 C: 퇴직 정산(귀속=퇴사일 연도)의 원천징수 반영. 연차 미사용 수당은
    # 근로소득이라 총급여·기납부세액에 합산(종전엔 빠져 퇴사자 연말정산이 틀렸다), 퇴직금·퇴직소득세는
    # 근로소득과 별개(퇴직소득 원천징수영수증)라 `retirement` 블록으로만 제공한다.
    from datetime import date as _date

    from erp.hr.models import OffboardingSettlement
    off = await session.scalar(select(OffboardingSettlement).where(
        OffboardingSettlement.employee_id == emp.id,
        OffboardingSettlement.resigned_on >= _date(int(year), 1, 1),
        OffboardingSettlement.resigned_on <= _date(int(year), 12, 31)))
    if not rows and off is None:
        return {"employeeNumber": employeeNumber, "year": year, "issued": False}

    months: dict[str, dict] = {}
    total = {"gross": 0.0, "incomeTax": 0.0, "localTax": 0.0,
             "pension": 0.0, "health": 0.0, "longterm": 0.0, "employment": 0.0}
    # INCTAX/INCLOCALTAX(성과급 원천징수 소득세·지방세) 포함 — 2026-10-09 전체 흐름 검토 C: 총급여엔
    # 성과급이 들어가는데 기납부세액에서 빠져 연말정산 '추가납부'가 허위로 커졌다.
    code_key = {"TAX": "incomeTax", "INCTAX": "incomeTax", "LOCALTAX": "localTax",
                "INCLOCALTAX": "localTax", "NP": "pension",
                "HI": "health", "LTC": "longterm", "EI": "employment",
                # 요율 차액 소급정산 공제(v1.295.0) — 실제 납부한 보험료라 연말정산 공제액에 합산
                "NP_ADJ": "pension", "HI_ADJ": "health", "LTC_ADJ": "longterm", "EI_ADJ": "employment"}
    for ym, code, kind, amount in rows:
        m = months.setdefault(ym, {"period": ym.replace("-", ""), "gross": 0.0,
                                   "deduction": 0.0})
        amt = float(amount)
        if kind == "EARNING":
            m["gross"] += amt
            total["gross"] += amt
        else:
            m["deduction"] += amt
            key = code_key.get(code)
            if key:
                total[key] += amt
    retirement = None
    if off is not None:
        la = float(off.leave_allowance or 0)
        if la > 0:
            ym = f"{off.resigned_on.year:04d}-{off.resigned_on.month:02d}"
            m = months.setdefault(ym, {"period": ym.replace("-", ""), "gross": 0.0,
                                       "deduction": 0.0})
            lt, llt = float(off.leave_allowance_tax or 0), float(off.leave_allowance_local_tax or 0)
            m["gross"] += la
            m["deduction"] += lt + llt
            total["gross"] += la
            total["incomeTax"] += lt
            total["localTax"] += llt
        retirement = {"resignedOn": off.resigned_on,
                      "severance": float(off.severance_amount or 0),
                      "incomeTax": float(off.retirement_income_tax or 0),
                      "localTax": float(off.retirement_local_tax or 0),
                      "leaveAllowance": la}
    # 가족·자녀·보험료공제는 관리자 화면(연말정산 준비·직원 간이 정산)과 같은 기준(v1.304) — 부양가족 등록 > 보수 프로필 ·
    # 8~20세 자녀 · 납부한 국민연금·건강·장기요양·고용보험료 공제(종전: 프로필 가족 수·자녀 0·보험료 미공제라 결정세액 과대).
    from erp.hr.compensation_service import CompensationService
    from erp.hr.models import Dependent
    from erp.hr.withholding_table import children_8_20, family_from
    prof = await CompensationService().get(session, employee_id=emp.id)
    dep_rows = (await session.scalars(select(Dependent).where(Dependent.employee_id == emp.id))).all()
    deps = family_from(dep_rows, getattr(prof, "dependents", None))
    kids = min(children_8_20(dep_rows, int(year)), deps - 1)
    # 정산 미리보기는 관리자 화면과 **같은 집계**(year_end.annual_totals) — 총급여=비과세 제외·일용(분리과세) 제외. 영수증의 월별·총지급
    # 표시(totalGross)는 종전 그대로 두고 총급여(소득세법)는 totalTaxable로 따로 준다(검토 MED — ESS만 총지급으로 정산해 값이 갈렸다).
    from erp.hr import year_end
    yt = (await year_end.annual_totals(session, year=int(year), employee_ids=[emp.id])).get(emp.id)
    settlement = statutory.year_end_settlement(
        yt["taxable"] if yt else 0.0, withheld_tax=yt["income_tax"] if yt else 0.0, dependents=deps, children=kids,
        year=int(year), pension_paid=yt["pension"] if yt else 0.0, insurance_paid=yt["insurance"] if yt else 0.0)
    return {"employeeNumber": employeeNumber, "year": year, "issued": True,
            "totalGross": total["gross"],
            "totalTaxable": round(yt["taxable"]) if yt else 0, "nontax": round(yt["nontax"]) if yt else 0,
            # 일용(분리과세) 달은 연말정산 대상이 아니라 정산(settlement)에서 빠진다 — withheld·months에는 그대로 보인다
            "separateTaxationMonths": (yt or {}).get("daily_months", []),
            "withheld": {"incomeTax": total["incomeTax"], "localTax": total["localTax"]},
            "insurance": {"pension": total["pension"], "health": total["health"],
                          "longterm": total["longterm"], "employment": total["employment"]},
            "settlement": {"determinedTax": settlement["결정세액"],
                           "prepaidTax": settlement["기납부세액"],
                           "balance": settlement["정산액"],
                           "refundable": settlement["환급여부"]},
            "months": sorted(months.values(), key=lambda m: m["period"]),
            "retirement": retirement}


@router.get("/leave-balance")
async def leave_balance(employeeNumber: str, year: int,  # noqa: N803 — W3.1 계약 표기
                        x_integration_key: str | None = Header(default=None),
                        x_target_token: str | None = Header(default=None, alias="X-Target-Token"),
                        session: AsyncSession = Depends(get_session)):
    """연차 잔여 조회 (W3.1 요청3) — 부여/사용/잔여 (SoT=ERP)."""
    _auth(x_integration_key)
    _verify_target(employeeNumber, x_target_token)  # #4 대상 인가
    await _audit.record_access(session, actor=_SYSTEM_ACTOR, action="VIEW",
                               entity="hr.leave_balance(ESS)", target_id=employeeNumber,
                               route="/integration/hr/leave-balance")
    emp = await _employee_by_no(session, employeeNumber)
    granted = float((await session.execute(
        select(func.coalesce(func.sum(LeaveGrant.granted_days), 0))
        .where(LeaveGrant.employee_id == emp.id,
               LeaveGrant.grant_year == year))).scalar_one())
    used = float((await session.execute(
        select(func.coalesce(func.sum(LeaveRequest.days), 0))
        .where(LeaveRequest.employee_id == emp.id,
               LeaveRequest.status == "APPROVED",
               LeaveRequest.leave_type == "ANNUAL",
               LeaveRequest.start_on >= date(year, 1, 1),
               LeaveRequest.start_on <= date(year, 12, 31)))).scalar_one())
    return {"employeeNumber": employeeNumber, "year": year,
            "items": [{"type": "ANNUAL", "granted": granted, "used": used,
                       "remaining": granted - used}]}


# ── 급여 기초정보 인입/리드백/공제코드 (HIS 연동 R-1/R-4/R-5) ─────────────────
@router.post("/payroll-identity")
async def payroll_identity_intake(request: Request,
                                  x_integration_key: str | None = Header(None),
                                  x_signature: str | None = Header(None),
                                  x_timestamp: str | None = Header(None),
                                  session: AsyncSession = Depends(get_session)):
    """직원 급여 기초정보 수신(R-1) — 계좌·주민번호·예금주·부양가족. 민감정보라 X-Integration-Key +
    HMAC 서명(X-Signature/X-Timestamp·재생방지) 필수. **기본급은 ERP 소유(보존)** — R-3 안A 경계.

    본문 예: {employeeCode, residentId, bankName, bankCode, bankAccount, holder, dependents}.
    계좌·주민번호는 컬럼 암호화 저장, 응답은 마스킹만. mTLS는 배포 인프라(nginx)에서 선택 적용.
    """
    _auth(x_integration_key)
    raw = await request.body()
    s = get_settings()
    # 민감정보(주민/계좌) 인입은 **전용 본문서명 키 필수** — 정적 X-Integration-Key로 폴백하면 HMAC이
    # 독립 2차인증이 못 되고(호출자가 어차피 아는 키), 방어심층이 0(적대검토 §4). 미설정 시 fail-closed.
    secret = s.inbound_webhook_secret
    if not secret:
        raise ForbiddenError("민감정보 인입 서명키(INBOUND_WEBHOOK_SECRET) 미설정 — 수신 거부")
    reason = inbound_signature_reason(raw, x_signature, x_timestamp, secret=secret,
                                      now_epoch=datetime.now(timezone.utc).timestamp(),
                                      max_skew=s.inbound_webhook_max_skew)
    if reason is not None:
        raise ForbiddenError(f"서명 검증 실패: {reason}")
    # nonce 단일소비 — 창(skew) 내 동일 서명 재전송 차단(재생 방지 심화)
    if not await _inbound_nonce_fresh(x_signature, window=s.inbound_webhook_max_skew):
        raise ForbiddenError("서명 재생 감지 — 수신 거부")
    try:
        p = json.loads(raw or b"{}")
    except ValueError:
        raise ValidationError("본문 JSON 파싱 실패") from None
    code = (p.get("employeeCode") or p.get("employeeNumber") or "").strip()
    if not code:
        raise ValidationError("employeeCode 필수")
    emp = await _employee_by_no(session, code)
    existing = await _comp.get(session, employee_id=emp.id)          # 기본급 보존(ERP 소유)
    base_pay = float(existing.base_pay) if existing else 0
    deps = int(p["dependents"]) if p.get("dependents") is not None \
        else (existing.dependents if existing else 1)
    await _comp.upsert(session, actor=_SYSTEM_ACTOR, employee_id=emp.id, base_pay=base_pay,
                       dependents=deps, bank_name=p.get("bankName"), bank_code=p.get("bankCode"),
                       bank_account=(p.get("bankAccount") or p.get("accountNumber")),  # 별칭 수용
                       account_holder=p.get("holder"), resident_id=p.get("residentId"))
    await _audit.record_audit(session, actor=_SYSTEM_ACTOR, action="UPDATE",
                              entity="hr.compensation_profiles", target_id=str(emp.id),
                              after={"source": "HIS.payroll-identity", "employee_no": code})
    prof = await _comp.get(session, employee_id=emp.id)
    return {"employeeNumber": code, "accepted": True,
            "accountMasked": _comp.masked_account(prof),
            "residentMasked": _comp.masked_resident(prof), "dependents": prof.dependents}


@router.get("/payroll-identity/{employeeNumber}")
async def payroll_identity_readback(employeeNumber: str,  # noqa: N803 — W3.1 계약 표기
                                    x_integration_key: str | None = Header(None),
                                    x_target_token: str | None = Header(default=None, alias="X-Target-Token"),
                                    session: AsyncSession = Depends(get_session)):
    """급여 기초정보 리드백(R-4) — 직원이 HIS에서 ERP 등록분(마스킹)을 확인·대사. 평문 미노출."""
    _auth(x_integration_key)
    _verify_target(employeeNumber, x_target_token)  # #4 대상 인가
    emp = await _employee_by_no(session, employeeNumber)
    prof = await _comp.get(session, employee_id=emp.id)
    await _audit.record_access(session, actor=_SYSTEM_ACTOR, action="VIEW",
                               entity="hr.compensation_profiles", target_id=str(emp.id),
                               route="/integration/hr/payroll-identity")
    if prof is None:
        return {"employeeNumber": employeeNumber, "registered": False}
    return {"employeeNumber": employeeNumber, "registered": True,
            "bankName": prof.bank_name, "bankCode": prof.bank_code,
            "accountMasked": _comp.masked_account(prof), "holder": prof.account_holder,
            "residentMasked": _comp.masked_resident(prof), "dependents": prof.dependents}


@router.get("/deduction-codes")
async def deduction_codes(x_integration_key: str | None = Header(None)):
    """인적공제 코드표 회신(R-5) — HIS 부양가족/공제 폼 매핑용. ERP 기준(소득세법)."""
    _auth(x_integration_key)
    return {"items": _DEDUCTION_CODES}


# ── 개인포털 2차: 당직·성과급실적·증명서·퇴직금 (HIS 연동 R-8~R-11) ─────────────
@router.get("/duties")
async def duties(employeeNumber: str,  # noqa: N803 — 계약 표기
                 x_integration_key: str | None = Header(None),
                 x_target_token: str | None = Header(default=None, alias="X-Target-Token"),
                 from_: date = Query(alias="from"), to: date = Query(...),
                 session: AsyncSession = Depends(get_session)):
    """당직/근무표 조회(R-8) — 기간(from~to) 본인 배정. SoT=ERP."""
    _auth(x_integration_key)
    _verify_target(employeeNumber, x_target_token)  # #4 대상 인가
    from erp.hr.models import DutyAssignment
    emp = await _employee_by_no(session, employeeNumber)
    rows = (await session.scalars(
        select(DutyAssignment).where(
            DutyAssignment.employee_id == emp.id,
            DutyAssignment.duty_on >= from_, DutyAssignment.duty_on <= to)
        .order_by(DutyAssignment.duty_on))).all()
    return {"employeeNumber": employeeNumber, "from": from_, "to": to,
            "items": [{"duty_on": r.duty_on, "shift": r.shift} for r in rows]}


@router.get("/performance")
async def performance(employeeNumber: str, period: str,  # noqa: N803 — 계약 표기
                      x_integration_key: str | None = Header(None),
                      x_target_token: str | None = Header(default=None, alias="X-Target-Token"),
                      session: AsyncSession = Depends(get_session)):
    """성과급 실적 상세(R-9) — 진료수익·순수입·승인 성과급·계약형. 본인만."""
    _auth(x_integration_key)
    _verify_target(employeeNumber, x_target_token)  # #4 대상 인가
    from erp.hr.models import IncentiveCalculation, MedicalStaffContract
    from erp.hr.physician_revenue import PhysicianRevenueService
    emp = await _employee_by_no(session, employeeNumber)
    ym = _ym_dash(period)
    agg = await PhysicianRevenueService().aggregate(session, period=ym, doctor_id=emp.id)
    rev = agg["rows"][0] if agg["rows"] else None
    ic = await session.scalar(select(IncentiveCalculation).where(
        IncentiveCalculation.employee_id == emp.id, IncentiveCalculation.period == ym,
        IncentiveCalculation.status == "APPROVED"))
    ct = await session.scalar(select(MedicalStaffContract).where(
        MedicalStaffContract.employee_id == emp.id,
        MedicalStaffContract.status.in_(("ACTIVE", "EXPIRING"))))
    return {"employeeNumber": employeeNumber, "period": period,
            "revenue": rev, "note": agg["note"],
            "incentive": ({"amount": float(ic.amount), "netRevenue": float(ic.net_revenue),
                           **(ic.snapshot or {})} if ic else None),
            "contract": ({"incentiveType": ct.incentive_type,
                          "incentiveRate": (float(ct.incentive_rate) if ct.incentive_rate else None)}
                         if ct else None)}


@router.get("/certificates")
async def certificates(employeeNumber: str,  # noqa: N803 — 계약 표기
                       x_integration_key: str | None = Header(None),
                       x_target_token: str | None = Header(default=None, alias="X-Target-Token"),
                       session: AsyncSession = Depends(get_session)):
    """재직/경력 증명서 발급 이력(R-10) — PDF는 .../{id}/pdf. ⚠ HIS 자체 발급과 정본 통일 결정 대기."""
    _auth(x_integration_key)
    _verify_target(employeeNumber, x_target_token)  # #4 대상 인가
    from erp.hr.models import CertificateIssue
    emp = await _employee_by_no(session, employeeNumber)
    rows = (await session.scalars(
        select(CertificateIssue).where(CertificateIssue.employee_id == emp.id)
        .order_by(CertificateIssue.id.desc()))).all()
    return {"employeeNumber": employeeNumber,
            "items": [{"id": c.id, "certType": c.cert_type, "purpose": c.purpose,
                       "issuedAt": c.issued_at} for c in rows]}


@router.get("/certificates/{issue_id}/pdf")
async def certificate_pdf(issue_id: int, employeeNumber: str,  # noqa: N803 — 계약 표기
                          x_integration_key: str | None = Header(None),
                          x_target_token: str | None = Header(default=None, alias="X-Target-Token"),
                          session: AsyncSession = Depends(get_session)):
    """증명서 PDF(R-10) — 본인 발급분만. 발급기관=기관 설정."""
    from fastapi.responses import Response

    from erp.common.org_service import org_display_name, org_logo_data_uri
    from erp.hr.models import CertificateIssue
    from erp.hr.router import _cert_pdf, _dept_name
    _auth(x_integration_key)
    _verify_target(employeeNumber, x_target_token)  # #4 대상 인가
    emp = await _employee_by_no(session, employeeNumber)
    issue = await session.get(CertificateIssue, issue_id)
    if issue is None or issue.employee_id != emp.id:
        raise ForbiddenError("본인 증명서만 발급할 수 있습니다")
    await _audit.record_access(session, actor=_SYSTEM_ACTOR, action="PRINT",
                               entity="hr.certificate_issues", target_id=str(issue.id),
                               route="/integration/hr/certificates")
    pdf = _cert_pdf(emp, issue, dept_name=await _dept_name(session, emp.department_id),
                    org_name=await org_display_name(session),
                    logo=await org_logo_data_uri(session))
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="certificate-{issue.id}.pdf"'})


@router.get("/severance")
async def severance(employeeNumber: str,  # noqa: N803 — 계약 표기
                    x_integration_key: str | None = Header(None),
                    x_target_token: str | None = Header(default=None, alias="X-Target-Token"),
                    as_of: date | None = None,
                    session: AsyncSession = Depends(get_session)):
    """퇴직금 추정(R-11) — 최근 급여 평균임금 × 근속(일할). 추정치(정산 아님)."""
    from erp.hr import statutory
    _auth(x_integration_key)
    _verify_target(employeeNumber, x_target_token)  # #4 대상 인가
    emp = await _employee_by_no(session, employeeNumber)
    from erp.hr.retirement_service import statutory_avg_wage
    as_of = as_of or date.today()
    anchor = min(as_of, emp.resigned_on) if emp.resigned_on else as_of
    avg = await statutory_avg_wage(session, emp, anchor=anchor)   # 법정 평균임금(시나리오 S-C)
    # 퇴직금은 반올림 전 값으로 — 정산(offboarding)과 1원 어긋나지 않게(시나리오 2회차)
    return {"employeeNumber": employeeNumber, "avgMonthlyWage": round(avg),
            **statutory.severance(avg, hire_date=emp.hired_on, as_of=as_of)}
