"""진료비 책정 엔진 (D-6) — 본인부담 분리·중간/최종 진료비 계산. 순수 함수.

본인부담률(국민건강보험법 시행령 기준 — 기관 종별·산정특례·의료급여 종별 정밀화):
- 입원: 20% (식대 50%)
- 외래: 기관 종별(의원30·병원40·종합50·상급60) — InstitutionGradeSetting 주입
- 산정특례(중증): 유형별(암·중증화상 5% / 희귀난치 10% …) — special_case_type
- 의료급여: 1종 0% / 2종 일부 — medical_aid_class
- 비급여(NONCOVERED): 전액 본인부담
- 자보(AUTO)·산재(INDUSTRIAL): 보험자 100%(창구 본인부담 0 — 구상 별도)

수가 적용(시점 단가)·기관 종별 rates는 호출자가 주입 — 엔진은 DB 비의존
(PreReviewEngine·AllocationEngine과 동일 패턴). 각 라인에 계산 메타(rate·rate_basis·formula)를
반환해 심사 화면이 "왜 이 금액인지" 그대로 노출.
"""

DEFAULT_RATES = {
    "INPATIENT": 0.20,          # 입원 본인부담
    "OUTPATIENT": 0.40,         # 외래 본인부담 (병원 기준 — 기관 종별 주입으로 대체)
    "MEAL": 0.50,               # 입원 식대 본인부담
    "SPECIAL_CASE": 0.05,       # 산정특례 기본(유형 미상)
}

# 산정특례 유형별 본인부담률 (HIS special_case_type 동봉 시 적용)
SPECIAL_CASE_RATES = {
    "CANCER": 0.05,             # 암
    "RARE": 0.10,               # 희귀난치성
    "SEVERE_BURN": 0.05,        # 중증화상
    "HEART": 0.05,              # 중증 심장
    "BRAIN": 0.05,              # 중증 뇌혈관
    "TUBERCULOSIS": 0.0,        # 결핵 산정특례 0%
}
GRADE_LABEL = {"CLINIC": "의원", "HOSPITAL": "병원",
               "GENERAL": "종합병원", "TERTIARY": "상급종합"}


