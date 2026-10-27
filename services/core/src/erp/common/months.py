"""급여월·귀속월 표기 단일 소스 — 저장형은 "YYYY-MM" 하나.

2026-10-10: 라우터가 "2026-07"·"202607"을 둘 다 받아 그대로 저장해, 같은 직원·같은 달에 두 행이 생길 수 있었다
(유니크=직원+원문) — 조회는 한쪽만 보고, 슬라이스 [5:7]은 압축형에서 틀린 월을 읽었다. 진입점(경로·질의·본문)
에서 정규형으로 바꾸고, DB CHECK(0258)가 그 밖의 경로를 막는다.
"""
import re
from typing import Annotated

from pydantic import AfterValidator

_YM = re.compile(r"(\d{4})-?(0[1-9]|1[0-2])", re.ASCII)   # 전각·아랍 숫자 거부


def canonical_ym(value: str) -> str:
    """"2026-07"·"202607" → "2026-07". 그 밖(월 13·자릿수 틀림·공백 포함)은 ValueError."""
    m = _YM.fullmatch(str(value).strip())
    if m is None:
        raise ValueError(f"월은 YYYY-MM 형식이어야 합니다: {str(value)[:20]!r}")
    return f"{m.group(1)}-{m.group(2)}"


# FastAPI 경로·질의·본문 필드용 — 잘못된 값은 422
YearMonth = Annotated[str, AfterValidator(canonical_ym)]


def require_ym(value: str) -> str:
    """서비스 진입점용 — canonical_ym과 같고 실패는 422(ValidationError)."""
    from erp.errors import ValidationError
    try:
        return canonical_ym(value)
    except ValueError as e:
        raise ValidationError(str(e)) from None


def ym_check_sql(col: str) -> str:
    """DB CHECK 식 — 저장형 "YYYY-MM"(자리마다 ASCII 숫자·월 01~12). sqlite·PG 공통 — 정규식·BETWEEN 대신 IN 목록
    (PG 로캘 정렬에서 BETWEEN '0' AND '9'는 전각 숫자를 통과시킬 수 있다)."""
    digit = "('0','1','2','3','4','5','6','7','8','9')"
    digits = " AND ".join(f"substr({col}, {i}, 1) IN {digit}" for i in (1, 2, 3, 4, 6, 7))
    months = "(" + ",".join(f"'{m:02d}'" for m in range(1, 13)) + ")"
    return (f"length({col}) = 7 AND substr({col}, 5, 1) = '-' AND {digits} "
            f"AND substr({col}, 6, 2) IN {months}")
