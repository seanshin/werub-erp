"""청구 명세서 산정(계산) 엔진 — 회계상 명시 계산식 (D3-B1).

EDI(포맷·코드 교체 잦고 소급 재계산 빈번)와 **분리**된 순수 산정 계층. 진료일(treated_on) 시점 단가로
계산해 **소급 재계산 안정**(과거 명세를 다시 산정해도 그 시점 FeeMaster·가산율로 동일 결과).

계산식(회계 근거):
  급여 라인 금액 = 단가(as-of) × 산정횟수 × (1 + 종별가산율[행위료 TARIFF])
  본인부담      = 급여 금액 × 본인부담률(종별·coverage·산정특례 — billing_engine)   [10원 절사]
  공단부담(청구) = 급여 금액 − 본인부담
  비급여        = 청구 제외(환자 전액)
  묶음(bundling) = 패키지 수가(parent) 존재 시 포함된 세부/재료(child) 청구 제외

재사용: `regulatory.FeeLookupService`(시점 단가)·`patient.billing_engine.compute_invoice`(본인부담 분리·
절사)·`regulatory.InstitutionSurchargeRate`(종별가산)·`claims.BundlingRule`(묶음). 중복 산정 로직 없음.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.claims.models import BundlingRule
from erp.patient.billing_engine import compute_invoice
from erp.patient.billing_rates import load_billing_rates
from erp.regulatory.models import InstitutionSurchargeRate
from erp.regulatory.service import FeeLookupService

_fees = FeeLookupService()

# 계산식 식별(회계 근거 추적·버전) — 산정 규칙 변경 시 증가
CALC_FORMULA_VERSION = "v1"


@dataclass
class ClaimLine:
    code: str
    code_type: str = "TARIFF"       # TARIFF|DRUG|MATERIAL|NONCOVERED
    count: float = 1.0
    coverage: str = "NHIS"          # NHIS|MEDICAID|NONCOVERED|AUTO|INDUSTRIAL...
    encounter_type: str = "OUTPATIENT"
    is_meal: bool = False
    name: str | None = None
    treated_on: date | None = None  # 라인별 진료일(다중 진료일 명세) — 미지정 시 명세 기준일


async def _surcharge_for_grade(session: AsyncSession,
                               grade: str | None) -> tuple[Decimal, set[str]]:
    """종별가산율 + 적용대상 code_type 집합 (worklist.resolve_fee_with_mapping과 동일 규약).

    반환 (rate, targets). rate는 [0,1] 클램프(>100% 가산 폭주 차단). targets는 applies_to 콤마구분
    (기본 TARIFF). 미적재/미지정 종별=(0, ∅)로 무영향.
    """
    if not grade:
        return Decimal(0), set()
    sr = await session.scalar(select(InstitutionSurchargeRate).where(
        InstitutionSurchargeRate.grade == grade,
        InstitutionSurchargeRate.active.is_(True)))
    if sr is None or not float(sr.rate):
        return Decimal(0), set()
    rate = Decimal(str(max(0.0, min(float(sr.rate), 1.0))))   # 방어 클램프
    targets = {t.strip().upper() for t in (sr.applies_to or "TARIFF").split(",") if t.strip()}
    return rate, targets


async def _bundled_children(session: AsyncSession, present: set[str]) -> set[str]:
    """명세에 존재하는 parent 패키지에 딸린 child 코드(청구 제외 대상)."""
    if not present:
        return set()
    excluded: set[str] = set()
    for rule in (await session.scalars(select(BundlingRule)
                                       .where(BundlingRule.parent_code.in_(present)))).all():
        for child in (rule.child_codes or "").split(","):
            c = child.strip()
            if c and c in present:
                excluded.add(c)
    return excluded


class ClaimCalcEngine:
    """명세서 산정 — 진료일 시점 단가·종별가산·묶음·본인부담 분리(순수 회계 계산·EDI 무관)."""

    async def calc_statement(self, session: AsyncSession, *, lines: list[ClaimLine],
                             treated_on: date, grade: str | None = None) -> dict:
        # 진료일 시점 종별 본인부담률(InstitutionGradeSetting 시점관리) — 미반영 시 전 종별 병원40% 오산정.
        # 종별은 명시 인자 우선, 없으면 시점 설정값(권위 소스). rates도 as-of 로드(소급 재계산 정합).
        br = await load_billing_rates(session, treated_on)
        grade = grade or br["grade"]
        rates = br["rates"]
        surcharge, sur_targets = await _surcharge_for_grade(session, grade)
        present = {ln.code for ln in lines}
        excluded = await _bundled_children(session, present)

        priced: list[dict] = []
        unresolved: list[str] = []
        for ln in lines:
            if ln.code in excluded:
                # 묶음 제외 = 0원 청구(미해소 아님 — priced=True로 거짓 "단가 미해소" 경고 방지)
                priced.append({"code": ln.code, "name": ln.name, "unit_price": 0, "count": ln.count,
                               "coverage": ln.coverage, "encounter_type": ln.encounter_type,
                               "code_type": ln.code_type, "priced": True, "bundled": True})
                continue
            try:
                # 라인별 진료일 우선(다중 진료일 명세) — as-of 단가로 소급 재계산 정합
                fm = await _fees.get_price(session, code=ln.code, code_type=ln.code_type,
                                           at_date=ln.treated_on or treated_on)
            except Exception:  # noqa: BLE001 — 단가 미해소는 0원·경고(합계 미반영)
                unresolved.append(ln.code)
                priced.append({"code": ln.code, "name": ln.name, "unit_price": 0, "count": ln.count,
                               "coverage": ln.coverage, "encounter_type": ln.encounter_type,
                               "code_type": ln.code_type, "priced": False})
                continue
            unit = Decimal(str(fm.unit_price))
            is_noncovered = (ln.code_type == "NONCOVERED" or (fm.benefit_type or "") == "비급여")
            cov = "NONCOVERED" if is_noncovered else ln.coverage
            # 종별가산은 급여 적용대상 code_type(applies_to)에만 — 비급여·대상외 code_type은 미적용
            do_surcharge = bool(surcharge) and not is_noncovered \
                and (ln.code_type or "TARIFF").upper() in sur_targets
            if do_surcharge:
                unit = (unit * (Decimal(1) + surcharge)).quantize(Decimal(1))
            priced.append({"code": ln.code, "name": ln.name or fm.name, "unit_price": float(unit),
                           "count": ln.count, "coverage": cov, "encounter_type": ln.encounter_type,
                           "code_type": ln.code_type, "is_meal": ln.is_meal, "priced": True,
                           "surcharged": do_surcharge})

        # 본인부담/공단부담/비급여 분리·10원 절사(재사용) — 종별 rates 주입(외래 종별 본인부담률 정확)
        inv = compute_invoice(priced, grade=grade, rates=rates)
        return {
            "treated_on": treated_on.isoformat(), "grade": grade,
            "formula_version": CALC_FORMULA_VERSION,
            "total": inv["total"],                    # 급여+비급여 총액
            "covered_total": inv["total"] - inv["non_covered"],   # 급여 총액
            "claim_amount": inv["insurer_share"],     # 공단부담(청구액)
            "patient_share": inv["patient_share"],    # 본인부담(10원 절사 후)
            "patient_share_raw": inv["patient_share_raw"],   # 절사 전
            "copay_rounding": inv["copay_rounding"],  # 10원 절사액(보험자 흡수)
            "non_covered": inv["non_covered"],        # 비급여(청구 제외)
            "bundled_excluded": sorted(excluded),
            "unresolved_codes": unresolved,
            "warnings": inv["warnings"],
            "lines": inv["lines"],
        }

    async def calc_for_chart(self, session: AsyncSession, *, chart_no: str,
                             care_month: str | None = None,
                             encounter_id: str | None = None) -> dict:
        """실 진료내역(`patient.BillingLine`)으로 청구 산정 — 라인별 진료일 as-of.

        청구 라인 상세는 ClaimStatement가 아닌 BillingLine(HIS billing.line_recorded 미러)에 영속.
        취소 라인 제외. care_month/encounter_id로 명세 범위 좁힘. 기준일(grade/rates)=최근 진료일.
        ⚠ 코드는 FeeMaster 표준코드 기준 해소(HIS 비표준코드 drug 매핑 필요 시 unresolved로 가시화 —
        실 EDI 생성 시 매핑 선적용). 미리보기·검증용.
        """
        from erp.patient.models import BillingLine
        q = select(BillingLine).where(BillingLine.chart_no == chart_no,
                                      BillingLine.cancelled.is_(False))
        if encounter_id:
            q = q.where(BillingLine.encounter_id == encounter_id)
        if care_month:
            y, mo = int(care_month[:4]), int(care_month[5:7])
            nxt = date(y + (mo == 12), (mo % 12) + 1, 1)
            q = q.where(BillingLine.treated_on >= date(y, mo, 1),
                        BillingLine.treated_on < nxt)
        rows = (await session.scalars(q.order_by(BillingLine.treated_on, BillingLine.id))).all()
        if not rows:
            return {"chart_no": chart_no, "care_month": care_month, "empty": True, "lines": []}
        lines = [ClaimLine(code=r.code, code_type=r.code_type, count=float(r.count),
                           coverage=r.coverage, encounter_type=r.encounter_type,
                           treated_on=r.treated_on) for r in rows]
        base = max((r.treated_on for r in rows if r.treated_on), default=None) or date.today()
        out = await self.calc_statement(session, lines=lines, treated_on=base)
        out["chart_no"] = chart_no
        out["care_month"] = care_month
        return out