def split_line(*, total: float, coverage: str = "NHIS",
               encounter_type: str = "OUTPATIENT", is_meal: bool = False,
               special_case: bool = False, special_case_type: str | None = None,
               medical_aid_class: str | None = None, grade: str | None = None,
               his_copay_rate: float | None = None,
               eligibility_status: str | None = None,
               copay_category: str | None = None,
               rates: dict | None = None) -> dict:
    """라인 1건의 본인부담/보험자부담/비급여 분리 + 계산 메타.

    반환: {total, patient, insurer, non_covered, rate, rate_basis}
    rate=적용 본인부담률, rate_basis=근거 라벨(화면 노출).
    """
    r = {**DEFAULT_RATES, **(rates or {})}
    total = round(total)

    def out(patient: int, rate: float, basis: str) -> dict:
        return {"total": total, "patient": patient, "insurer": total - patient,
                "non_covered": total if coverage == "NONCOVERED" else 0,
                "rate": rate, "rate_basis": basis}

    if coverage == "NONCOVERED":
        return out(total, 1.0, "비급여 — 전액 본인부담")
    if coverage in ("AUTO", "INDUSTRIAL"):
        label = "자동차보험" if coverage == "AUTO" else "산재보험"
        return out(0, 0.0, f"{label} — 보험자 부담(창구 0)")

    # 자격 특수 케이스 SoT (HIS 합의) — 자격상실·의료급여종별·산정특례는 HIS copayRate 신뢰.
    # 자격 원천이 HIS(공단 자격조회 스냅샷)라 ERP가 모르는 정보. 일반 급여는 아래 ERP 자체계산.
    _elig_special = eligibility_status in ("EXPIRED", "NOT_FOUND")
    _cat_special = copay_category in ("NEAR_POOR", "DISABLED", "VETERAN")
    if his_copay_rate is not None and (
            _elig_special or medical_aid_class or special_case_type or _cat_special):
        rate = float(his_copay_rate)
        if _elig_special:
            basis = f"HIS 자격기반 — 자격상실({eligibility_status})"
        elif medical_aid_class:
            basis = f"HIS 자격기반 — 의료급여 {medical_aid_class}종"
        elif special_case_type:
            basis = f"HIS 자격기반 — 산정특례({special_case_type})"
        else:
            basis = f"HIS 자격기반 — {copay_category}"
        return out(round(total * rate), rate, basis)

    if coverage == "MEDICAL_AID":
        if medical_aid_class == "2":      # 의료급여 2종 — 일부 본인부담(외래 일부)
            rate = r.get("MEDICAL_AID_2", 0.0)
            return out(round(total * rate), rate, "의료급여 2종")
        return out(0, 0.0, "의료급여 1종 — 본인부담 면제")
    if coverage == "PATRIOT":             # 국가유공자·보훈 — 국비(위탁 본인부담 면제·감면)
        rate = r.get("PATRIOT", 0.0)
        return out(round(total * rate), rate,
                   "보훈(국가유공자) — 국비" + (f" {rate:.0%}" if rate else " 면제"))

    # NHIS 급여 — 식대는 산정특례·입원율 예외(식대 본인부담 50% 유지)라 특례보다 먼저 판정.
    if is_meal and encounter_type == "INPATIENT":
        return out(round(total * r["MEAL"]), r["MEAL"], "입원 식대")
    if special_case or special_case_type:
        rate = SPECIAL_CASE_RATES.get(special_case_type or "", r["SPECIAL_CASE"])
        stype = special_case_type or "중증"
        return out(round(total * rate), rate, f"산정특례({stype})")
    if encounter_type == "INPATIENT":
        return out(round(total * r["INPATIENT"]), r["INPATIENT"], "입원")
    rate = r["OUTPATIENT"]
    basis = f"외래·{GRADE_LABEL.get(grade or '', '병원')} 종별" if grade else "외래"
    return out(round(total * rate), rate, basis)


