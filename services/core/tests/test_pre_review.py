"""사전심사 룰엔진 (FR-41) — 중복·산정횟수·미등재·금액이상 룰 평가."""
from types import SimpleNamespace

from erp.claims.pre_review import PreReviewEngine

eng = PreReviewEngine()


def _rule(code, rtype, params=None, sev="WARN"):
    return SimpleNamespace(code=code, name=code, rule_type=rtype, params=params,
                           severity=sev, active=True)


def test_duplicate():
    lines = [{"chart_no": "P1", "code": "AA001", "treated_on": "2026-05-01", "count": 1},
             {"chart_no": "P1", "code": "AA001", "treated_on": "2026-05-01", "count": 1}]
    f = eng.evaluate(lines=lines, rules=[_rule("PR-DUP", "DUPLICATE")], fee={})
    assert len(f) == 1 and f[0].rule_code == "PR-DUP"


def test_count_limit():
    lines = [{"chart_no": "P1", "code": "BB", "treated_on": "2026-05-01", "count": 3}]
    f = eng.evaluate(lines=lines, rules=[_rule("PR-CNT", "COUNT_LIMIT", {"max": 1})], fee={})
    assert len(f) == 1 and "3회" in f[0].detail


def test_count_limit_zero_and_negative_not_inflated():
    """count=0(취소)·음수(조정)가 `or 1`로 1로 둔갑하거나 합산을 왜곡하지 않아야 한다(전체검토 G)."""
    rule = _rule("PR-CNT", "COUNT_LIMIT", {"max": 1})
    # 일자합산: 0건 두 라인은 1+1=2로 오탐되면 안 됨(상한 1)
    zero_lines = [{"chart_no": "P1", "code": "BB", "treated_on": "2026-05-01", "count": 0},
                  {"chart_no": "P1", "code": "BB", "treated_on": "2026-05-01", "count": 0}]
    assert eng.evaluate(lines=zero_lines, rules=[rule], fee={}) == []
    # 라인단위(mai=1): count=0 라인은 적발 대상 아님
    line_rule = _rule("PR-CNT", "COUNT_LIMIT", {"max": 1, "mai": 1})
    assert eng.evaluate(lines=[{"chart_no": "P1", "code": "BB", "count": 0}],
                        rules=[line_rule], fee={}) == []
    # 음수 조정이 정상 2건을 깎아 미탐시키면 안 됨(2건+(-1)=1 ≤ 1 통과 방지)
    mixed = [{"chart_no": "P1", "code": "BB", "treated_on": "2026-05-01", "count": 2},
             {"chart_no": "P1", "code": "BB", "treated_on": "2026-05-01", "count": -1}]
    f = eng.evaluate(lines=mixed, rules=[rule], fee={})
    assert len(f) == 1 and "2회" in f[0].detail   # 음수 제외 → 2 > 1 적발


def test_unknown_code_blocks():
    lines = [{"chart_no": "P1", "code": "ZZZ", "count": 1}]
    f = eng.evaluate(lines=lines, rules=[_rule("PR-UNK", "UNKNOWN_CODE", sev="BLOCK")],
                     fee={"AA001": 1000})
    assert len(f) == 1 and f[0].severity == "BLOCK"


def test_amount_outlier():
    lines = [{"chart_no": "P1", "code": "AA001", "count": 1, "amount": 2000}]
    f = eng.evaluate(lines=lines, rules=[_rule("PR-AMT", "AMOUNT_OUTLIER", {"tolerance": 0.1})],
                     fee={"AA001": 1000})
    assert len(f) == 1   # 2000 vs 1000 → 100% > 10%


def test_clean_claim_no_findings():
    lines = [{"chart_no": "P1", "code": "AA001", "count": 1, "amount": 1000,
              "treated_on": "2026-05-01"}]
    rules = [_rule("PR-DUP", "DUPLICATE"), _rule("PR-CNT", "COUNT_LIMIT", {"max": 1}),
             _rule("PR-UNK", "UNKNOWN_CODE"), _rule("PR-AMT", "AMOUNT_OUTLIER", {"tolerance": 0.1})]
    f = eng.evaluate(lines=lines, rules=rules, fee={"AA001": 1000})
    assert f == []


