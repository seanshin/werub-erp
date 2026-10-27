"""공식 수가/치료재료 Excel → regulatory.fee_master (TARIFF/MATERIAL). 파일명으로 설정 자동선택.

스트리밍 적재(대량 413K 대응)·중복(code,valid_from) 스킵·멱등(FeeSyncRun source_version).
사용: python scripts/import_fee_excel.py <xlsx 경로> [설정키(수가반영내역|치료재료)]
"""
import asyncio
import os
import sys
from datetime import date, datetime, timezone

import openpyxl
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from erp.db import session_factory
from erp.regulatory.models import FeeMaster
from erp.regulatory.sync import FeeSyncRun

# 파일명 키워드 → 설정 (열 인덱스 0-base)
CONFIGS = {
    "수가반영내역": dict(code_type="TARIFF", version="TARIFF-2026-06-01",
                     sheets=["의치과_급여_전체", "의치과_비급여_전체", "한방_급여_전체",
                             "약국_급여_전체"],
                     code=0, name=3, price=7, date=1, crit=[2], default_from="2026-06-01"),
    "치료재료": dict(code_type="MATERIAL", version="MATERIAL-DRG-2026-06-01",
                  sheets=["치료재료별도보상목록"],
                  code=3, name=4, price=None, date=7, crit=[2, 10, 11, 5, 6],
                  benefit=5, reward=6, default_from="2026-06-01"),  # 급여구분·보상률
    "응급가산": dict(code_type="TARIFF", version="TARIFF-EMERG-2026-04-01",
                  sheets=["별표2_전체", "별표3_전체"], min_row=6,
                  code=1, name=2, price=8, date=10, crit=[0, 3, 4],
                  default_from="2026-04-01"),
    "행위별도보상": dict(code_type="TARIFF", version="TARIFF-DRGACT-2026-05-01",
                    sheets=["행위별도보상목록"], min_row=2,
                    code=2, name=3, price=None, date=7, crit=[1, 4, 5, 10],
                    default_from="2026-05-01"),
}


def _pick(fname: str):
    for k, cfg in CONFIGS.items():
        if k in fname:
            return cfg
    return None


def _parse_date(v, default: date) -> date:
    if isinstance(v, datetime):
        return v.date()
    s = str(v or "").strip().replace(".", "-").replace("/", "-")
    try:
        if len(s) == 8 and s.isdigit():          # YYYYMMDD
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        if len(s) >= 10:
            return date.fromisoformat(s[:10])
    except ValueError:
        pass
    return default


def _get(row, i):
    return row[i] if i is not None and i < len(row) else None


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


async def main(path: str, key: str | None = None) -> None:
    cfg = CONFIGS.get(key) if key else _pick(os.path.basename(path))
    if cfg is None:
        print("설정 매칭 실패:", key or os.path.basename(path), "| 키:", list(CONFIGS))
        return
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    default_from = date.fromisoformat(cfg["default_from"])

    async def _flush(s, batch):
        stmt = pg_insert(FeeMaster.__table__).values(batch).on_conflict_do_nothing(
            index_elements=["code", "code_type", "valid_from"])  # 기존/중복 자동 스킵
        await s.execute(stmt)

    async with session_factory()() as s:
        if await s.scalar(select(FeeSyncRun).where(FeeSyncRun.source_version == cfg["version"])):
            print(f"이미 로드됨(멱등): {cfg['version']}")
            return
        seen: set = set()
        total = skipped = 0
        batch: list = []
        for sn in cfg["sheets"]:
            if sn not in wb.sheetnames:
                continue
            for row in wb[sn].iter_rows(min_row=cfg.get("min_row", 2), values_only=True):
                cv = _get(row, cfg["code"])
                code = str(cv).strip() if cv is not None else ""
                if not code:
                    continue
                vf = _parse_date(_get(row, cfg["date"]), default_from)
                key = (code, vf)
                if key in seen:                  # 같은 실행 내 (code,vf) 중복만 컷
                    skipped += 1
                    continue
                seen.add(key)
                price = _num(_get(row, cfg["price"])) if cfg["price"] is not None else 0.0
                nv = _get(row, cfg["name"])
                crit = " | ".join(str(_get(row, c)).strip() for c in cfg["crit"]
                                  if _get(row, c) not in (None, ""))[:500]
                rr = _num(_get(row, cfg["reward"])) if cfg.get("reward") is not None else None
                batch.append({"code": code, "code_type": cfg["code_type"],
                              "name": (str(nv).strip() if nv else code), "unit_price": price,
                              "price_cap": price or None, "criteria_text": crit or None,
                              "benefit_type": (str(_get(row, cfg["benefit"])).strip()
                                               if cfg.get("benefit") is not None
                                               and _get(row, cfg["benefit"]) else None),
                              "reward_rate": rr if rr else None, "valid_from": vf})
                total += 1
                if len(batch) >= 4000:           # asyncpg 파라미터 한계(32767) — 4000×8열<32767
                    await _flush(s, batch)
                    batch.clear()
        if batch:
            await _flush(s, batch)
        s.add(FeeSyncRun(source_version=cfg["version"],
                         finished_at=datetime.now(timezone.utc), row_count=total))
        await s.commit()
    print(f"적재 시도: {total}건 {cfg['code_type']} ({cfg['version']}), "
          f"중복컷 {skipped} (기존충돌은 ON CONFLICT 스킵)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
