"""청구액 대사(제출 vs 심사대상) 데모 정합 — 명세가 있는 진료월에 대응 제출건 시드. 멱등·TEST-.

**왜 필요한가**: prod 시드가 서로 다른 목적으로 따로 만들어져 두 소스의 진료월이 겹치지 않았다.
  - 제출(`claim_submissions`) = `seed_claim_payment_demo.py`가 **공단 지급대사** 데모용으로 2026-08만
  - 심사대상(`claim_statements`) = 진료과 손익·MEDRLT 데모가 2025-07~2026-07
→ `/claims` 청구액 대사가 **어느 달로 조회해도 한쪽이 0**이라 상시 적색 "불일치"였다. 개발 중에는
   이 상시 적색이 진짜 청구 누락 신호와 구분되지 않아 화면이 무의미해진다.

**하는 일**: 명세가 있는 각 진료월에 명세와 **1:1 대응하는 제출건**을 만든다(금액·건수 모두 일치).
  - GL 전표는 만들지 않는다. 대사는 `claim_submissions` vs `claim_statements` 비교라 전표가 불필요하고,
    12개월치 1140/4110 전표를 넣으면 `demo-sim`이 맞춰 놓은 손익·경영진 대시보드가 왜곡된다.
  - 2026-08(제출만 있고 통보서 미도착)은 **그대로 둔다** — 실제로 있을 수 있는 정상 진행 상태다.

사용: docker cp 후 `docker exec -w /app -e PYTHONPATH=/app/src infra-core-1 python /tmp/<이 파일>`
정리: `DELETE FROM claims.claim_submissions WHERE claim_id LIKE 'TEST-SUB-%';`
"""
import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, "/app/src")

from sqlalchemy import select  # noqa: E402

from erp.db import session_factory  # noqa: E402

SKIP_MONTHS = {"2026-08"}      # 지급대사 데모 소유 — 건드리지 않는다


def _submitted_on(care_month: str) -> date:
    """청구는 진료월 익월 초 제출이 관례 — 대사는 진료월 기준이라 표시용."""
    y, m = (int(x) for x in care_month.split("-"))
    return date(y + (m == 12), (m % 12) + 1, 5)


async def main() -> None:
    from erp.claims.models import ClaimBatch, ClaimStatement, ClaimSubmission

    async with session_factory()() as session:
        rows = (await session.execute(
            select(ClaimBatch.care_month, ClaimStatement.id, ClaimStatement.claimed_amt)
            .join(ClaimBatch, ClaimBatch.id == ClaimStatement.batch_id)
            .order_by(ClaimBatch.care_month, ClaimStatement.id))).all()

        existing = set((await session.scalars(
            select(ClaimSubmission.claim_id).where(
                ClaimSubmission.claim_id.like("TEST-SUB-%")))).all())

        made = skipped = 0
        totals: dict[str, int] = {}
        for care_month, stmt_id, amt in rows:
            if care_month in SKIP_MONTHS or not amt:
                continue
            cid = f"TEST-SUB-{care_month}-{stmt_id:05d}"    # 명세 1:1 (건수도 일치)
            if cid in existing:
                skipped += 1
                continue
            session.add(ClaimSubmission(
                claim_id=cid, claim_number=cid, care_month=care_month,
                claimed_amt=round(float(amt)), submitted_on=_submitted_on(care_month)))
            made += 1
            totals[care_month] = totals.get(care_month, 0) + round(float(amt))
        await session.commit()

        print(f"제출건 생성 {made}건 · 기존 유지 {skipped}건")
        for cm in sorted(totals):
            print(f"  {cm}  {totals[cm]:>15,}원")


asyncio.run(main())