def test_inactive_rule_skipped():
    r = _rule("PR-DUP", "DUPLICATE")
    r.active = False
    lines = [{"chart_no": "P1", "code": "A", "treated_on": "x"},
             {"chart_no": "P1", "code": "A", "treated_on": "x"}]
    assert eng.evaluate(lines=lines, rules=[r], fee={}) == []


def test_amount_outlier_drug_cap():
    # 약가(DRUG)는 상한금액 초과 검증
    from erp.claims.pre_review import PreReviewEngine
    eng = PreReviewEngine()
    lines = [{"chart_no": "P1", "code": "D1", "count": 2, "amount": 5000}]
    fee = {"D1": {"price": 0, "cap": 2000, "type": "DRUG"}}   # 상한 2000×2=4000 < 5000
    f = eng.evaluate(lines=lines, rules=[_rule("PR-AMT", "AMOUNT_OUTLIER")], fee=fee)
    assert len(f) == 1 and "상한" in f[0].detail
    # 상한 이내면 미적발
    fee2 = {"D1": {"price": 0, "cap": 3000, "type": "DRUG"}}  # 3000×2=6000 > 5000
    assert eng.evaluate(lines=lines, rules=[_rule("PR-AMT", "AMOUNT_OUTLIER")], fee=fee2) == []


def test_unknown_code_with_dict_fee():
    from erp.claims.pre_review import PreReviewEngine
    eng = PreReviewEngine()
    lines = [{"chart_no": "P1", "code": "ZZZ", "count": 1}]
    fee = {"AA001": {"price": 1000, "cap": None, "type": "TARIFF"}}
    f = eng.evaluate(lines=lines, rules=[_rule("PR-UNK", "UNKNOWN_CODE", sev="BLOCK")], fee=fee)
    # detail은 코드만(rule_name이 "미등재" 의미 — 표시 "{rule_name}: {detail}" 중복 방지)
    assert len(f) == 1 and f[0].detail == "청구 코드 ZZZ"


def test_unknown_code_skips_noncovered():
    """비급여 라인은 미등재(UNKNOWN_CODE) 적발 제외 — 비급여 코드/가격은 HIS 소관, 보험
    삭감 위험 없음(D-1). 급여(NHIS) 라인의 미등재만 적발."""
    from erp.claims.pre_review import PreReviewEngine
    rule = _rule("PR-UNK", "UNKNOWN_CODE", sev="BLOCK")
    lines = [
        {"chart_no": "P1", "code": "M091501", "count": 2, "coverage": "NONCOVERED"},  # 비급여 → 제외
        {"chart_no": "P1", "code": "NHISX", "count": 1, "coverage": "NHIS"},          # 급여 → 적발
    ]
    f = PreReviewEngine().evaluate(lines=lines, rules=[rule], fee={})
    codes = {x.code for x in f}
    assert "M091501" not in codes      # 비급여 미등재 제외
    assert "NHISX" in codes            # 급여 미등재는 여전히 적발


def test_count_limit_skips_drug_material():
    """약제·재료 투여량은 일반 산정횟수 룰 제외 — 21정·28바이알이 행위 상한으로 오탐 안 함."""
    from datetime import date
    rules = [_rule("CNT", "COUNT_LIMIT", params={"max": 1}, sev="WARN"),
             _rule("PER", "PERIOD_LIMIT", params={"max": 4, "window": "M"}, sev="WARN")]
    lines = [
        {"chart_no": "P1", "code": "M300401", "code_type": "DRUG", "count": 28,
         "treated_on": date(2026, 6, 12)},      # 약제 — 산정횟수 제외
        {"chart_no": "P1", "code": "KK059", "code_type": "TARIFF", "count": 2,
         "treated_on": date(2026, 6, 12)},      # 행위 — 적발
    ]
    f = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    codes = {x.code for x in f}
    assert "M300401" not in codes      # 약제 산정횟수 제외
    assert "KK059" in codes            # 행위는 적발


