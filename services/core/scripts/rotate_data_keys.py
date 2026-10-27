"""컬럼 암호화 키 회전 재암호화 도구 (P2-6).

구 버전(예: v1) 키로 암호화된 모든 PII/시크릿 컬럼 blob을 현재 버전(예: v2) 키로 재암호화한다.
복호는 blob에 박힌 구 버전 키로, 재암호화는 현재 버전 키로 — 동일 AAD(행 바인딩) 유지.

전제(런북 docs/07-ops/data-key-rotation.md):
  1) DATA_ENC_KEY_V2 주입 + DATA_ENC_KEY(v1) 유지 → 배포(구·신 키 공존, 신규는 아직 v1)
  2) DATA_ENC_KEY_VERSION=2 로 승격 → 배포(신규 암호화는 v2, 구 v1 blob은 복호 가능)
  3) ★ 본 스크립트 실행 → 잔존 v1 blob 전부 v2로 재암호화
  4) (후일) v1 키 제거 → 배포

실행:
  docker cp .../scripts/rotate_data_keys.py infra-core-1:/tmp/
  docker exec -w /app -e PYTHONPATH=/app/src infra-core-1 python /tmp/rotate_data_keys.py --dry-run
  docker exec -w /app -e PYTHONPATH=/app/src infra-core-1 python /tmp/rotate_data_keys.py --apply
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, "/app/src")

from sqlalchemy import select  # noqa: E402

from erp.common import crypto  # noqa: E402
from erp.db import session_factory  # noqa: E402


def _registry():
    """(모델, 암호화 컬럼 속성, AAD 접두) — 전 암호화 컬럼 인벤토리(신규 컬럼 추가 시 등록)."""
    from erp.hr.models import CompensationProfile, Employee
    from erp.itam.models import SoftwareService
    from erp.patient.models import Patient
    from erp.scm.models import Supplier
    return [
        (Patient, "phone_enc", "patient.patients.phone"),
        (Employee, "phone_enc", "hr.employees.phone"),
        (Employee, "address_enc", "hr.employees.address"),
        (CompensationProfile, "bank_account_enc", "hr.compensation_profiles.bank_account"),
        (CompensationProfile, "resident_id_enc", "hr.compensation_profiles.resident_id"),
        (SoftwareService, "license_key_enc", "itam.software_services.license_key"),
        (SoftwareService, "api_token_enc", "itam.software_services.api_token"),
        (Supplier, "tax_id_enc", "scm.suppliers.tax_id"),
        (Supplier, "bank_account_enc", "scm.suppliers.bank_account"),
        (Supplier, "bank_swift_enc", "scm.suppliers.bank_swift"),
    ]


async def _process(session, model, attr, aad_prefix, *, apply: bool) -> dict:
    col = getattr(model, attr)
    rows = (await session.scalars(select(model).where(col.is_not(None)))).all()
    by_ver: dict[int, int] = {}
    rotated = 0
    for row in rows:
        blob = getattr(row, attr)
        if not blob:
            continue
        ver = crypto.key_version_of(blob)
        by_ver[ver] = by_ver.get(ver, 0) + 1
        aad = crypto.column_aad(aad_prefix, row.id)
        new_blob = crypto.reencrypt_column(blob, aad=aad)   # 현재 버전이면 None
        if new_blob is not None:
            rotated += 1
            if apply:
                setattr(row, attr, new_blob)
    return {"column": aad_prefix, "total": len(rows), "by_version": by_ver, "rotated": rotated}


async def main(apply: bool) -> None:
    cur = crypto.current_key_version()
    print(f"현재 키 버전 = v{cur} · 모드 = {'APPLY' if apply else 'DRY-RUN'}")
    grand = 0
    async with session_factory()() as session:
        for model, attr, aad_prefix in _registry():
            res = await _process(session, model, attr, aad_prefix, apply=apply)
            grand += res["rotated"]
            vers = ", ".join(f"v{v}:{n}" for v, n in sorted(res["by_version"].items()))
            print(f"  {res['column']:<42} total={res['total']:>5} [{vers}] "
                  f"재암호화={res['rotated']}")
        if apply:
            await session.commit()
            print(f"✔ 커밋 완료 — 총 {grand}건 v{cur}로 재암호화")
        else:
            print(f"(dry-run) 재암호화 대상 총 {grand}건 — 적용하려면 --apply")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="실제 재암호화·커밋")
    g.add_argument("--dry-run", action="store_true", help="스캔만(기본)")
    args = ap.parse_args()
    asyncio.run(main(apply=args.apply))
