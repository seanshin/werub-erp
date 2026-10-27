"""성과급 산정엔진 (P4-3) — 계약 산식 × 진료실적 → 성과급액. 순수 함수.

산식(계약 incentive_type):
- NET_COLLECTION: 순수입 × 배분율%
- TARGET_EXCESS: max(순수입 − 목표, 0) × 배분율%
- DEPT_SHARE: 진료과 순이익 × 기여율%(params.dept_net 입력)
- RVU: RVU점수 × 단가(params.rvu_points·rvu_unit)
공통: floor(하한)·cap(상한) 적용. 음수 0.
"""


# 인사평가 등급 → 성과급 배율(P1-1·0232). 등급 없음/미연계=1.0(무영향).
GRADE_FACTORS = {"S": 1.2, "A": 1.1, "B": 1.0, "C": 0.9, "D": 0.8}


def grade_factor(grade: str | None) -> float:
    return GRADE_FACTORS.get((grade or "").upper(), 1.0)


def calculate(*, incentive_type: str, incentive_rate: float | None,
              params: dict | None, net_revenue: float,
              eval_grade: str | None = None) -> dict:
    p = params or {}
    rate = (incentive_rate or 0) / 100
    if incentive_type == "NET_COLLECTION":
        raw = net_revenue * rate
    elif incentive_type == "TARGET_EXCESS":
        raw = max(net_revenue - float(p.get("target", 0)), 0) * rate
    elif incentive_type == "DEPT_SHARE":
        raw = float(p.get("dept_net", net_revenue)) * rate
    elif incentive_type == "RVU":
        raw = float(p.get("rvu_points", 0)) * float(p.get("rvu_unit", 0))
    else:                                    # NONE 등
        raw = 0.0
    raw = max(raw, 0.0)
    # 등급 배율 — floor/cap 전 raw에 적용(성과 반영을 상하한 판정에 포함)
    gf = grade_factor(eval_grade) if eval_grade else 1.0
    graded = raw * gf
    floor, cap = p.get("floor"), p.get("cap")
    adjusted = graded
    if floor is not None:
        adjusted = max(adjusted, float(floor))
    if cap is not None:
        adjusted = min(adjusted, float(cap))
    return {"incentive_type": incentive_type, "rate_pct": incentive_rate,
            "net_revenue": net_revenue, "raw_amount": int(round(raw)),
            "eval_grade": (eval_grade or None), "grade_factor": gf,
            "graded_amount": int(round(graded)),
            "amount": int(round(adjusted)),
            "capped": cap is not None and graded > float(cap),
            "floored": floor is not None and graded < float(floor)}