def test_benefit_rules_skip_expired_eligibility():
    """자격상실(EXPIRED)/자격없음 라인은 급여 삭감 룰 제외 — 보험 청구 대상 아님(보험 0원)."""
    from datetime import date
    rules = [_rule("DUP", "DUPLICATE", sev="WARN"),
             _rule("CNT", "COUNT_LIMIT", params={"max": 1}, sev="WARN")]
    lines = [
        {"chart_no": "P1", "code": "AA", "code_type": "TARIFF", "count": 1,
         "eligibility_status": "EXPIRED", "treated_on": date(2026, 6, 12)},
        {"chart_no": "P1", "code": "AA", "code_type": "TARIFF", "count": 1,
         "eligibility_status": "EXPIRED", "treated_on": date(2026, 6, 12)},
        {"chart_no": "P2", "code": "BB", "code_type": "TARIFF", "count": 1,
         "treated_on": date(2026, 6, 12)},
        {"chart_no": "P2", "code": "BB", "code_type": "TARIFF", "count": 1,
         "treated_on": date(2026, 6, 12)},
    ]
    f = PreReviewEngine().evaluate(lines=lines, rules=rules, fee={})
    charts = {x.chart_no for x in f}
    assert "P1" not in charts      # 자격상실 — 급여 룰 제외
    assert "P2" in charts          # 정상 자격 — 중복 적발


def test_reason_code_stamped():
    """P0-2 — 룰타입별 거부사유 코드(reason_group/reason_code) 스탬프."""
    lines = [{"chart_no": "P1", "code": "BB", "treated_on": "2026-05-01", "count": 3}]
    f = eng.evaluate(lines=lines, rules=[_rule("PR-CNT", "COUNT_LIMIT", {"max": 1})], fee={})
    assert f[0].reason_group == "B" and f[0].reason_code == "B-CNT"
    dup = [{"chart_no": "P1", "code": "AA", "treated_on": "2026-05-01", "count": 1},
           {"chart_no": "P1", "code": "AA", "treated_on": "2026-05-01", "count": 1}]
    fd = eng.evaluate(lines=dup, rules=[_rule("PR-DUP", "DUPLICATE")], fee={})
    assert fd[0].reason_group == "D" and fd[0].reason_code == "D-DUP"
    fu = eng.evaluate(lines=[{"chart_no": "P1", "code": "ZZ", "count": 1}],
                      rules=[_rule("PR-UNK", "UNKNOWN_CODE", sev="BLOCK")], fee={"AA": 1000})
    assert fu[0].reason_group == "K" and fu[0].reason_code == "K-UNK"


def test_count_limit_mai_line_vs_day():
    """P0-3 — MAI 단위: 일자합산(기본) vs 라인단위(mai=1)."""
    # 같은 날 2라인(각 1회)·합산 2 > 1 → 일자합산은 적발, 라인단위(각 1≤1)는 무적발
    lines = [{"chart_no": "P1", "code": "CC", "treated_on": "2026-05-01", "count": 1},
             {"chart_no": "P1", "code": "CC", "treated_on": "2026-05-01", "count": 1}]
    day = eng.evaluate(lines=lines, rules=[_rule("PR-CNT", "COUNT_LIMIT", {"max": 1})], fee={})
    assert len(day) == 1 and "2회" in day[0].detail
    line = eng.evaluate(lines=lines, rules=[_rule("PR-CNT", "COUNT_LIMIT", {"max": 1, "mai": 1})], fee={})
    assert line == []
    # 단일 라인 count 3 > 1 → 라인단위 적발 + MAI 라벨
    big = [{"chart_no": "P1", "code": "CC", "treated_on": "2026-05-01", "count": 3}]
    lf = eng.evaluate(lines=big, rules=[_rule("PR-CNT", "COUNT_LIMIT", {"max": 1, "mai": 1})], fee={})
    assert len(lf) == 1 and "MAI1" in lf[0].detail
    # 일자절대 MAI2 라벨
    d2 = eng.evaluate(lines=big, rules=[_rule("PR-CNT", "COUNT_LIMIT", {"max": 1, "mai": 2}, sev="BLOCK")], fee={})
    assert d2[0].severity == "BLOCK" and "MAI2" in d2[0].detail


