"""사전심사 룰 시더 — JSON 파일을 claims 룰 테이블에 멱등 적재.

룰 엔진은 LIVE이나 룰 데이터가 비면 적발 0(무영향). 본 스크립트로 데이터를 적재하면
사전심사가 실제로 발화한다. 적재 로직은 `erp.claims.rule_loader`(라우터 EP와 SoT 공유)
재사용 — 멱등(재실행 안전)·동일 검증.

데이터 디렉터리(기본 seeds/prereview/)에서 룰 종류별 파일을 읽는다:
  ingredient_map.json  dur_rules.json  dx_rules.json  bundling_rules.json  surcharge_rates.json
각 파일 = 행(dict) 배열. 없는 파일은 건너뜀.

⚠️ 동봉 seeds/prereview/는 **구조 예시(SAMPLE)** — 식약처 DUR·HIRA 검증 데이터로 교체해야
운영 신뢰 가능(임상 타당성은 의료진 게이트·약제팀 확인). DUR 샘플은 source="SAMPLE"이라
`DELETE ... WHERE source='SAMPLE'`로 제거 가능.

사용:
  .venv/bin/python scripts/seed_prereview_rules.py [--dir DIR] [--dry-run] [--replace-source SRC]
대상: DATABASE_URL — **운영 DB 적재 시 데이터 출처·라이선스(식약처 유형2 비상업) 확인**.
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

_DEFAULT_DIR = os.path.join(os.path.dirname(__file__), "..", "seeds", "prereview")
_ORDER = ["ingredient_map", "dur_rules", "dx_rules", "bundling_rules", "surcharge_rates"]


def _load_file(data_dir: str, kind: str) -> list[dict] | None:
    path = os.path.join(data_dir, f"{kind}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    if not isinstance(rows, list):
        raise ValueError(f"{path}: 최상위는 행 배열(JSON array)이어야 합니다")
    return rows


async def main() -> None:
    ap = argparse.ArgumentParser(description="사전심사 룰 시더(멱등)")
    ap.add_argument("--dir", default=_DEFAULT_DIR, help="룰 JSON 디렉터리")
    ap.add_argument("--dry-run", action="store_true", help="검증·집계만(미적용)")
    ap.add_argument("--replace-source", default=None,
                    help="dur_rules 해당 출처 전체 교체(고시 갱신)")
    args = ap.parse_args()

    from erp.claims import rule_loader
    from erp.db import session_factory

    report: dict[str, int] = {}
    async with session_factory()() as session:
        for kind in _ORDER:
            raw = _load_file(args.dir, kind)
            if raw is None:
                continue
            RowModel, loader = rule_loader.LOADERS[kind]
            rows = [RowModel(**d) for d in raw]          # pydantic 검증(불량 행 조기 실패)
            if kind == "dur_rules":
                n = await loader(session, rows, replace_source=args.replace_source)
            else:
                n = await loader(session, rows)
            report[kind] = n
        if args.dry_run:
            await session.rollback()
            print("[dry-run] 검증 통과 — 미적용:", report or "(파일 없음)")
        else:
            await session.commit()
            print("적재 완료(멱등):", report or "(파일 없음)")
            print("→ 기존 청구 반영은 재-sync 필요(job_prereview_sync 또는 표적 sync). 런북 §5.")


if __name__ == "__main__":
    asyncio.run(main())
