"""공시(고시·심사기준) 본문 일괄 임포터 → regulatory.notices + AI 발신 큐 자동 적재.

NoticeService.create가 AI 색인 발신 큐에 자동 적재 → worker-integration이 weve로 immediate push
→ notice_knowledge 색인. 즉 이 임포터로 실 고시를 넣으면 AI 검색·평가 근거가 실데이터로 채워짐.

CSV(UTF-8) 컬럼: notice_type, title, body_text, effective_from[, effective_to]
  notice_type: TARIFF|DRUG_MATERIAL|REVIEW_CRITERIA|MEDICAL_LAW|ADMIN|NARCOTIC|LABOR|INTERNAL
사용: python scripts/import_notices.py <csv 경로> [created_by=1]
"""
import asyncio
import csv
import sys
from datetime import date

from erp.db import session_factory
from erp.regulatory.service import NoticeService


def _d(s: str | None):
    s = (s or "").strip()
    return date.fromisoformat(s[:10]) if s else None


async def main(path: str, created_by: int = 1) -> None:
    svc = NoticeService()
    n = skipped = 0
    async with session_factory()() as s:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                nt = (row.get("notice_type") or "").strip()
                title = (row.get("title") or "").strip()
                body = (row.get("body_text") or row.get("body") or "").strip()
                ef = _d(row.get("effective_from"))
                if not (nt and title and body and ef):
                    skipped += 1
                    continue
                try:
                    await svc.create(s, notice_type=nt, title=title, body_text=body,
                                     effective_from=ef, effective_to=_d(row.get("effective_to")),
                                     created_by=created_by)
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"  스킵: {title[:30]} — {str(exc)[:60]}")
                    skipped += 1
        await s.commit()
    print(f"적재 완료: {n}건 공시 (AI 발신 큐 자동 적재 → weve push). 스킵 {skipped}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1))
