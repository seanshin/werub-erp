"""운영 정합성 점검 — 도메인 경계를 가로지르는 불변식을 상시 검사한다.

**왜 있는가**: 이번까지 정합 문제는 사람이 의심할 때만 손으로 SQL을 돌려 찾았다(외화 GL 통화
혼입·미러만 있고 GL 없는 채권·병합이 남긴 고아 전표·환자 마스터 불일치). 전부 **조용히
틀린 채로 있다가 우연히 드러난** 것들이다. 실 운영에서는 우연에 맡길 수 없으므로 같은 검사를
기능으로 고정한다.

**설계 규율 — 영구 발화 금지**:
  각 검사는 *정상 상태에서 반드시 GREEN이 되는 불변식*이어야 한다. 활동량 기반 고정 임계
  (예: "24시간 무수신이면 경고")는 저빈도 이벤트에서 영구 적색이 되어 진짜 신호를 가린다
  (인바운드 무수신 경보를 그 이유로 폐기한 적이 있다). 활동 의존 검사는 **조건부 불변식**으로
  만든다 — 예: "발생주의가 켜져 있는데 수납 유입이 없다"는 게이트가 꺼져 있으면 검사 자체를
  건너뛴다(SKIP).

각 검사는 `{key, title, domain, status, summary, detail, action}`을 돌려준다.
status: OK | WARN | FAIL | SKIP.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import String, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

_CHECKS: list = []


def check(key: str, title: str, domain: str):
    """검사 등록 — 순서는 등록 순."""
    def deco(fn):
        _CHECKS.append((key, title, domain, fn))
        return fn
    return deco


def _r(status: str, summary: str, detail: Any = None, action: str | None = None) -> dict:
    return {"status": status, "summary": summary, "detail": detail, "action": action}


# ── 원장 불변식 ────────────────────────────────────────────────────────────

@check("ledger.balanced_vouchers", "전표 차대 일치", "finance")
async def _balanced(s: AsyncSession) -> dict:
    n = await s.scalar(text("""
        SELECT count(*) FROM (
          SELECT v.id FROM finance.vouchers v
          JOIN finance.voucher_lines vl ON vl.voucher_id = v.id
          GROUP BY v.id
          HAVING sum(CASE vl.side WHEN 'DEBIT' THEN vl.amount ELSE -vl.amount END) <> 0) t"""))
    return (_r("OK", "차대가 맞지 않는 전표 없음") if not n else
            _r("FAIL", f"차대 불일치 전표 {n}건", {"count": n},
               "해당 전표를 역분개하고 원인 경로를 확인하세요(원장 파손)"))


@check("ledger.accounting_equation", "회계 항등식(자산=부채+자본+손익)", "finance")
async def _equation(s: AsyncSession) -> dict:
    rows = dict((await s.execute(text("""
        SELECT a.kind, round(sum(CASE vl.side WHEN 'DEBIT' THEN vl.amount ELSE -vl.amount END))
        FROM finance.voucher_lines vl
        JOIN finance.vouchers v ON v.id = vl.voucher_id
        JOIN finance.accounts a ON a.code = vl.account_code
        WHERE v.status IN ('POSTED','REVERSED') GROUP BY a.kind"""))).all())
    g = lambda k: int(rows.get(k) or 0)  # noqa: E731
    lhs = g("ASSET")
    rhs = -g("LIABILITY") - g("CAPITAL") + (-g("REVENUE") - g("EXPENSE"))
    diff = lhs - rhs
    return (_r("OK", f"항등식 성립 (자산 {lhs:,})") if diff == 0 else
            _r("FAIL", f"항등식 불일치 {diff:,}원", {"asset": lhs, "rhs": rhs},
               "전표 차대·계정 kind 매핑을 확인하세요"))


@check("ledger.unknown_accounts", "미등록 계정코드 전기", "finance")
async def _unknown_acc(s: AsyncSession) -> dict:
    rows = (await s.execute(text("""
        SELECT DISTINCT vl.account_code FROM finance.voucher_lines vl
        LEFT JOIN finance.accounts a ON a.code = vl.account_code
        WHERE a.code IS NULL LIMIT 20"""))).scalars().all()
    return (_r("OK", "계정 마스터에 없는 코드 없음") if not rows else
            _r("FAIL", f"미등록 코드 {len(rows)}종", {"codes": list(rows)},
               "계정 마스터에 등록하거나 전기 경로를 수정하세요"))


@check("ledger.orphan_payroll_vouchers", "고아 급여 전표", "finance")
async def _orphan_payroll(s: AsyncSession) -> dict:
    """급여기록이 지워졌는데 전표만 남은 경우 — 직원 병합이 실제로 만든 적 있는 결함.

    ⚠ 원시 SQL 대신 Core로 쓴다 — `::text` 같은 PG 전용 캐스트는 sqlite 테스트 레인에서 깨지고,
    검사가 죽으면 그 자체가 FAIL로 보고돼 **진짜 결함과 구분되지 않는다**(검사의 자기 오류가
    가장 나쁜 형태의 오탐이다).
    """
    from erp.finance.models import Voucher
    from erp.hr.models import PayrollRecord
    ref = func.concat("payroll:", cast(PayrollRecord.id, String))
    n = await s.scalar(
        select(func.count()).select_from(Voucher).where(
            Voucher.source_module == "hr.payroll", Voucher.status != "REVERSED",
            Voucher.source_ref.notlike("%/reversal-%"),
            ~select(PayrollRecord.id).where(ref == Voucher.source_ref).exists()))
    return (_r("OK", "고아 급여 전표 없음") if not n else
            _r("FAIL", f"고아 급여 전표 {n}건", {"count": n},
               "정본 전표와 이중계상 상태입니다 — 해당 전표를 역분개하세요"))


# ── 외화 ──────────────────────────────────────────────────────────────────

@check("forex.gl_currency_mix", "외화 GL 통화 혼입", "finance")
async def _fx_mix(s: AsyncSession) -> dict:
    from erp.finance.models import CorporateAccount
    rows = (await s.execute(select(
        CorporateAccount.gl_account_code, CorporateAccount.currency).distinct())).all()
    by_gl: dict[str, set] = {}
    for gl, cur in rows:
        by_gl.setdefault(gl, set()).add(cur)
    mixed = {gl: sorted(c) for gl, c in by_gl.items() if len(c) > 1}
    return (_r("OK", "통화별 GL 분리 정상") if not mixed else
            _r("FAIL", f"통화가 섞인 GL {len(mixed)}개",
               [{"gl": gl, "currencies": c} for gl, c in sorted(mixed.items())],
               "통화마다 전용 GL 계정을 지정하세요 — 장부가가 섞여 기말환산이 부정확해집니다"))


# ── 채권·수익 인식 ─────────────────────────────────────────────────────────

#: 채권 미러 출처 → 대응 GL 계정. 미러는 여러 도메인이 공유하지만 GL 계정은 출처마다 다르다.
#
# ⚠ 처음엔 **전체 미러를 GL 1130 하나와** 비교했다. 지금은 데모 채권뿐이라 맞아떨어졌지만,
#   발생주의를 켜는 순간 환자채권(source_module='patient' → GL **1131**)이 미러에만 쌓여
#   **영구 WARN**이 됐을 것이다 — 이 서비스가 금지한 바로 그 안티패턴이다.
_AR_MIRROR_GL = {"patient": "1131"}
_AR_MIRROR_GL_DEFAULT = "1130"


@check("ar.mirror_vs_gl", "채권 미러 ↔ GL 대사", "finance")
async def _ar_mirror(s: AsyncSession) -> dict:
    """출처별로 대응 GL과 대조한다 — 미러만 있고 GL 전기가 없는 채권을 잡는다."""
    from sqlalchemy import case

    from erp.finance.models import Receivable, Voucher, VoucherLine

    rows = (await s.execute(
        select(Receivable.source_module,
               func.coalesce(func.sum(Receivable.amount
                                      - func.coalesce(Receivable.collected_amount, 0)), 0))
        .where(Receivable.status != "WRITTEN_OFF")
        .group_by(Receivable.source_module))).all()
    by_acct: dict[str, float] = {}
    for src, amt in rows:
        acct = _AR_MIRROR_GL.get(src or "", _AR_MIRROR_GL_DEFAULT)
        by_acct[acct] = by_acct.get(acct, 0.0) + float(amt or 0)

    bad, detail = [], []
    for acct, mirror in sorted(by_acct.items()):
        gl = round(float(await s.scalar(
            select(func.coalesce(func.sum(case(
                (VoucherLine.side == "DEBIT", VoucherLine.amount), else_=-VoucherLine.amount)), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code == acct,
                   Voucher.status.in_(("POSTED", "REVERSED")))) or 0))
        m = round(mirror)
        detail.append({"account": acct, "mirror": m, "gl": gl, "diff": m - gl})
        if m != gl:
            bad.append(acct)
    if not detail:
        return _r("OK", "채권 미러 없음")
    if not bad:
        return _r("OK", " · ".join(f"{d['account']} {d['mirror']:,}원" for d in detail))
    return _r("WARN", f"미러≠GL {len(bad)}개 계정({', '.join(bad)})", detail,
              "미러만 있고 GL 전기가 없는 채권이 있는지 확인하세요")


@check("ar.patient_master_match", "진료 라인 ↔ 환자 마스터 정합", "patient")
async def _patient_master(s: AsyncSession) -> dict:
    """수납 조회(`_collected`)가 chart_no로 조인하므로, 마스터에 없으면 수납을 못 찾는다."""
    total = int(await s.scalar(text(
        "SELECT count(DISTINCT chart_no) FROM patient.billing_lines WHERE NOT cancelled")) or 0)
    if not total:
        return _r("SKIP", "진료 라인 없음")
    matched = int(await s.scalar(text("""
        SELECT count(DISTINCT bl.chart_no) FROM patient.billing_lines bl
        WHERE NOT bl.cancelled
          AND EXISTS (SELECT 1 FROM patient.patients p WHERE p.chart_no = bl.chart_no)""")) or 0)
    missing = total - matched
    return (_r("OK", f"환자 {total}명 전원 마스터 보유") if not missing else
            _r("FAIL", f"마스터에 없는 환자 {missing}/{total}명",
               {"total": total, "missing": missing},
               "환자 마스터 동기화가 필요합니다 — 수납 조회가 조인 실패로 0원이 되어 "
               "발생주의 전환 시 미수가 과대계상됩니다"))


@check("ar.accrual_prerequisites", "발생주의 전제(수납 유입)", "patient")
async def _accrual_prereq(s: AsyncSession) -> dict:
    """⚠ 조건부 불변식 — 게이트가 꺼져 있으면 SKIP한다(활동 기반 고정 임계 회피).

    ⚠ 수납 경로를 **HIS 이벤트로 한정하지 않는다.** ERP 자체 수납(`/patients/closing` → Payment)만
    쓰는 설치도 정상이며, 그것까지 막으면 HIS 없는 기관에서 발생주의를 아예 못 쓴다.
    검사가 방어할 수 있는 명제는 **"수납 기록이 어디에도 없는데 발생만 켜면 1131이 단조 증가한다"**
    이므로, 두 경로 중 **하나라도** 있으면 통과시킨다(적용 범위까지는 이 검사가 보증하지 않는다).
    """
    from erp.config import get_settings
    if not get_settings().patient_accrual_enabled:
        return _r("SKIP", "발생주의 OFF — 점검 대상 아님")
    events = int(await s.scalar(text("""
        SELECT count(*) FROM integration.his_inbound_events
        WHERE event_type IN ('billing.settled','billing.interim_paid',
                             'billing.discharge_settlement')""")) or 0)
    local = int(await s.scalar(text(
        "SELECT count(*) FROM patient.payments WHERE status = 'PAID'")) or 0)
    if events or local:
        return _r("OK", f"수납 유입 있음 (HIS 이벤트 {events} · 자체 수납 {local})",
                  {"paid_events": events, "local_payments": local})
    return _r("FAIL", "발생주의가 켜져 있는데 수납 기록이 어디에도 없음",
              {"paid_events": 0, "local_payments": 0},
              "DR 1131만 쌓이고 상계가 오지 않습니다 — 수납 경로(HIS 이벤트 또는 원무 수납) "
              "개통 전까지 게이트를 끄세요")


# ── 재고·조달 ──────────────────────────────────────────────────────────────

@check("scm.stock_within_received", "재고 잔량 > 입고량", "scm")
async def _stock_sane(s: AsyncSession) -> dict:
    """잔량이 입고량을 넘을 수 없다 — 반품·정정이 과대 반제하면 여기서 드러난다.

    ⚠ **음수 잔량은 검사하지 않는다.** DB CHECK(`lots_remaining_qty_check`·`ck_consign_nonneg`)이
    이미 막고 있어 이 검사로는 **영원히 걸리지 않는다** — DB가 보장하는 것을 또 재는 검사는
    죽은 코드이고, 검사 목록을 부풀려 진짜 신호를 묻는다.
    """
    from erp.scm.models import ConsignmentStock, Lot
    over_lot = await s.scalar(select(func.count()).select_from(Lot)
                              .where(Lot.remaining_qty > Lot.received_qty))
    over_con = await s.scalar(select(func.count()).select_from(ConsignmentStock)
                              .where(ConsignmentStock.qty_on_hand > ConsignmentStock.placed_qty))
    bad = (over_lot or 0) + (over_con or 0)
    return (_r("OK", "로트·위탁 잔량이 입고량 이내") if not bad else
            _r("FAIL", f"로트 초과 {over_lot}건 · 위탁 초과 {over_con}건",
               {"lot_over_received": over_lot, "consign_over_placed": over_con},
               "반품·정정이 과대 반제했는지 확인하세요 — 물리적으로 불가능한 잔량입니다"))


# ── 보험청구 ───────────────────────────────────────────────────────────────

@check("claims.submission_vs_statement", "청구 제출액 ↔ 명세 합계", "claims")
async def _claim_recon(s: AsyncSession) -> dict:
    """진료월별 제출액과 명세 합계는 같아야 한다 — 어긋나면 청구 누락·중복 신호다.

    ⚠ 제출건이 아직 없는 월(통보서 미도착 등)은 **불일치가 아니다** — 제출이 있는 월만 본다.
    이 구분이 없으면 최근 월이 상시 적색이 되어 진짜 누락을 가린다.
    """
    rows = (await s.execute(text("""
        SELECT b.care_month AS cm, sum(st.claimed_amt) AS stmt,
               (SELECT sum(claimed_amt) FROM claims.claim_submissions x
                 WHERE x.care_month = b.care_month) AS sub
        FROM claims.claim_batches b
        JOIN claims.claim_statements st ON st.batch_id = b.id
        GROUP BY b.care_month"""))).all()
    bad = [{"care_month": r.cm, "statement": int(r.stmt or 0), "submission": int(r.sub or 0)}
           for r in rows if r.sub is not None and int(r.sub or 0) != int(r.stmt or 0)]
    return (_r("OK", f"{len(rows)}개 진료월 제출=명세 일치") if not bad else
            _r("WARN", f"제출≠명세 {len(bad)}개월", bad[:10],
               "청구 누락·중복 또는 시드 부정합입니다 — /claims 대사 화면에서 확인하세요"))


# ── 인사 신원 ──────────────────────────────────────────────────────────────

@check("hr.identity_uniqueness", "직원 신원 유일성", "hr")
async def _hr_identity(s: AsyncSession) -> dict:
    dup_no = int(await s.scalar(text("""
        SELECT count(*) FROM (SELECT employee_no FROM hr.employees
        WHERE employee_no IS NOT NULL GROUP BY 1 HAVING count(*) > 1) t""")) or 0)
    dup_ext = int(await s.scalar(text("""
        SELECT count(*) FROM (SELECT external_user_id FROM hr.employees
        WHERE external_user_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1) t""")) or 0)
    orphan_user = int(await s.scalar(text("""
        SELECT count(*) FROM common.users u WHERE u.employee_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM hr.employees e WHERE e.id = u.employee_id)""")) or 0)
    bad = dup_no + dup_ext + orphan_user
    return (_r("OK", "중복 사번·중복 연동ID·고아 참조 없음") if not bad else
            _r("FAIL", f"중복 사번 {dup_no} · 중복 연동ID {dup_ext} · 고아 user {orphan_user}",
               {"dup_employee_no": dup_no, "dup_external_user_id": dup_ext,
                "orphan_users": orphan_user},
               "동일인 중복 등록일 수 있습니다 — merge_duplicate_employees.py 참조"))


# ── 첨부 ───────────────────────────────────────────────────────────────────

@check("attachments.body_present", "첨부 본문 존재", "common")
async def _attachment_bodies(s: AsyncSession) -> dict:
    """메타는 있는데 파일이 없는 첨부 — 배포 후 CLI(`erp.attachment_check`)로만 보던 것을 상시 검사로(2026-10-10).

    v1.288 전 worker-batch에 첨부 볼륨이 없어 회수한 서명 완료본이 컨테이너 안에만 쓰였을 수 있다. 정상이면
    항상 0이라 영구 발화하지 않는다. ATTACHMENT_DIR 미설정(로컬·테스트)이면 SKIP.
    """
    from erp.attachment_check import find_missing
    from erp.config import get_settings
    if not get_settings().attachment_dir:
        return _r("SKIP", "ATTACHMENT_DIR 미설정 — 첨부 저장소가 없는 환경")
    # 파일명은 싣지 않는다 — 직원·계약 첨부명에 실명이 들어간다(화면·일일 점검 알림으로 퍼짐)
    missing = [{"id": m["id"], "entity_type": m["entity_type"], "entity_id": m["entity_id"]}
               for m in await find_missing(s) if "id" in m]
    return (_r("OK", "본문 없는 첨부 없음") if not missing else
            _r("FAIL", f"본문 없는 첨부 {len(missing)}건", missing[:20],
               "서명 완료본이면 HIS에 완료본 재발급을 요청하고, 그 밖의 첨부는 원본을 다시 올리세요"
               "(배포 전 컨테이너 안에 남은 파일은 v1288 런북 §3 절차로 옮길 수 있습니다)"))


#: 발생주의 전기 시 함께 확인하는 검사 — **차단하지 않고 경고**한다.
#
# ⚠ 처음엔 하드블록으로 만들었다가 두 번 좁히고 결국 경고로 바꿨다. 막으려다 정상 상태를
# 막았기 때문이다:
#   · 환자 마스터 불일치로 차단 → `accrue_all`의 건별 savepoint 격리(1건 실패가 배치를 못 세움)
#     설계를 무력화한다. 동기화 지연으로 일시적으로 빠질 수 있다.
#   · 수납 기록 0으로 차단 → **개통 첫날은 진료만 있고 수납이 없는 것이 정상**이다.
# "오래 지속되면 문제"는 추세이지 시점 상태가 아니라서 하드블록으로 표현할 수 없다.
# 그래서 실행은 계속하되 결과에 위반을 실어 보내고 관리자에게 알린다(화면·일일 점검과 동일 소스).
ACCRUAL_WATCHED = ("ar.patient_master_match", "ar.accrual_prerequisites")


async def accrual_warnings(session: AsyncSession) -> list[dict]:
    """발생주의 전기와 함께 확인할 항목 중 이상인 것 — 비어 있으면 정상.

    호출자는 이것을 실행 결과에 실어 보내고, 배치라면 관리자 알림까지 남긴다.
    게이트가 꺼져 있으면 `ar.accrual_prerequisites`가 SKIP이라 자연히 빠진다.
    """
    out = []
    for key, title, _domain, fn in _CHECKS:
        if key not in ACCRUAL_WATCHED:
            continue
        r = await fn(session)
        if r["status"] in ("FAIL", "WARN"):
            out.append({"key": key, "title": title, **r})
    return out


async def run_all(session: AsyncSession) -> dict:
    """전체 검사 실행 — 개별 검사 실패가 전체를 죽이지 않도록 격리한다."""
    items = []
    for key, title, domain, fn in _CHECKS:
        try:
            out = await fn(session)
        except Exception as exc:  # noqa: BLE001 — 검사 자체의 오류도 결과로 보여준다
            out = _r("FAIL", f"검사 실행 실패: {str(exc)[:120]}", None,
                     "검사 코드 또는 스키마 변경을 확인하세요")
        items.append({"key": key, "title": title, "domain": domain, **out})
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    worst = ("FAIL" if counts.get("FAIL") else
             "WARN" if counts.get("WARN") else "OK")
    return {"items": items, "counts": counts, "overall": worst}
