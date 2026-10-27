"""동일인 중복 직원 레코드 병합 — HIS 디렉터리를 정본으로 (B-2 선결).

배경:
  ERP에 같은 사람이 두 번 있다. 초기 동기화가 만든 행(`C0xxx` 사번·이름 뒤 `_xxxx` 접미)이
  실제 `clinicUserId`를 보유하고, 병원 정식 사번 행(`EMP-*`/`BF-*`)은 그 값이 비어 있다.
  그래서 B-2 백필(`backfill_external_ids`)이 "이미 다른 직원이 보유" 사유로 78건을 전부 거부했다.
  양쪽 모두 급여·계약·근태 데이터를 갖고 있어 매핑만 옮겨서는 신원이 계속 갈린다.

정본 규칙(사용자 결정):
  HIS 디렉터리의 `employeeNumber`를 정본으로 삼는다.
  · 생존 행(survivor) = `employee_no == employeeNumber`
  · 병합 소멸 행(dup)   = `external_user_id == clinicUserId`
  종속 데이터를 survivor로 이관한 뒤 dup을 삭제하고, `external_user_id`를 survivor로 옮긴다.

안전 장치:
  · **이름이 다르면 병합하지 않는다.** dup 이름의 `_xxxx` 접미를 떼고 대조해 불일치는 보고만 한다
    (동일인 확인 없이 급여·계약을 합치면 되돌릴 수 없다).
  · 참조 컬럼과 유니크 제약을 **런타임에 information_schema에서 읽는다** — 테이블이 늘어도
    스크립트를 고치지 않아도 되고, 목록을 손으로 적다 빠뜨리는 사고를 막는다.
  · 유니크 충돌 시 **정본(survivor) 행을 남기고 dup 행을 버린다**. 버린 건수를 반드시 보고한다.
  · dup 삭제는 마지막에 한다 — 참조를 하나라도 놓쳤으면 FK가 막아 큰 소리로 실패한다.
  · 기본은 dry-run. `--apply`로만 실제 반영하고 `--limit N`으로 소수 시험이 가능하다.

실행(컨테이너에 scripts/ 미포함):
  docker cp .../merge_duplicate_employees.py infra-core-1:/tmp/
  docker exec -w /app -e PYTHONPATH=/app/src infra-core-1 python /tmp/merge_duplicate_employees.py
  ... --apply --limit 1     # 1명 시험
  ... --apply               # 전량
"""
import asyncio
import re
import sys

from sqlalchemy import select, text

from erp.config import get_settings
from erp.db import session_factory
from erp.hr.models import Employee
from erp.integrations.esign_emp_map import _fetch_directory

_SUFFIX = re.compile(r"_[a-z0-9]{4}$")

# 참조 컬럼 — FK로 걸린 것 + FK 없이 employees.id를 담는 것(common.users.employee_id 등)
_REF_SQL = text("""
SELECT tc.table_schema AS sch, tc.table_name AS tbl, kcu.column_name AS col
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name
JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND ccu.table_schema = 'hr' AND ccu.table_name = 'employees'
UNION
SELECT table_schema, table_name, column_name
FROM information_schema.columns
WHERE column_name LIKE '%employee_id%'
  AND data_type IN ('bigint', 'integer')   -- 이름만 닮은 문자열 컬럼 오탐 차단
  AND table_schema NOT IN ('pg_catalog', 'information_schema')
  AND NOT (table_schema = 'hr' AND table_name = 'employees')
""")

# 어떤 테이블을 참조하는 자식 FK — 충돌 행을 지우려면 자식부터 지워야 한다
_CHILD_SQL = text("""
SELECT tc.table_schema AS sch, tc.table_name AS tbl, kcu.column_name AS col,
       ccu.column_name AS parent_col
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name
JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND ccu.table_schema = :sch AND ccu.table_name = :tbl
""")

# 유니크 — **인덱스 기준**으로 읽는다.
# information_schema.table_constraints는 CREATE UNIQUE INDEX로 만든 것을 담지 않아서
# `hr.uq_mc_active`(medical_contracts 부분 유니크: employee_id WHERE status='ACTIVE')를 놓쳤다.
# 놓치면 재지정 UPDATE가 인덱스 위반으로 죽는다(전량 롤백이라 안전하긴 하나 병합이 멈춘다).
# 부분 인덱스(indpred)는 조건을 알 수 없으므로 **보수적으로 전체 유니크로 취급**한다 —
# 실제로는 충돌하지 않을 행까지 폐기 후보가 되므로, 폐기 건수를 반드시 보고해 눈으로 본다.
_UNIQ_SQL = text("""
SELECT n.nspname AS sch, c.relname AS tbl, i.indexrelid::regclass::text AS name,
       array_agg(a.attname ORDER BY a.attnum) AS cols,
       i.indpred IS NOT NULL AS partial
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
WHERE i.indisunique AND NOT i.indisprimary
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
GROUP BY 1, 2, 3, 5
""")


