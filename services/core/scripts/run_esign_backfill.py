"""e-sign 사번 매핑 백필 실행기 (HIS (나)·2026-07-24) — 컨테이너 내 1회 ops 실행.

HIS 디렉터리(employeeNumber+clinicUserId) 조인 → 서명자(ACTIVE 의료계약) 우선으로 HIS map EP에
ERP employee_no를 erpEmployeeCode로 백필. HIS 신규 작업 0(양키 OR 해소).

사용: python scripts/run_esign_backfill.py [--dry-run] [--all]
  --dry-run: 조인만·발신 없음 / --all: 서명자 외 재직 전원(기본=서명자 우선)
"""
import asyncio
import sys

from erp.db import session_factory
from erp.integrations.esign_emp_map import backfill


async def main(dry_run: bool, signatories_only: bool) -> None:
    async with session_factory()() as s:
        out = await backfill(s, actor=None, signatories_only=signatories_only, dry_run=dry_run)
        if not dry_run:
            await s.commit()
    print("▶ 백필 결과:")
    for k, v in out.items():
        print(f"    {k}: {v}")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv[1:]
    sig_only = "--all" not in sys.argv[1:]
    asyncio.run(main(dry, sig_only))