def _dur(dtype, ing=None, ing_b=None, min_age=None, max_age=None, reason="", sev="BLOCK"):
    return SimpleNamespace(dur_type=dtype, ingredient_code=ing, ingredient_code_b=ing_b,
                           min_age=min_age, max_age=max_age, reason=reason, notice_no="2026-1",
                           severity=sev, active=True)


def test_dur_contraindication_and_dup_and_age():
    """P0-1 DUR — 병용금기·동일성분 중복·연령금기."""
    lines = [
        {"chart_no": "P1", "code": "D1", "code_type": "DRUG", "ingredient": "ING-A",
         "treated_on": "2026-06-01", "patient_age": 40},
        {"chart_no": "P1", "code": "D2", "code_type": "DRUG", "ingredient": "ING-B",
         "treated_on": "2026-06-01", "patient_age": 40},
        {"chart_no": "P1", "code": "D3", "code_type": "DRUG", "ingredient": "ING-A",
         "treated_on": "2026-06-01", "patient_age": 40},
    ]
    rules = [_dur("CONTRA", "ING-A", "ING-B", reason="상호작용"),
             _dur("DUP"), _dur("AGE", "ING-A", min_age=65, reason="고령전용", sev="BLOCK")]
    f = eng.evaluate_dur(lines=lines, dur_rules=rules)
    kinds = {x.rule_code for x in f}
    assert "DUR-CONTRA" in kinds          # ING-A+ING-B 병용금기
    assert "DUR-DUP" in kinds             # ING-A가 D1·D3 두 코드 → 동일성분 중복
    assert "DUR-AGE" in kinds             # 연령 40 < 65 → 연령금기
    contra = next(x for x in f if x.rule_code == "DUR-CONTRA")
    assert contra.severity == "BLOCK" and contra.reason_code == "B-DUR"
    dup = next(x for x in f if x.rule_code == "DUR-DUP")
    assert dup.reason_group == "D" and dup.severity == "WARN"
    # 성분 미부여/비약품은 무시
    assert eng.evaluate_dur(lines=[{"chart_no": "P1", "code": "X", "code_type": "TARIFF"}],
                            dur_rules=rules) == []


def test_age_sex_auto_from_fee_master():
    """P1 G5 — params.code 미지정 시 fee_master 성/연령 제약으로 전 코드 자동 점검."""
    fee = {"GYN1": {"price": 1000, "cap": None, "type": "TARIFF", "sex": "F", "min_age": None, "max_age": None},
           "PED1": {"price": 1000, "cap": None, "type": "TARIFF", "sex": None, "min_age": None, "max_age": 6},
           "GEN1": {"price": 1000, "cap": None, "type": "TARIFF", "sex": None, "min_age": None, "max_age": None}}
    lines = [
        {"chart_no": "P1", "code": "GYN1", "sex": "M", "age": 40, "treated_on": "2026-06-01"},  # 성별위반
        {"chart_no": "P2", "code": "PED1", "sex": "M", "age": 30, "treated_on": "2026-06-01"},  # 연령상한 초과
        {"chart_no": "P3", "code": "GEN1", "sex": "M", "age": 40, "treated_on": "2026-06-01"},  # 제약없음 무적발
        {"chart_no": "P4", "code": "GYN1", "sex": "F", "age": 40, "treated_on": "2026-06-01"},  # 정상
    ]
    rule = _rule("PR-AGESEX-AUTO", "AGE_SEX_LIMIT", None, sev="WARN")
    f = eng.evaluate(lines=lines, rules=[rule], fee=fee)
    charts = {x.chart_no for x in f}
    assert "P1" in charts and "P2" in charts
    assert "P3" not in charts and "P4" not in charts
    assert all(x.reason_code == "B-AGESEX" for x in f)   # 거부사유 코드 연계(P0-2)