async def _refs(s) -> list[tuple[str, str, str]]:
    return [(r.sch, r.tbl, r.col) for r in (await s.execute(_REF_SQL)).all()]


async def _uniques(s) -> dict[tuple[str, str], list[tuple[str, list[str]]]]:
    out: dict[tuple[str, str], list[tuple[str, list[str]]]] = {}
    for r in (await s.execute(_UNIQ_SQL)).all():
        out.setdefault((r.sch, r.tbl), []).append((r.name, list(r.cols)))
    return out


# 폐기 대상 행이 GL 전표를 갖고 있으면 지울 수 없다 — 원장은 불변이라 전표가 고아로 남고
# 정본 행의 전표와 **이중계상**이 된다. (실제로 첫 실행에서 payroll_records 1건을 지워
# voucher 248이 고아로 남았고 급여비가 두 번 잡혔다.)
_GL_LINKED = {("hr", "payroll_records"): "payroll:{id}"}


async def _gl_blocked(s, sch: str, tbl: str, where: str, params: dict) -> list[str]:
    """폐기 후보 중 GL 전표가 걸린 행의 source_ref 목록(있으면 폐기하지 않는다)."""
    pat = _GL_LINKED.get((sch, tbl))
    if pat is None:
        return []
    ids = (await s.execute(text(  # noqa: S608 — 식별자는 information_schema 출처
        f'SELECT t.id FROM "{sch}"."{tbl}" t WHERE {where}'), params)).scalars().all()
    if not ids:
        return []
    refs = [pat.format(id=i) for i in ids]
    return list((await s.execute(
        text("SELECT source_ref FROM finance.vouchers WHERE source_ref = ANY(:r)"),
        {"r": refs})).scalars().all())


async def _delete_cascade(s, sch: str, tbl: str, where: str, params: dict,
                          depth: int = 0) -> int:
    """조건에 걸린 행을 자식부터 지운다.

    `payroll_records`를 지우려다 `payroll_lines`의 FK에 막혔다. 자식 목록을 손으로 적으면
    다음에 테이블이 늘 때 또 막히므로 information_schema에서 매번 읽어 재귀로 처리한다.
    """
    if depth > 4:
        raise RuntimeError(f"자식 FK 재귀가 너무 깊습니다: {sch}.{tbl}")
    for c in (await s.execute(_CHILD_SQL, {"sch": sch, "tbl": tbl})).all():
        if (c.sch, c.tbl) == (sch, tbl):      # 자기참조는 건너뜀(무한재귀 방지)
            continue
        sub = (f'"{c.col}" IN (SELECT "{c.parent_col}" FROM "{sch}"."{tbl}" t '  # noqa: S608
               f'WHERE {where})')
        await _delete_cascade(s, c.sch, c.tbl, sub, params, depth + 1)
    r = await s.execute(text(  # noqa: S608 — 식별자는 information_schema 출처
        f'DELETE FROM "{sch}"."{tbl}" t WHERE {where}'), params)
    return r.rowcount or 0


async def build_pairs(s) -> tuple[list[tuple[Employee, Employee]], list[dict]]:
    """디렉터리 기준 (survivor, dup) 쌍. 이름 불일치는 병합하지 않고 별도 보고."""
    cfg = get_settings()
    key = cfg.his_pii_read_key or cfg.his_webhook_key
    if not key:
        raise SystemExit("연동키 미설정(HIS_PII_READ_KEY/HIS_WEBHOOK_KEY)")
    directory = await _fetch_directory(key)

    emps = (await s.scalars(select(Employee))).all()
    by_no = {str(e.employee_no): e for e in emps if e.employee_no}
    by_ext = {str(e.external_user_id): e for e in emps if e.external_user_id}

    pairs, mismatched = [], []
    for row in directory:
        cu = row.get("clinicUserId") or row.get("clinic_user_id")
        en = row.get("employeeNumber") or row.get("employee_number")
        if not cu or not en:
            continue
        survivor, dup = by_no.get(str(en)), by_ext.get(str(cu))
        if survivor is None or dup is None or survivor.id == dup.id:
            continue
        base = _SUFFIX.sub("", dup.name or "")
        if base != (survivor.name or ""):
            mismatched.append({"employee_no": survivor.employee_no, "survivor": survivor.name,
                               "dup_no": dup.employee_no, "dup": dup.name})
            continue
        pairs.append((survivor, dup))
    return pairs, mismatched


