"""부서 경계 해소(커널) — dept_id(common.departments PK)를 dept_code 후보로 변환.

HIS가 보내는 dept_code 체계가 도메인마다 다름:
  - claims(billing_lines·pre_review_cases): 부서명 "내과"
  - patient(receptions·reservations): 진료과코드 "IM"
사용자의 dept_id는 정수 PK라 직접 비교 불가(`"내과" == str(2)` 영원히 불일치).
→ dept_id를 common.departments로 해소해 **부서명·코드 둘 다** 후보 반환(양 체계 수용).

claims·patient 등 도메인 공통 — common은 커널(도메인이 의존). 빈 리스트=과차단 안전.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.models import Department


async def dept_code_candidates(session: AsyncSession, dept_id: int | None) -> list[str]:
    """dept_id → 매칭 후보(부서명·진료과코드). 미상/미해소는 [](아무것도 매칭 안 함=과차단)."""
    if dept_id is None:
        return []
    d = await session.get(Department, dept_id)
    return [x for x in ((d.name, d.code) if d else ()) if x]