def test_dx_procedure_mismatch():
    """P1 G2 — 수가 필수 상병(KCD prefix) 미기재 형식 적발(차트 단위 상병 합집합)."""
    rule = SimpleNamespace(code="OP-CANCER", require_dx_prefixes="C,D0", name="암수술", reason="악성종양 상병 필요",
                           severity="WARN", active=True)
    # 같은 차트의 다른 라인에 상병이 있으면 충족(합집합)
    ok = [{"chart_no": "P1", "code": "OP-CANCER", "treated_on": "2026-06-01", "diagnosis_codes": "Z00"},
          {"chart_no": "P1", "code": "X", "treated_on": "2026-06-01", "diagnosis_codes": "C50"}]
    fok = eng.evaluate_dx(lines=ok, dx_rules=[rule])
    assert fok == []                       # C50이 prefix C 충족
    bad = [{"chart_no": "P2", "code": "OP-CANCER", "treated_on": "2026-06-01", "diagnosis_codes": "Z00,J20"}]
    fb = eng.evaluate_dx(lines=bad, dx_rules=[rule])
    assert len(fb) == 1 and fb[0].rule_code == "DX-MISMATCH" and fb[0].reason_code == "M-DX"


def test_bundling_unbundling():
    """P1 G1/G4 — 패키지 수가(parent)+세부/재료(child) 동시청구 시 언번들링 적발."""
    rb = SimpleNamespace(parent_code="PKG1", child_codes="C1,C2", rule_type="BUNDLE",
                         name="패키지 포함행위", reason="별도산정 불가", severity="WARN", active=True)
    rm = SimpleNamespace(parent_code="OP1", child_codes="M1", rule_type="MATERIAL",
                         name="수술포함 재료", reason=None, severity="WARN", active=True)
    lines = [
        {"chart_no": "P1", "code": "PKG1", "treated_on": "2026-06-01"},
        {"chart_no": "P1", "code": "C1", "treated_on": "2026-06-01"},   # 언번들링
        {"chart_no": "P1", "code": "C2", "treated_on": "2026-06-01"},   # 언번들링
        {"chart_no": "P2", "code": "OP1", "treated_on": "2026-06-01"},
        {"chart_no": "P2", "code": "M1", "treated_on": "2026-06-01"},   # 재료 별도산정
        {"chart_no": "P3", "code": "C1", "treated_on": "2026-06-01"},   # parent 없음 → 무적발
    ]
    f = eng.evaluate_bundle(lines=lines, bundle_rules=[rb, rm])
    codes = {(x.chart_no, x.code) for x in f}
    assert ("P1", "C1") in codes and ("P1", "C2") in codes
    assert ("P2", "M1") in codes
    assert ("P3", "C1") not in codes      # parent 미청구 → 적발 없음
    bundle = next(x for x in f if x.code == "C1")
    assert bundle.reason_code == "B-BUNDLE"
    material = next(x for x in f if x.code == "M1")
    assert material.reason_code == "B-MATERIAL"


def test_eligibility_invalid():
    """P1 G11 — 자격상실/미확인 환자 급여청구 적발(비급여는 제외)."""
    rule = _rule("PR-ELIG-AUTO", "ELIGIBILITY_INVALID", None, sev="WARN")
    lines = [
        {"chart_no": "P1", "code": "A", "coverage": "NHIS", "eligibility_status": "EXPIRED",
         "treated_on": "2026-06-01"},                                    # 자격상실 급여 → 적발
        {"chart_no": "P2", "code": "B", "coverage": "NHIS", "eligibility_status": "NOT_FOUND",
         "treated_on": "2026-06-01"},                                    # 미확인 급여 → 적발
        {"chart_no": "P3", "code": "C", "coverage": "NONCOVERED", "eligibility_status": "EXPIRED",
         "treated_on": "2026-06-01"},                                    # 비급여 → 제외
        {"chart_no": "P4", "code": "D", "coverage": "NHIS", "eligibility_status": "ACTIVE",
         "treated_on": "2026-06-01"},                                    # 정상 자격 → 무적발
    ]
    f = eng.evaluate(lines=lines, rules=[rule], fee={})
    charts = {x.chart_no for x in f}
    assert charts == {"P1", "P2"}
    assert all(x.reason_code == "B-ELIG" for x in f)