async def merge_one(s, survivor: Employee, dup: Employee, refs, uniques,
                    *, apply: bool, stats: dict | None = None) -> dict:
    """dup의 종속 데이터를 survivor로 이관. 유니크 충돌 행은 정본 우선으로 버린다.

    stats에 테이블별 이관/폐기 건수를 누적한다 — 총계만 보면 무엇이 버려지는지 알 수 없다
    (폐기 건수에는 자식 행까지 포함되므로 부모 기준으로도 따로 세야 한다).
    """
    moved, dropped = 0, 0
    for sch, tbl, col in refs:
        n = await s.scalar(text(  # noqa: S608 — 식별자는 information_schema 출처
            f'SELECT count(*) FROM "{sch}"."{tbl}" WHERE "{col}" = :d'), {"d": dup.id})
        if not n:
            continue
        for _name, cols in uniques.get((sch, tbl), []):
            if col not in cols:
                continue
            others = [c for c in cols if c != col]
            if not others:      # employee_id 단독 유니크 — survivor 행이 있으면 dup 행은 버린다
                cond = f'EXISTS (SELECT 1 FROM "{sch}"."{tbl}" k WHERE k."{col}" = :s)'
            else:
                keys = " AND ".join(f'k."{c}" IS NOT DISTINCT FROM t."{c}"' for c in others)
                cond = (f'EXISTS (SELECT 1 FROM "{sch}"."{tbl}" k '
                        f'WHERE k."{col}" = :s AND {keys})')
            where = f't."{col}" = :d AND {cond}'
            blocked = await _gl_blocked(s, sch, tbl, where, {"d": dup.id, "s": survivor.id})
            if blocked:
                # 지우면 전표가 고아가 되고 정본 전표와 이중계상 → 병합 자체를 중단한다.
                raise RuntimeError(
                    f"{sch}.{tbl}: GL 전표가 걸린 행은 폐기할 수 없습니다 {blocked} — "
                    f"해당 전표를 먼저 역분개한 뒤 다시 실행하세요 "
                    f"(dup={dup.employee_no}, survivor={survivor.employee_no})")
            n_parent = await s.scalar(text(  # noqa: S608 — 폐기되는 '부모' 행만 별도 집계
                f'SELECT count(*) FROM "{sch}"."{tbl}" t WHERE {where}'),
                {"d": dup.id, "s": survivor.id})
            n_all = await _delete_cascade(s, sch, tbl, where, {"d": dup.id, "s": survivor.id})
            dropped += n_all
            if stats is not None and n_parent:
                e = stats.setdefault(f"{sch}.{tbl}", {"moved": 0, "dropped": 0,
                                                      "dropped_cascade": 0})
                e["dropped"] += n_parent
                e["dropped_cascade"] += n_all - n_parent
        r = await s.execute(text(  # noqa: S608
            f'UPDATE "{sch}"."{tbl}" SET "{col}" = :s WHERE "{col}" = :d'),
            {"s": survivor.id, "d": dup.id})
        moved += r.rowcount or 0
        if stats is not None and r.rowcount:
            e = stats.setdefault(f"{sch}.{tbl}", {"moved": 0, "dropped": 0,
                                                  "dropped_cascade": 0})
            e["moved"] += r.rowcount

    ext = dup.external_user_id
    if apply:
        dup.external_user_id = None      # 유니크/인덱스 충돌 회피 — 먼저 떼고 옮긴다
        await s.flush()
        survivor.external_user_id = ext
        await s.flush()
        await s.delete(dup)              # 참조를 놓쳤으면 FK가 막는다(의도된 안전망)
        await s.flush()
    return {"survivor": survivor.employee_no, "dup": dup.employee_no,
            "external_user_id": ext, "moved": moved, "dropped_conflicts": dropped}


async def main() -> None:
    apply = "--apply" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    async with session_factory()() as s:
        refs, uniques = await _refs(s), await _uniques(s)
        pairs, mismatched = await build_pairs(s)
        print(f"참조 컬럼 {len(refs)}개 · 병합 대상 {len(pairs)}쌍 · 이름 불일치 {len(mismatched)}건")
        if mismatched:
            print("\n⚠ 이름 불일치 — 병합하지 않음(수동 확인 필요):")
            for m in mismatched[:10]:
                print(f"  {m['employee_no']} {m['survivor']}  ↮  {m['dup_no']} {m['dup']}")

        todo = pairs[:limit] if limit else pairs
        print(f"\n{'적용' if apply else 'DRY RUN'} — {len(todo)}쌍")
        tot_moved = tot_dropped = 0
        stats: dict = {}
        verbose = "--verbose" in sys.argv
        for survivor, dup in todo:
            out = await merge_one(s, survivor, dup, refs, uniques, apply=apply, stats=stats)
            tot_moved += out["moved"]
            tot_dropped += out["dropped_conflicts"]
            if verbose:
                print(f"  {out['dup']} → {out['survivor']} "
                      f"(이관 {out['moved']}, 충돌폐기 {out['dropped_conflicts']})")

        print("\n테이블별 (이관 / 폐기=정본과 겹쳐 버린 dup 행 / 그에 딸린 자식 행):")
        for t, e in sorted(stats.items()):
            print(f"  {t:<32} 이관 {e['moved']:>4} · 폐기 {e['dropped']:>4}"
                  f" (+자식 {e['dropped_cascade']})")
        if apply:
            await s.commit()
        else:
            await s.rollback()
        print(f"\n합계 이관 {tot_moved} · 충돌폐기 {tot_dropped} · "
              f"{'커밋됨' if apply else '롤백됨(dry-run)'}")


if __name__ == "__main__":
    asyncio.run(main())
