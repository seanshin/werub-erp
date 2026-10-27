"""약제급여목록 및 급여상한금액표 (보건복지부/심평원 고시) → regulatory.fee_master(code_type=DRUG).

공식 공개 Excel을 ERP 약가 마스터로 적재. 첫 로드는 전부 신규 코드라 벌크 insert.
멱등: source_version(FeeSyncRun) — 동일 버전 재실행 시 skip. 후속 약가 개정은 FeeSyncService.apply
(close-then-open)로.

사용: python scripts/import_drug_benefits.py <xlsx 경로> [valid_from=YYYY-MM-DD]
컬럼(0-base): 2 분류 · 7 주성분명 · 8 제품코드 · 9 제품명 · 10 업체명 · 11 규격 · 12 단위 · 13 상한금액
"""
import asyncio
import sys
from datetime import date, datetime, timezone

import openpyxl
from sqlalchemy import select

from erp.db import session_factory
from erp.regulatory.models import FeeMaster
from erp.regulatory.sync import FeeSyncRun


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


async def main(path: str, valid_from_str: str) -> None:
    vf = date.fromisoformat(valid_from_str)
    source_version = f"DRUG-{valid_from_str}"
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active

    seen: dict[str, FeeMaster] = {}      # 제품코드 dedupe (마지막 행 우선)
    skipped = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        code = _s(row[8])
        price = row[13]
        if not code or price in (None, ""):
            skipped += 1
            continue
        try:
            price = float(str(price).replace(",", ""))
        except (TypeError, ValueError):
            skipped += 1
            continue
        crit = f"{_s(row[7])} | {_s(row[10])} | {_s(row[11])}{_s(row[12])} | 분류{_s(row[2])}"
        seen[code] = FeeMaster(code=code, code_type="DRUG", name=_s(row[9]) or code,
                               unit_price=price, price_cap=price, criteria_text=crit[:500],
                               valid_from=vf)

    async with session_factory()() as s:
        if await s.scalar(select(FeeSyncRun).where(FeeSyncRun.source_version == source_version)):
            print(f"이미 로드됨(멱등): {source_version}")
            return
        items = list(seen.values())
        for i in range(0, len(items), 2000):
            s.add_all(items[i:i + 2000])
            await s.flush()
        s.add(FeeSyncRun(source_version=source_version,
                         finished_at=datetime.now(timezone.utc), row_count=len(items)))
        await s.commit()
    print(f"적재 완료: {len(items)}건 DRUG @ {valid_from_str} (중복/누락 {skipped} 제외)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "2026-06-01"))