def test_grade_consistency():
    """P1 G6 — HIS 종별 vs ERP 마스터 불일치/미설정 적발(데이터 기반·#75)."""
    mism = [{"chart_no": "P1", "code": "A", "coverage": "NHIS", "institution_grade": "CLINIC"}]
    f = eng.evaluate_grade(lines=mism, grade="HOSPITAL")          # 불일치
    assert len(f) == 1 and f[0].rule_code == "GRADE-MISMATCH" and f[0].reason_code == "B-GRADE"
    assert eng.evaluate_grade(                                    # 일치 → 무적발
        lines=[{**mism[0], "institution_grade": "HOSPITAL"}], grade="HOSPITAL") == []
    fu = eng.evaluate_grade(lines=mism, grade=None)               # ERP 미설정 + HIS 종별
    assert len(fu) == 1 and fu[0].rule_code == "GRADE-UNSET"
    assert eng.evaluate_grade(                                    # HIS 종별 미동봉 → 점검 불가
        lines=[{"chart_no": "P2", "code": "B", "coverage": "NHIS"}], grade="HOSPITAL") == []
    assert eng.evaluate_grade(                                    # 비급여 제외
        lines=[{"chart_no": "P3", "code": "C", "coverage": "NONCOVERED",
                "institution_grade": "CLINIC"}], grade="HOSPITAL") == []


def test_dur_pregnancy():
    """P0-1 DUR — 임부금기(HIS pregnancy_status 수신분, #72)."""
    rules = [_dur("PREGNANCY", "ING-X", reason="임부금기약", sev="BLOCK")]
    preg = [{"chart_no": "P1", "code": "DX", "code_type": "DRUG", "ingredient": "ING-X",
             "treated_on": "2026-06-01", "pregnancy_status": "PREGNANT"}]
    f = eng.evaluate_dur(lines=preg, dur_rules=rules)
    assert len(f) == 1 and f[0].rule_code == "DUR-PREG" and f[0].severity == "BLOCK"
    # 비임신/미상은 무적발
    nonpreg = [{**preg[0], "pregnancy_status": "N"}]
    assert eng.evaluate_dur(lines=nonpreg, dur_rules=rules) == []
    assert eng.evaluate_dur(lines=[{**preg[0], "pregnancy_status": None}], dur_rules=rules) == []


def test_amount_outlier_underclaim_not_flagged():
    """과소청구(청구액<표준)는 삭감 리스크 아님 → 미적발. 과대청구만 적발(DRUG 상한초과-only와 정합)."""
    r = [_rule("PR-AMT", "AMOUNT_OUTLIER", {"tolerance": 0.1})]
    fee = {"AA001": 1000}
    # 과소청구 500 (표준 1000의 -50%) → 미적발(수정 전: abs편차로 오탐)
    under = eng.evaluate(lines=[{"chart_no": "P1", "code": "AA001", "count": 1, "amount": 500}],
                         rules=r, fee=fee)
    assert under == []
    # 과대청구 2000 (+100%) → 적발 유지
    over = eng.evaluate(lines=[{"chart_no": "P1", "code": "AA001", "count": 1, "amount": 2000}],
                        rules=r, fee=fee)
    assert len(over) == 1


def test_stat_outlier_small_sample_flags_outlier():
    """소표본(n=10) p95 백분위 붕괴 회귀 — 명확한 이상 단가가 임계=최댓값에 막혀 미탐되던 것 해소.

    수정 전: idx=round(0.95×9)=9=len-1 → 임계=최댓값×factor → 어떤 값도 초과 못해 0건(미탐).
    수정 후: 선형보간 p95(units[8]~[9] 사이)로 임계<최댓값 → 명확한 이상치 적발."""
    lines = ([{"chart_no": f"P{i}", "code": "AA001", "count": 1, "amount": 100} for i in range(9)]
             + [{"chart_no": "P9", "code": "AA001", "count": 1, "amount": 1000}])   # 명확한 이상치
    r = [_rule("PR-STAT", "STAT_OUTLIER",
               {"percentile": 95, "factor": 1.5, "min_samples": 10})]
    f = eng.evaluate(lines=lines, rules=r, fee={})
    assert len(f) == 1 and "1,000" in f[0].detail       # 이상치 1000 적발(수정 전 0건)


