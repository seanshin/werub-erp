"""심평원 약가마스터_의약품표준코드 CSV → regulatory.drug_std_codes 적재 (전량 교체).

표준코드(GS1 바코드)↔품목기준코드↔제품코드(보험 EDI)↔성분코드 브리지 마스터.
공공데이터 공개 CSV(CP949) 그대로 적재 — 버전 파일 단위 전량 교체(DELETE→INSERT)라
재실행 멱등. 판본은 source_file 컬럼으로 추적.

사용: python scripts/import_drug_std_codes.py <csv 경로>
컬럼(0-base): 0 한글상품명 · 1 업체명 · 2 약품규격 · 3 제품총수량 · 4 제형구분 · 6 품목기준코드 ·
8 전문일반구분 · 9 대표코드 · 10 표준코드 · 11 제품코드(개정후) · 12 일반명코드 · 14 취소일자 ·
19 ATC코드 · 20 특수관리약품구분
"""
import asyncio
import csv
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import delete, insert  # noqa: E402

from erp.db import session_factory  # noqa: E402
from erp.regulatory.models import DrugStdCode  # noqa: E402

BATCH = 5_000


def _s(v: str | None, limit: int) -> str | None:
    s = (v or "").strip()
    return s[:limit] or None


def _d(v: str | None) -> date | None:
    s = (v or "").strip()
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _i(v: str | None) -> int | None:
    s = (v or "").strip().replace(",", "")
    try:
        return int(float(s)) if s else None
    except ValueError:
        return None


def parse_rows(path: str, *, source_file: str) -> dict[str, dict]:
    """CSV → {std_code: row dict} — 표준코드 dedupe(마지막 행 우선·공백 strip)."""
    seen: dict[str, dict] = {}
    skipped = 0
    with open(path, encoding="cp949", errors="replace", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)                       # 헤더
        for row in reader:
            if len(row) < 21:
                skipped += 1
                continue
            std = (row[10] or "").strip()
            name = (row[0] or "").strip()
            if not std or not name:               # 표준코드/상품명 없는 행은 브리지 불가
                skipped += 1
                continue
            seen[std] = {
                "std_code": std[:14], "item_seq": _s(row[6], 12),
                "product_code": _s(row[11], 12), "ingredient_code": _s(row[12], 12),
                "name": name[:200], "company": _s(row[1], 120), "spec": _s(row[2], 120),
                "pack_qty": _i(row[3]), "form": _s(row[4], 40), "rx_otc": _s(row[8], 20),
                "rep_code": _s(row[9], 14), "atc": _s(row[19], 80),
                "special_class": _s(row[20], 60), "canceled_on": _d(row[14]),
                "source_file": source_file[:120],
            }
    print(f"파싱: 유효 {len(seen):,}건 (스킵 {skipped:,})")
    return seen


async def main(path: str) -> None:
    source_file = os.path.basename(path)
    rows = list(parse_rows(path, source_file=source_file).values())
    async with session_factory()() as session:
        await session.execute(delete(DrugStdCode))          # 전량 교체(버전 파일 단위)
        for i in range(0, len(rows), BATCH):
            await session.execute(insert(DrugStdCode), rows[i:i + BATCH])
            print(f"  적재 {min(i + BATCH, len(rows)):,}/{len(rows):,}", end="\r")
        await session.commit()
    with_product = sum(1 for r in rows if r["product_code"])
    canceled = sum(1 for r in rows if r["canceled_on"])
    print(f"\n완료: {len(rows):,}건 적재 (제품코드 보유 {with_product:,}·취소 {canceled:,}) "
          f"— source_file={source_file}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("사용: python scripts/import_drug_std_codes.py <csv 경로>")
    asyncio.run(main(sys.argv[1]))
