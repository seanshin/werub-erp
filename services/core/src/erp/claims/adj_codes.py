"""심사조정 사유별 코드 분류 (FR-39) — references/hira-edi-spec/adjustment-reason-codes.md.

코드 첫 알파벳 = 사유 대분류. ⚠️ 실무 해설(2차 자료) 기반 — 실물 통보서 대조 시 보정.
F(증빙 미제출)·R(장비 미신고)은 신고 보완 후 재청구 가능 → 이의신청 우선순위 힌트.
"""

ADJ_CATEGORIES: dict[str, str] = {
    "A": "금액 산정착오",
    "B": "요양급여기준 적용착오",
    "C": "기준 범위 초과·처방내역 미확인",
    "D": "계산착오",
    "E": "비급여·100분의100 본인부담",
    "F": "구입 증빙자료 미제출",
    "G": "중복청구",
    "K": "산정코드 착오·누락",
    "R": "의료장비 미신고",
    "S": "범위초과 처방의약품",
}

# 보완(신고·증빙) 후 재심사조정청구로 회수 가능성이 높은 분류 — 이의신청 우선 검토 대상
RECOVERABLE = {"F", "R", "D", "K"}


def categorize(adj_code: str | None) -> tuple[str, str]:
    """조정코드 → (대분류 코드, 라벨). 미상은 ('?', '미분류')."""
    if not adj_code:
        return "?", "미분류"
    letter = adj_code.strip()[:1].upper()
    return (letter, ADJ_CATEGORIES[letter]) if letter in ADJ_CATEGORIES \
        else ("?", "미분류")
