"""진료비 항목 분류 (D-6 P1c) — 급여 6분류 + 비급여 4분류.

명칭(fee_master.name) 키워드 기반 — 운영 코드명이 충실(예: '재진진찰료', '입원관리료').
fee_master.benefit_category에 1회 계산 저장(마이그 0052 백필) + 런타임 폴백 동일 규칙.

급여: 진찰료 / 입원료 / 식대 / 행위료(검사·영상·처치·수술·마취·주사·이학) / 약제비 / 치료재료대
비급여: 상급병실료차액 / 선택진료비 / 특수검사 / 제증명수수료 / 기타비급여
"""

# (key, 한글 라벨) — key는 fee_master.benefit_category 저장값
BENEFIT_LABELS = {
    "CONSULT": "진찰료", "ADMISSION": "입원료", "MEAL": "식대",
    "ACT": "행위료", "DRUG": "약제비", "MATERIAL": "치료재료대",
    "ROOM": "상급병실료차액", "SELECT": "선택진료비",
    "SPECIAL_EXAM": "특수검사", "CERTIFICATE": "제증명수수료",
    "ETC_NC": "기타비급여",
}
# 표시 순서 (명세 그룹 정렬)
BENEFIT_ORDER = ["CONSULT", "ADMISSION", "MEAL", "ACT", "DRUG", "MATERIAL",
                 "ROOM", "SELECT", "SPECIAL_EXAM", "CERTIFICATE", "ETC_NC"]


def classify_noncovered(name: str) -> str:
    n = name or ""
    if any(k in n for k in ("병실", "상급병상", "1인실", "특실", "병상")):
        return "ROOM"
    if any(k in n for k in ("선택진료", "특진")):
        return "SELECT"
    if any(k in n for k in ("진단서", "증명", "사본", "확인서", "제증명", "소견서")):
        return "CERTIFICATE"
    if any(k in n for k in ("검사", "검진", "초음파", "MRI", "CT", "내시경")):
        return "SPECIAL_EXAM"
    return "ETC_NC"


def classify_benefit(code_type: str | None, name: str | None) -> str:
    """code_type + 명칭 → benefit_category key."""
    n = name or ""
    if code_type == "DRUG":
        return "DRUG"
    if code_type == "MATERIAL":
        return "MATERIAL"
    if code_type == "NONCOVERED":
        return classify_noncovered(n)
    # 급여 수가(TARIFF)
    if any(k in n for k in ("식대", "식사", "경관영양")):
        return "MEAL"
    if any(k in n for k in ("입원료", "입원관리", "입원환자", "격리실", "병실료")):
        return "ADMISSION"
    if any(k in n for k in ("진찰료", "외래관리료")):
        return "CONSULT"
    return "ACT"   # 검사·영상·처치·수술·마취·주사·이학 등 급여 행위


def label(cat: str) -> str:
    return BENEFIT_LABELS.get(cat, cat)


# ── 임상영역 분류(심사평가용) — 청구 급여분류와 별개의 진료행위 축.
#    급여분류는 청구 소계용(행위료에 검사·처치·수술·영상 혼재), 임상분류는 심사 스캔용 분리.
CLINICAL_LABELS = {
    "CONSULT": "진찰", "PROC": "처치·수술", "LAB": "검사", "IMAGING": "영상",
    "INJECT": "주사", "ANES": "마취", "PT": "이학요법", "DRUG": "투약",
    "MATERIAL": "재료", "ADMISSION": "입원", "MEAL": "식대",
    "NONCOVERED": "비급여", "ETC": "기타",
}
CLINICAL_ORDER = ["CONSULT", "PROC", "LAB", "IMAGING", "INJECT", "ANES", "PT",
                  "DRUG", "MATERIAL", "ADMISSION", "MEAL", "NONCOVERED", "ETC"]


def classify_clinical(code_type: str | None, name: str | None) -> str:
    """code_type + 명칭 → 임상영역 key(진찰/처치·수술/검사/영상/주사/마취/이학/투약/재료…).

    명칭 키워드 우선순위(구체 먼저). 약품·재료는 code_type로 확정.
    """
    if code_type == "DRUG":
        return "DRUG"
    if code_type == "MATERIAL":
        return "MATERIAL"
    n = name or ""
    if any(k in n for k in ("물리치료", "재활", "도수치료", "온열", "전기자극", "운동치료")):
        return "PT"
    if "마취" in n:
        return "ANES"
    if any(k in n for k in ("촬영", "CT", "MRI", "초음파", "방사선", "엑스선", "X선",
                            "조영", "투시", "핵의학", "영상")):
        return "IMAGING"
    if any(k in n for k in ("검사", "검체", "면역", "배양", "항체", "항원", "병리",
                            "도말", "동정", "현미경", "PCR", "염색체", "유전자")):
        return "LAB"
    if any(k in n for k in ("주사", "수액", "주입", "정맥내", "근육내", "피하주")):
        return "INJECT"
    if any(k in n for k in ("수술", "절제", "고정", "정복", "절개", "봉합", "성형",
                            "이식", "삽입", "적출", "생검", "천자", "드레싱", "처치",
                            "소작", "결찰", "내시경적")):
        return "PROC"
    if any(k in n for k in ("진찰료", "외래관리", "재진", "초진")):
        return "CONSULT"
    if any(k in n for k in ("입원료", "입원관리", "병실", "격리실")):
        return "ADMISSION"
    if any(k in n for k in ("식대", "식사", "경관영양")):
        return "MEAL"
    if code_type == "NONCOVERED":
        return "NONCOVERED"
    return "ETC"


def clinical_label(cat: str) -> str:
    return CLINICAL_LABELS.get(cat, cat)


def time_surcharge(treated_at) -> str | None:
    """진료시간 가산 판정 (표시용) — 야간(18시~익일09시)·공휴(일요일).

    가산 코드 자동 추가는 HIS(EDI) 영역 — ERP는 명세에 시간대 표기만.
    법정공휴일은 후속(공휴일 캘린더) — 현재 일요일 기준.
    """
    if treated_at is None:
        return None
    at = treated_at
    if getattr(at, "tzinfo", None) is not None:   # tz-aware(UTC 저장·asyncpg) → KST 환산 후 판정
        from zoneinfo import ZoneInfo
        at = at.astimezone(ZoneInfo("Asia/Seoul"))
    tags = []
    if at.hour >= 18 or at.hour < 9:
        tags.append("야간")
    if at.weekday() == 6:
        tags.append("공휴")
    return "·".join(tags) or None