def compute_invoice(lines: list[dict], *, prepaid: float = 0,
                    rates: dict | None = None, grade: str | None = None,
                    round_unit: int = 10) -> dict:
    """라인 목록 → 진료비 합산 (중간/최종 공통).

    line: {code, name?, unit_price, count, coverage, encounter_type, code_type?,
           is_meal?, special_case?, special_case_type?, medical_aid_class?,
           priced(단가 해소 여부)}
    반환: 합계·본인부담·보험자·비급여·기수납 차감 잔액 + 라인별 분리(계산 메타 포함).

    round_unit: 급여 본인부담금 절사 단위(건보 시행령 §19 — 10원 미만 버림). 급여분만 절사하고
    비급여는 전액, 절사분은 보험자 부담. 1이면 절사 없음. patient_share_raw/copay_rounding로 노출.
    """
    out_lines, warnings = [], []
    total = patient = insurer = non_covered = 0
    for ln in lines:
        priced = ln.get("priced", True)
        unit = float(ln.get("unit_price") or 0)
        if not priced:
            warnings.append(f"단가 미해소 코드: {ln.get('code')} — 0원 처리(합계 미반영)")
            unit = 0
        # 산정횟수 — 미지정(None)만 1로 보정. 0(취소·영량 라인)은 `or 1`로 1건 유령청구되던 것 방지.
        count = float(ln["count"]) if ln.get("count") is not None else 1.0
        amount = unit * count
        s = split_line(total=amount, coverage=ln.get("coverage") or "NHIS",
                       encounter_type=ln.get("encounter_type") or "OUTPATIENT",
                       is_meal=bool(ln.get("is_meal")),
                       special_case=bool(ln.get("special_case")),
                       special_case_type=ln.get("special_case_type"),
                       medical_aid_class=ln.get("medical_aid_class"),
                       his_copay_rate=ln.get("his_copay_rate"),
                       eligibility_status=ln.get("eligibility_status"),
                       copay_category=ln.get("copay_category"),
                       grade=grade, rates=rates)
        # 의료급여 2종 본인부담률 미설정 방어 — HIS copayRate·rates 둘 다 없으면 0원 처리(과소청구) 경고
        if (ln.get("coverage") == "MEDICAL_AID"
                and str(ln.get("medical_aid_class") or "") == "2"
                and ln.get("his_copay_rate") is None
                and "MEDICAL_AID_2" not in (rates or {})):
            warnings.append(f"의료급여 2종 본인부담률 미설정: {ln.get('code')} — "
                            "0원 처리(HIS copayRate 또는 rates[MEDICAL_AID_2] 필요)")
        total += s["total"]
        patient += s["patient"]
        insurer += s["insurer"]
        non_covered += s["non_covered"]
        out_lines.append({
            "code": ln.get("code"), "name": ln.get("name"),
            # 투약 약품명·성분 통과(사전심사 병용·중복투약 식별)
            "drug_name": ln.get("drug_name"), "drug_ingredient": ln.get("drug_ingredient"),
            "code_type": ln.get("code_type", "TARIFF"),
            "benefit_category": ln.get("benefit_category"),
            "treated_at": ln.get("treated_at"), "treated_on": ln.get("treated_on"),
            "fee_effective_from": ln.get("fee_effective_from"),
            "fee_valid_to": ln.get("fee_valid_to"), "fee_source": ln.get("fee_source"),
            "count": count, "unit_price": unit,
            "price_source": ln.get("price_source", "MASTER"),
            "priced": priced,
            "coverage": ln.get("coverage") or "NHIS",
            # 심사 판단 근거 통과 — 종전에는 이 화이트리스트가 떨어뜨려 화면·사전심사에서
            # "왜 이 수가가 인정/부인되는지"를 볼 수 없었다. 연령·성별은 **연령 제한 수가**
            # (만 6세 미만 가산 등)와 성별 제한 항목 판정에 필요하다.
            # 개인정보: HIS가 `patientAge`(정수)·`patientSex`만 보내며 **생년월일은 이 경로로
            # 들어오지 않는다** — 병원 개인정보보호책임자 확정 범위(성별+연령, 생년월일 미노출)와 일치.
            "age": ln.get("age"), "sex": ln.get("sex"),
            "special_case_type": ln.get("special_case_type"),
            "medical_aid_class": ln.get("medical_aid_class"),
            "formula": f"{unit:,.0f} × {count:g} = {s['total']:,.0f}"
                       + (f" → ×{s['rate']:.0%} 본인부담" if s["rate"] not in (0.0, 1.0)
                          else ""),
            **s})
    prepaid = round(prepaid)
    # 10원 절사(건보 시행령 §19) — 급여 본인부담분(총 본인부담−비급여)만 10원 미만 버림, 비급여는 전액.
    # 절사분은 보험자 부담(insurer 흡수). round_unit=1이면 절사 없음.
    patient_raw = patient
    if round_unit and round_unit > 1:
        benefit_patient = patient - non_covered            # 급여 본인부담(비급여 제외)
        benefit_trunc = (int(benefit_patient) // round_unit) * round_unit
        patient = benefit_trunc + non_covered              # 비급여는 절사 안 함
        insurer = total - patient                          # 절사분(급여) 보험자 흡수
    copay_rounding = patient_raw - patient                 # 절사액(참고 노출)
    return {"total": total, "patient_share": patient, "insurer_share": insurer,
            "non_covered": non_covered, "prepaid": prepaid,
            "patient_share_raw": patient_raw,              # 절사 전 본인부담(노출)
            "copay_rounding": copay_rounding,              # 10원 절사액(보험자 부담)
            "balance_due": patient - prepaid,              # 음수 = 환불 대상
            "lines": out_lines, "warnings": warnings}
