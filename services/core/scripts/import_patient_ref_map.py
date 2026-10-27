"""Phase0 가명 토큰 대사표(patientRef ↔ chartNo) 이관 배치 (HIS 통지 2026-07-24).

HIS 전달물:
  - 키: /home/deploy/pseudonym-key-for-erp.env (0600) — md5 앞12 `f85cc2502a20` 상호검증 후 삭제
  - 대사표: /home/deploy/patient-ref-map-for-erp.csv (0600) — 컬럼 chartNo,patientRef (873명)

동작: 키 지문 확인 → CSV 파싱 → 각 행 patientRef=HMAC-SHA256(chartNo,key)[:16] 재계산 검증 →
      integration.patient_ref_map 멱등 이관 → 결과 리포트(이관·검증·불일치·총건수).
키는 PSEUDONYM_KEY(env) 또는 --key-file 경로에서 읽는다(둘 다 없으면 config.pseudonym_key).

사용:
  python scripts/import_patient_ref_map.py <csv경로> [--key-file <경로>] [--no-verify] [--dry-run]
  예) PSEUDONYM_KEY=$(grep -oP '(?<=PSEUDONYM_KEY=).*' /home/deploy/pseudonym-key-for-erp.env) \
        python scripts/import_patient_ref_map.py /home/deploy/patient-ref-map-for-erp.csv
"""
import asyncio
import csv
import os
import sys

from erp.config import get_settings
from erp.db import session_factory
from erp.integrations import patient_ref as pr


def _read_rows(path: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rdr = csv.DictReader(f)
        # 헤더 정규화(chartNo/chart_no, patientRef/patient_ref 수용)
        for r in rdr:
            cn = (r.get("chartNo") or r.get("chart_no") or "").strip()
            pref = (r.get("patientRef") or r.get("patient_ref") or "").strip()
            if cn and pref:
                rows.append((cn, pref))
    return rows


async def main(path: str, *, key: str | None, verify: bool, dry_run: bool) -> None:
    if key is None:
        key = get_settings().pseudonym_key
    if key:
        print(f"▶ 키 지문 md5[:12] = {pr.key_fingerprint(key)}  (HIS 전달값 f85cc2502a20 과 대조)")
    elif verify:
        print("⚠ 키 미제공 — 검증 없이 이관하려면 --no-verify. 중단.")
        sys.exit(2)

    rows = _read_rows(path)
    print(f"▶ CSV {path}: {len(rows)}행")
    if dry_run:
        # 발신·기록 없이 검증만 — 키·표 정합 사전 확인
        ok = bad = 0
        for cn, pref in rows:
            if key and pr.token_for(cn, key) == pref:
                ok += 1
            else:
                bad += 1
        print(f"  [dry-run] 검증 일치 {ok} · 불일치 {bad} (DB 미변경)")
        return

    async with session_factory()() as s:
        out = await pr.ingest_map(s, rows, key=key, verify=verify)
        await s.commit()
    print("▶ 이관 결과:")
    for k, v in out.items():
        print(f"    {k}: {v}")
    if out.get("mismatched"):
        print("  ⚠ 불일치 행 존재 — 키/표 버전 확인 필요(이관에서 제외됨).")
    else:
        print("  ✔ 전 행 검증 일치·이관 완료.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    csv_path = args[0]
    key_file = None
    verify = True
    dry_run = False
    i = 1
    while i < len(args):
        a = args[i]
        if a == "--key-file":
            key_file = args[i + 1]
            i += 2
        elif a == "--no-verify":
            verify = False
            i += 1
        elif a == "--dry-run":
            dry_run = True
            i += 1
        else:
            i += 1
    key_val = os.environ.get("PSEUDONYM_KEY")
    if key_file:
        with open(key_file, encoding="utf-8") as f:
            txt = f.read().strip()
        # KEY=값 형식이면 값만, 아니면 전체를 키로
        key_val = txt.split("=", 1)[1].strip() if txt.startswith("PSEUDONYM_KEY=") else txt
    asyncio.run(main(csv_path, key=key_val, verify=verify, dry_run=dry_run))