def test_stat_outlier_excludes_zero_negative_count():
    """단가 분포 — 취소(count=0)·조정(음수) 라인 제외(_norm_count·단가 왜곡 방지)."""
    from erp.claims.pre_review import _stat_outlier
    # count=0 라인은 unit 계산서 제외돼 표본에 안 들어감(min_samples 미달로 무적발)
    lines = [{"chart_no": f"P{i}", "code": "AA001", "count": 0, "amount": 100} for i in range(20)]
    r = _rule("PR-STAT", "STAT_OUTLIER", {"min_samples": 10})
    assert _stat_outlier(lines, r, {}) == []


def test_evaluate_dx_holds_when_no_diagnosis_at_all():
    """상병 전무(HIS 미동봉) 차트는 판정 보류 — 데이터갭 오탐 억제. 상병 있으나 미부합이면 적발 유지."""
    rule = SimpleNamespace(code="OP-CANCER", require_dx_prefixes="C,D0", name="암수술",
                           reason="악성종양 상병 필요", rule_type="DIAGNOSIS", active=True, severity="WARN")
    # 상병 전무(diagnosis_codes 없음) → 보류(수정 전: 필수상병 미기재 오탐)
    no_dx = [{"chart_no": "P1", "code": "OP-CANCER", "treated_on": "2026-06-01"}]
    assert eng.evaluate_dx(lines=no_dx, dx_rules=[rule]) == []
    # 상병은 있으나(J20) 필수(C/D0) 미부합 → 적발 유지
    mismatch = [{"chart_no": "P2", "code": "OP-CANCER", "treated_on": "2026-06-01",
                 "diagnosis_codes": "J20"}]
    assert len(eng.evaluate_dx(lines=mismatch, dx_rules=[rule])) == 1


def test_amount_outlier_skips_negative_count():
    """조정(음수 count) 라인은 금액이상 대상 아님 — 음수 count가 상한/편차 부호를 뒤집는 오탐 방지."""
    r = [_rule("PR-AMT", "AMOUNT_OUTLIER", {"tolerance": 0.1})]
    neg = [{"chart_no": "P1", "code": "AA001", "count": -2, "amount": -2000}]
    assert eng.evaluate(lines=neg, rules=r, fee={"AA001": 1000}) == []
    # 정상 라인은 여전히 적발(회귀 가드)
    ok = [{"chart_no": "P1", "code": "AA001", "count": 1, "amount": 2000}]
    assert len(eng.evaluate(lines=ok, rules=r, fee={"AA001": 1000})) == 1


def test_los_ratio_skips_drug_material():
    """일별 행위량 — 약제·재료 투여량(정·바이알)은 제외(_count_limit·_period_limit 동형)."""
    r = [_rule("PR-LOS", "LOS_RATIO", {"per_day_max": 2})]
    # 처방 30정(약제) 단독 → 행위량 상한 트립 안 됨
    drug = [{"chart_no": "P1", "code": "DRG", "code_type": "DRUG",
             "treated_on": "2026-05-01", "count": 30}]
    assert eng.evaluate(lines=drug, rules=r, fee={}) == []
    # 행위 라인 3건은 여전히 적발
    proc = [{"chart_no": "P1", "code": f"X{i}", "treated_on": "2026-05-01", "count": 1}
            for i in range(3)]
    assert len(eng.evaluate(lines=proc, rules=r, fee={})) == 1


def test_surcharge_check_utc_aware_converted_to_kst():
    """tz-aware 진료시각(UTC 저장)은 KST로 환산 후 야간 판정 — UTC .hour 오탐 해소."""
    from datetime import datetime, timezone
    r = [_rule("PR-SUR", "SURCHARGE_CHECK",
               {"surcharge_suffix": "010", "night_start": 20, "night_end": 8})]
    # 21:00 KST(월) = 12:00 UTC(aware) → 야간 → 적발 안 됨(미환산 시 hour=12로 오탐)
    night_kst = [{"chart_no": "P1", "code": "AA010",
                  "treated_at": datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)}]
    assert eng.evaluate(lines=night_kst, rules=r, fee={}) == []
    # 14:00 KST(월, 주간) = 05:00 UTC(aware) → 주간·평일 → 적발 유지
    day_kst = [{"chart_no": "P2", "code": "AA010",
                "treated_at": datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc)}]
    assert len(eng.evaluate(lines=day_kst, rules=r, fee={})) == 1
