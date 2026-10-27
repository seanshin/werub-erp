"""HIS 웹훅 인박스 소비 (마스터플랜 Wave) — his_inbound_events(NEW) → 도메인 모델 적용.

이벤트별 savepoint 격리(한 건 실패가 다른 건 영향 없음) + 멱등(인박스 source_ref UNIQUE).
도메인별 핸들러는 본 모듈 하단 HANDLERS 매핑이 단일 출처(SoT) — 신규 도메인 추가 시 그곳에 등록.
미등록 (domain, event_type)은 DEAD(error 명시)로 가시화 → 운영자 확인. 일시실패는 FAILED·선형 백오프 재시도.
"""
import hashlib
import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.kst import KST, as_kst_wallclock
from erp.common.models import Department
from erp.errors import ValidationError
from erp.finance.asset_service import AssetService
from erp.finance.models import FixedAsset
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.hr.models import AttendanceSummary, Employee, GroupwareAttendanceStaging, License
from erp.integrations.models import HisInboundEvent, HisStockMirror
from erp.integrations.patient_ref import (
    PendingRefError, record_pending, resolve_inbound_chart,
)

logger = logging.getLogger("erp.integrations.inbound")
_ASSET_LIFE_DEFAULT = 8     # 의료장비 기본 내용연수(년) — payload 미포함 시
# 회계 계정 (일마감과 동일): 현금 1110·카드미수 1132(일반미수 1130과 분리)·외래수익 4110
_ACC_CASH, _ACC_CARD_AR, _ACC_REVENUE = "1110", "1132", "4110"
_ACC_CERT_REVENUE = "4120"   # 제증명수입(진단서·소견서 등·진료수익 4110과 분리·HIS e결재 연계)
_ACC_AP_MAINT = "2110"      # 미지급금 (유지보수비)
_ACC_INS_AR = "1140"        # 보험미수금
_ACC_CLAIM_LOSS = "5910"    # 삭감손실
_vouchers = VoucherService()
_REVIEW_SUFFIX = re.compile(r"(?::(?:v|rev)(\d+))?")   # 심사 전표 ref 접미(원본·정정본·역분개)


_REF_MAX = 64            # finance.vouchers.source_ref VARCHAR(64)
_BASE_REF_MAX = 56       # 정정 체인 기준 키 — `:v{n}`·`:rev{n}` 접미 여유(최대 8자)
# 2026-10-09 시나리오 3회차: 정정 충돌·추론 정정 가드가 DEAD로 보낸 이벤트의 출구 — 재큐는 같은 가드에 다시
# 걸리므로 운영자가 대조 후 '처리 완료(사유)'로 종결한다(POST /integrations/his/inbound/{id}/resolve).
_RESOLVE_HINT = "관리자 > 연동 인박스에서 '처리 완료(사유)'로 종결"
# 삭감 감소 재심사 보류 표지 — apply-confirmed는 이 사유로 DEAD된 건에만 허용(관리 화면 버튼도 같은 기준·6회차)
RESTORE_HOLD_PREFIX = "삭감이 줄어든 HIS 재심사"
#: 외화 라인 환율 미동봉 보류 문구 — 관리 화면 '환율 지정 후 반영'의 대상 판정(구 문구 "환율(DB-2…) 확정 후"도 포함).
#: "외화(USD) 법인계좌 미설정"은 보류가 아니라 ERP 설정 문제라 제외된다('보류' 문구가 없다).
FX_HOLD_RE = re.compile(r"외화\([A-Z]{3}\) [^—]*?보류")   # lineType 빈 값·옛 "sale 부킹 보류" 문구 포함


def _effective_fx_rate(p) -> tuple[float | None, str | None]:
    """라인 환율 — HIS 동봉 `fxRate` 우선, 없으면 관리자가 화면에서 지정한 `_erpFxRate`(근거·감사 기록).

    `_erp*` 키는 웹훅 수신 때 제거되므로(HIS가 실어 보낼 수 없다) 관리 EP만 채운다. 반환=(환율, 출처 표기).
    """
    if p.get("fxRate"):
        return float(p["fxRate"]), str(p["fxRate"])
    erp = p.get("_erpFxRate") or {}
    if isinstance(erp, dict) and erp.get("rate"):
        return float(erp["rate"]), f"{erp['rate']}(ERP 지정 #{erp.get('by')})"
    return None, None


def _bounded_ref(head: str, tail: str = "", *, limit: int = _REF_MAX) -> str:
    """외부 식별자로 조립한 멱등키를 길이 상한 안에 **결정적으로** 맞춘다.

    2026-10-09 시나리오 S-A: 실 HIS billing id는 UUID, 이벤트 ref는 `billing:refund:<uuid>:<ms>`라
    `adjust:{bid}:{kind}:{ref}`가 102~116자 → PG VARCHAR(64) StringDataRightTruncation으로 환불·취소가
    전부 실패 후 DEAD였다(sqlite는 길이를 강제하지 않아 테스트가 통과). 상한 이내면 **원문 그대로**(기존
    전기 키와 멱등 호환), 넘으면 원문 전체의 sha256 접두로 치환한다 — 같은 이벤트 재수신은 같은 키,
    다른 이벤트(같은 청구의 부분환불 2회)는 다른 키. head가 길어 못 담으면 head 첫 토큰+해시만 남긴다."""
    full = f"{head}:{tail}" if tail else head
    if len(full) <= limit:
        return full
    h = hashlib.sha256(full.encode()).hexdigest()
    keep = f"{head}:#{h[:16]}"
    if len(keep) <= limit:
        return keep
    return f"{head.split(':', 1)[0]}:#{h[:32]}"


async def _assert_no_conflicting_voucher(session: AsyncSession, *, source_module: str,
                                         source_ref: str, expected_debit: int) -> bool:
    """id-only 멱등키(settle:{date}·claim:review:{cid})로 이미 전기된 전표가 있는데 **금액이 다르면**
    정정 재수신을 오류로 가시화(적대검토 MED §1/§2).

    VoucherService.create는 기존 ref면 조용히 반환 → 정정본(금액 상이)이 무음 유실되던 갭. 여기서
    기존 차변 합과 비교해 불일치면 ValidationError(→ 이벤트 DEAD·경보). 정정 자동 역분개는 별도
    정책 필요이므로 여기선 '무음 유실 차단'까지. 반환 True=기존 일치(멱등 no-op)·False=신규.
    """
    from erp.finance.models import Voucher, VoucherLine
    existing = await session.scalar(select(Voucher).where(
        Voucher.source_module == source_module, Voucher.source_ref == source_ref))
    if existing is None:
        return False
    ex_debit = await session.scalar(select(func.coalesce(func.sum(VoucherLine.amount), 0))
                                    .where(VoucherLine.voucher_id == existing.id,
                                           VoucherLine.side == "DEBIT"))
    if int(ex_debit or 0) != int(expected_debit):
        raise ValidationError(
            # 청구 심사 전표(claim:review)는 수동 역분개가 막혀 있다(finance) — 정정은 HIS 재발행으로만(4회차)
            "정정 재수신 — 기존 전표와 금액 불일치(무음 유실 차단) — 원장 금액이 맞으면 회계팀이 "
            f"{_RESOLVE_HINT}하고, 금액을 바꿔야 하면 HIS 담당자에게 revision을 실어 재발행을 요청하세요",
            {"source_ref": source_ref, "existing_debit": int(ex_debit or 0),
             "incoming_debit": int(expected_debit)})
    return True


def _revision(p: dict) -> int:
    """HIS 정정 버전 — `revision`(HIS 계약) 우선, `correctionVersion`(구 ERP) 폴백. `correctionOf`가
    있으면 정정본이므로 최소 v2로 간주(revision 누락 방어). 없으면 1(최초)."""
    rev = p.get("revision") or p.get("correctionVersion")
    if rev is not None:
        return max(int(rev), 1)
    return 2 if p.get("correctionOf") else 1


async def _post_with_correction(session: AsyncSession, *, source_module: str, base_ref: str,
                                version: int, lines: list, voucher_on: date, memo: str,
                                expected_debit: int) -> None:
    """정정 지원 전표 전기(#2 correctionOf·B안). HIS가 correctionOf+revision을 동봉하면:

    - version≤1/없음: `base_ref`로 단순 전기 + 무음유실 가드(비정정 재전송의 금액 상이는 DEAD).
    - version≥2: **직전 유효 버전을 역분개**(차/대 스왑·ref `{base_ref}:rev{v}`) 후 신버전 전기
      (ref `{base_ref}:v{v}`). 원본·역분개·정정본이 모두 원장에 남아 감사추적 보존(append-only 정합).
      멱등: 같은 version 재수신은 VoucherService (module,ref) UNIQUE로 이중전기 차단.
    """
    from erp.finance.models import Voucher, VoucherLine
    if not version or version <= 1:
        await _assert_no_conflicting_voucher(session, source_module=source_module,
                                             source_ref=base_ref, expected_debit=expected_debit)
        await _vouchers.create(session, source_module=source_module, source_ref=base_ref,
                               voucher_on=voucher_on, lines=lines, memo=memo)
        return
    new_ref = f"{base_ref}:v{version}"
    # 같은 버전이 이미 있으면 **금액이 같을 때만** 멱등 no-op — 종전엔 무조건 반환해, 다른 금액의 같은 버전
    # (ERP 추론 v2 뒤 HIS 명시 revision=2, 동시 추론 충돌)이 무음 유실되고 decided_amt만 바뀌었다(독립검토 MED).
    # 금액 0 정정본은 `:v{n}` 없이 `:rev{n}`만 남으므로 그것도 '이미 처리된 버전'으로 본다.
    if await session.scalar(select(Voucher.id).where(
            Voucher.source_module == source_module, Voucher.source_ref == new_ref)):
        await _assert_no_conflicting_voucher(session, source_module=source_module,
                                             source_ref=new_ref, expected_debit=expected_debit)
        return
    if await session.scalar(select(Voucher.id).where(
            Voucher.source_module == source_module,
            Voucher.source_ref == f"{base_ref}:rev{version}")):
        if expected_debit:   # v{n}이 금액 0 정정으로 처리됐는데 같은 버전이 삭감액을 들고 옴
            raise ValidationError(
                "정정 재수신 — 같은 버전이 기존 처리(삭감 0)와 금액 불일치(무음 유실 차단) — 회계팀이 원장과 "
                f"대조해 {_RESOLVE_HINT}하거나, HIS에 다음 revision을 실어 재발행을 요청하세요",
                {"source_ref": new_ref, "existing_debit": 0, "incoming_debit": int(expected_debit)})
        return
    # 직전 **유효** 전표 찾기 — 바로 앞 버전이 '금액 0 정정'이라 전표가 없을 수 있다(v2 전액승인 후 v3).
    # 종전엔 `:v{version-1}`만 찾아, 없으면 원본을 역분개하지 않고 이중 계상했다(2026-10-09 검토 B).
    # 가장 최근 전기 버전 k를 찾고, 그 뒤 버전이 이미 그것을 역분개(`:rev{j}`, j>k)했으면 대상 없음.
    prev = None
    for k in range(version - 1, 0, -1):
        ref_k = base_ref if k == 1 else f"{base_ref}:v{k}"
        cand = await session.scalar(select(Voucher).where(
            Voucher.source_module == source_module, Voucher.source_ref == ref_k))
        if cand is None:
            continue
        reversed_later = await session.scalar(select(Voucher.id).where(
            Voucher.source_module == source_module,
            Voucher.source_ref.in_([f"{base_ref}:rev{j}" for j in range(k + 1, version)])))
        # 수동 역분개(REVERSED)된 전표는 이미 상쇄 — 다시 역분개하면 이중 복원(독립검토 LOW)
        prev = None if (reversed_later or cand.status == "REVERSED") else cand
        break
    if prev is not None:   # 직전 유효분 역분개(차/대 스왑)
        prev_lines = (await session.scalars(select(VoucherLine)
                                            .where(VoucherLine.voucher_id == prev.id))).all()
        rev = [VoucherLineDTO(pl.account_code,
                              "CREDIT" if pl.side == "DEBIT" else "DEBIT", int(pl.amount))
               for pl in prev_lines]
        await _vouchers.create(session, source_module=source_module,
                               source_ref=f"{base_ref}:rev{version}", voucher_on=voucher_on,
                               lines=rev, memo=f"[정정 역분개 v{version}] {memo}")
    if lines:   # 금액 0 정정(전액 승인 등)은 역분개만 — 신버전 전표 없음
        await _vouchers.create(session, source_module=source_module, source_ref=new_ref,
                               voucher_on=voucher_on, lines=lines, memo=f"[정정 v{version}] {memo}")


async def _review_ledger_state(session: AsyncSession, base_ref: str) -> tuple[int, int, bool]:
    """심사 전표 체인(`base_ref`·`:vN`·`:revN`)의 (최고 버전, 삭감손실 5910 순액, 전표 존재) — 정정 추론용.

    5910 순액은 원본·정정본·역분개를 모두 합한 **현재 유효 삭감액**이다(역분개가 원본을 상쇄)."""
    from erp.finance.models import Voucher, VoucherLine
    rows = (await session.execute(select(Voucher.id, Voucher.source_ref, Voucher.status).where(
        Voucher.source_module == "his.claim",
        (Voucher.source_ref == base_ref)
        | Voucher.source_ref.startswith(f"{base_ref}:", autoescape=True)))).all()
    max_v, ids, seen = 0, [], False
    for vid, ref, st in rows:
        suffix = ref[len(base_ref):]
        m = _REVIEW_SUFFIX.fullmatch(suffix)
        if m is None:
            continue
        seen = True
        max_v = max(max_v, int(m.group(1) or 1))   # 버전 번호는 수동 역분개된 것도 소진(재사용 금지)
        # 수동 역분개(VoucherService.reverse → `<ref>/reversal-<id>`·원본 REVERSED)는 원본+역분개 합 0 —
        # 역분개 ref는 접미 규칙 밖이라 원본만 세면 이미 취소된 삭감이 유효로 남았다(독립검토 LOW).
        if st != "REVERSED":
            ids.append(vid)
    if not ids:
        return max_v, 0, seen
    net = await session.scalar(select(func.coalesce(func.sum(case(
        (VoucherLine.side == "DEBIT", VoucherLine.amount), else_=-VoucherLine.amount)), 0))
        .where(VoucherLine.voucher_id.in_(ids), VoucherLine.account_code == _ACC_CLAIM_LOSS))
    return max_v, round(float(net or 0)), True


class ClaimLockBusy(Exception):
    """같은 청구의 심사 이벤트를 다른 워커 배치가 잡고 있음 — 일시 상태라 재시도(FAILED) 대상."""


async def _try_lock_claim(session: AsyncSession, key: str) -> None:
    """청구 단위 advisory 잠금 — **비차단**(pg_try_advisory_xact_lock).

    2026-10-09 시나리오 3회차: 종전 `pg_advisory_xact_lock`은 consume() 배치(최대 200건) 트랜잭션 끝까지
    잠금을 쥐어, 리스너·폴러 두 워커가 서로 다른 청구 순서로 잠그면 교착·장시간 정체가 났다. 못 잡으면
    기다리지 않고 ClaimLockBusy → 이벤트는 FAILED로 두고 다음 소비에서 재시도한다. 같은 트랜잭션의 재획득은
    PG가 허용(재진입)한다. sqlite(테스트)는 단일 커넥션이라 no-op."""
    if session.bind.dialect.name != "postgresql":
        return
    import zlib

    from erp.common.doc_no import _NS
    got = await session.scalar(select(func.pg_try_advisory_xact_lock(
        _NS | zlib.crc32(key.encode("utf-8")))))
    if not got:
        raise ClaimLockBusy(f"같은 청구를 다른 워커가 처리 중 — 다음 소비에서 자동 재시도({key[:40]})")


async def _newer_review_applied(session: AsyncSession, cid: str, event_id: int | None) -> bool:
    """같은 청구의 claim.reviewed 중 **현재 이벤트보다 늦게 수신된(인박스 id가 큰)** 것이 이미 APPLIED인가.

    2026-10-09 시나리오 3회차: HIS claim.reviewed 페이로드엔 시각이 없다(HIS 소스 확인) — 오래된 DEAD 재큐·
    재전송이 최신 결정 뒤에 처리되면 추론 정정이 최신 결정을 낡은 금액으로 덮었다. 수신 순서(인박스 id)를
    순서의 근거로 쓴다."""
    if event_id is None:
        return False
    return await session.scalar(select(HisInboundEvent.id).where(
        HisInboundEvent.event_type == "claim.reviewed", HisInboundEvent.status == "APPLIED",
        HisInboundEvent.id > event_id,
        HisInboundEvent.source_ref.startswith(f"claim:review:{cid}:", autoescape=True)
        | (HisInboundEvent.payload["claimId"].as_string() == cid)).limit(1)) is not None


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s[:10]) if s else None


def _kst_date(s: str | None) -> date | None:
    """시각 문자열의 **KST 날짜** — HIS는 `toISOString()`(UTC·Z)으로 보낸다. 앞 10자리를 자르면 KST
    00:00~09:00 건이 전날로 귀속돼(마감월이면 DEAD) 전표 일자가 틀린다(2026-10-09 전체 흐름 검토 A).
    날짜만 온 값은 그대로."""
    if not s:
        return None
    if len(s) <= 10:
        return date.fromisoformat(s[:10])
    dt = _dt(s)
    if dt is None:
        return date.fromisoformat(s[:10])
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(KST).date()


_CARD_KEYS = ("CARD", "CREDIT", "카드", "신용")


def _is_card_method(m: str | None) -> bool:
    u = (m or "").upper()
    return any(k in u for k in _CARD_KEYS)


def _dt(v: str | None) -> datetime | None:
    """안전 ISO datetime 파싱 — `Z` 정규화 + 실패 시 None(형제 sign_webhook_router._parse_dt 동형).

    무가드 `fromisoformat`은 비ISO/epoch `completedAt` 수신 시 예외→재시도 소진→DEAD로 계약이
    COMPLETED/발효로 못 넘어감(적대검토 L-1). 파싱 실패 시 None 반환(호출부가 기존값 유지)."""
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _resolve_dept(session: AsyncSession, name: str | None, cache: dict) -> int | None:
    if not name:
        return None
    if name in cache:
        return cache[name]
    did = await session.scalar(select(Department.id).where(Department.name == name))
    if did is None:
        d = Department(name=name)
        session.add(d)
        await session.flush()
        did = d.id
    cache[name] = did
    return did


async def _equipment_acquired(session, p, event_type, ctx):
    asset_no = p.get("assetNumber")
    if not asset_no:
        raise ValidationError("assetNumber 없음")
    if await session.scalar(select(FixedAsset.id).where(FixedAsset.asset_no == asset_no)):
        return {"skipped": "이미 등록"}
    await AssetService().register(
        session, asset_no=asset_no, name=p.get("name") or asset_no, category="MEDICAL",
        acquired_on=_d(p.get("purchaseDate")) or date.today(),
        acquisition_cost=float(p.get("purchasePrice") or 0),
        useful_life_years=_ASSET_LIFE_DEFAULT, payment_method="PAYABLE", created_by=None)
    return {"asset_no": asset_no}


async def _staff(session, p, event_type, ctx):
    emp_no = p.get("employeeNumber")
    if not emp_no:
        # 영구 실패 — 재시도해도 사번이 생기지 않는다(ValueError는 재시도 대상으로 분류돼 백오프만 반복)
        raise ValidationError("employeeNumber 없음 — HIS가 사번을 넣어 재발신해야 합니다")
    resigned = event_type == "staff.resigned" or p.get("status") == "RESIGNED"
    dept_id = await _resolve_dept(session, p.get("department"), ctx)
    emp = await session.scalar(select(Employee).where(Employee.employee_no == emp_no))
    if emp is None:
        # HIS 관리(의료진·정규직) 미러 — source=HIS·정규직 (SoT §3.5)
        hired = _d(p.get("hireDate")) or date.today()
        new_emp = Employee(
            employee_no=emp_no, name=p.get("name") or emp_no, department_id=dept_id,
            position=p.get("position"), source="HIS", employment_type="REGULAR",
            email=(str(p["email"]).strip().lower() if p.get("email") else None),
            hired_on=hired, resigned_on=_d(p.get("resignDate")) if resigned else None)
        session.add(new_emp)
        await session.flush()
        # 4대보험 취득 신고 추적 (P1-③) — HIS 동기화 입사도 동일 적재
        from erp.hr.insurance_report_service import InsuranceReportService
        await InsuranceReportService().record(session, employee_id=new_emp.id,
                                              event="ACQUIRE", basis_on=hired)
        await _link_user_by_email(session, new_emp, p.get("email"))
        await _notify_rehire_candidates(session, new_emp)
        if resigned:
            from erp.common.kst import kst_today
            from erp.hr.access_revoke import revoke_for_employee
            await revoke_for_employee(session, new_emp, today=kst_today())
        return {"created": emp_no}
    if emp.source != "HIS":     # SoT 가드 — ERP 관리 직원은 HIS가 덮어쓰지 않음
        return {"skipped": f"ERP 관리 직원(미러 대상 아님): {emp_no}"}
    emp.name = p.get("name") or emp.name
    if dept_id:
        emp.department_id = dept_id
    emp.position = p.get("position") or emp.position
    if p.get("email"):
        emp.email = str(p["email"]).strip().lower()    # JIT 연결 키 (0048)
    if resigned:
        # 이미 퇴사일이 있으면 resignDate 없는 재수신이 오늘로 덮어쓰지 않는다 — 퇴직금 기준일이 바뀌고
        # 상실 신고가 기준일만 다르게 중복 적재됐다(2026-10-09 검토 C).
        emp.resigned_on = _d(p.get("resignDate")) or emp.resigned_on or date.today()
        # 4대보험 상실 신고 추적 (P1-③) — HIS 퇴사 동기화도 동일 적재
        from erp.hr.insurance_report_service import InsuranceReportService
        await InsuranceReportService().record(session, employee_id=emp.id,
                                              event="LOSE", basis_on=emp.resigned_on)
    await _link_user_by_email(session, emp, p.get("email"))
    if resigned:
        # 퇴사일이 지났으면 ERP 로그인도 즉시 차단(미래 퇴사일은 일일 정기업무가 도래 시 처리)
        from erp.common.kst import kst_today
        from erp.hr.access_revoke import revoke_for_employee
        await revoke_for_employee(session, emp, today=kst_today())
    return {"updated": emp_no}


async def _notify_rehire_candidates(session, emp) -> None:
    """새 직원이 생겼는데 같은 이메일의 계정이 **퇴사 직원 기록에 연결돼 있으면** 재입사일 수 있다 —
    관리자에게 재연결 확인 알림(7차 검토: 퇴사→차단→몇 달 뒤 재입사는 차단 시점 알림으로 못 잡는다)."""
    if not emp.email:
        return
    from sqlalchemy import func

    from erp.common.identity import norm_email, relink_candidate
    from erp.common.models import Notification, User
    users = (await session.scalars(select(User).where(
        func.lower(func.trim(User.email)) == norm_email(emp.email),
        User.employee_id.isnot(None), User.employee_id != emp.id))).all()
    for u in users:
        if await relink_candidate(session, employee_id=u.employee_id, email=u.email) != emp.id:
            continue
        for aid in (await session.scalars(select(User.id).where(
                User.role == "ADMIN", User.is_active.is_(True)))).all():
            session.add(Notification(
                user_id=aid, type="ACCOUNT_RELINK_REVIEW", title="재입사 의심 계정 확인",
                body=f"새 직원 기록(#{emp.id})과 같은 이메일의 계정 #{u.id}이 퇴사 직원 기록에 연결돼 있습니다 — "
                     "같은 사람이면 사용자 관리에서 '재직 기록으로 재연결'을 실행하세요"))


async def _link_user_by_email(session, emp, email: str | None) -> None:
    """staff payload email → common.users 연결 (W3.1 답변④ 합의 — ESS 급여/연차 키).

    user.employee_id 미설정인 경우만 연결(기존 수동 매핑 보존). email 미동봉이면 무시.
    """
    if not email:
        return
    from sqlalchemy import func

    from erp.common.identity import active_employee_for_email, norm_email
    from erp.common.models import User
    # 규칙(common.identity): 재직 중이고 그 이메일의 유일한 재직 직원일 때만 연결 — 퇴사 행에 연결하면
    # 바로 이어지는 퇴사 차단이 재입사자 계정을 막았다(5차 검토). 계정도 대소문자 무관·여럿이면 연결 안 함.
    if await active_employee_for_email(session, email) != emp.id:
        return
    users = (await session.scalars(select(User).where(
        func.lower(func.trim(User.email)) == norm_email(email)))).all()
    if len(users) != 1:
        return
    user = users[0]
    if user.employee_id is None:
        user.employee_id = emp.id
        from erp.common.crypto import mask_id
        logger.info("직원-계정 email 연결", extra={"ctx": {
            "employee_no": mask_id(emp.employee_no), "user_id": user.id}})


# HIS insuranceType → ERP coverage (회신서 §2.2 — 매핑은 ERP 책임, HIS는 원enum 송신)
_COVERAGE_MAP = {"NHI": "NHIS", "MEDICAID": "MEDICAL_AID", "SELF_PAY": "NONCOVERED",
                 "NHIS": "NHIS", "MEDICAL_AID": "MEDICAL_AID",
                 "AUTO": "AUTO", "INDUSTRIAL": "INDUSTRIAL", "NONCOVERED": "NONCOVERED",
                 "PATRIOT": "PATRIOT", "VETERAN": "PATRIOT", "보훈": "PATRIOT"}


def _coverage(p: dict) -> str:
    # 라인 우선: insuranceCategory=NON_COVERED → 비급여 (회신서 §2.2)
    if (p.get("insuranceCategory") or "").upper() == "NON_COVERED":
        return "NONCOVERED"
    return _COVERAGE_MAP.get((p.get("coverage") or "").upper(), "NHIS")


async def _billing_line(session, p, event_type, ctx):
    """진료행위 라인 수신 (D-6 회신서 §2 확정) — lineId 멱등 치환, cancelled=취소.

    ERP 진료비 책정·사전심사 자동 적재의 원천. coverage는 HIS enum
    (NHI·MEDICAID·SELF_PAY 등) 그대로 받아 ERP가 매핑(_COVERAGE_MAP).
    """
    from erp.patient.models import BillingLine
    line_id, chart_no, code = p.get("lineId"), p.get("chartNo"), p.get("code")
    if not line_id or not chart_no or not code:
        raise ValidationError("lineId/chartNo/code 필수")
    if len(str(code)) > 20:        # EDI 코드 문자열(≤20)이어야 — uuid면 HIS 해석 누락
        raise ValidationError(f"code가 EDI 문자열이 아님(uuid 의심, len={len(str(code))}) — HIS 코드 해석 필요")
    # M-3: 컬럼 한도 선검증 — DB 예외 대신 명시 사유(건별 격리 리포트 가시성)
    if len(str(line_id)) > 64 or len(str(chart_no)) > 20:
        raise ValidationError(f"식별자 길이 초과(lineId≤64, chartNo≤20): "
                         f"{len(str(line_id))}/{len(str(chart_no))}")
    # Phase0 컷오버 ON: chartNo 필드=patientRef → 원 chart_no 역해소(OFF면 무변경·미등록=보류)
    chart_no = await resolve_inbound_chart(session, chart_no, event_type=event_type)
    row = await session.scalar(select(BillingLine).where(BillingLine.line_id == line_id))
    if row is None:
        row = BillingLine(line_id=line_id, chart_no=chart_no, code=code)
        session.add(row)
    treated_at_raw = p.get("treatedAt")
    row.encounter_id = p.get("encounterId")
    row.chart_no, row.code = chart_no, code
    row.encounter_type = p.get("encounterType") or "OUTPATIENT"
    row.admitted_on = _d(p.get("admittedOn"))
    row.dept_code = p.get("deptCode")
    row.code_type = p.get("codeType") or "TARIFF"
    row.count = float(p.get("count") or 1)
    row.treated_on = _d(p.get("treatedOn"))
    if treated_at_raw:
        from datetime import datetime as _dt
        row.treated_at = _dt.fromisoformat(treated_at_raw)
    row.patient_age = p.get("patientAge")
    row.patient_sex = (p.get("patientSex") or None)
    row.coverage = _coverage(p)
    row.special_case_type = p.get("specialCaseType")   # 산정특례 유형(D-6 정밀화·0050)
    row.medical_aid_class = p.get("medicalAidClass")   # 의료급여 종(1/2)
    row.drug_name = p.get("drugName")                  # 약품 매칭 근거(0053)
    row.drug_ingredient = p.get("ingredient")
    row.ingredient_code = p.get("ingredientCode")      # 주성분코드(DUR 직접 매칭·#72)
    row.pregnancy_status = p.get("pregnancyStatus")    # 임신상태(임부금기 DUR·#72)
    row.institution_grade = p.get("institutionGrade")  # HIS config 종별(가산=ERP·#75)
    row.price_basis = p.get("priceBasis")              # BASE_CATALOG(기본수가·#75)
    row.insurance_product_code = p.get("insuranceProductCode")  # HIS 동봉 보험코드(자격검증·0055)
    row.item_category = p.get("itemCategory")
    _dx = p.get("diagnosisCodes")                       # 상병코드(진단·의학적 심사) — list 또는 쉼표문자열
    row.diagnosis_codes = ((",".join(str(x) for x in _dx) if isinstance(_dx, list)
                            else str(_dx))[:120] if _dx else None)
    row.eligibility_status = p.get("eligibilityStatus")
    _cr = p.get("copayRate")
    row.his_copay_rate = float(_cr) if _cr is not None else None
    row.copay_category = p.get("copayCategory")    # 차상위(NEAR_POOR) 등 자격 본인부담 구분
    if p.get("unitPrice") is not None:      # 제안 B — HIS 산정가(대사·비급여 폴백)
        row.unit_price_his = float(p["unitPrice"])
    row.cancelled = bool(p.get("cancelled"))
    res = {"line_id": line_id, "cancelled": row.cancelled, "coverage": row.coverage}
    if row.treated_on is not None:     # 이벤트 기반 사전심사 표적(차트×월) — 소비 후 자동 재평가
        res["_prereview"] = (chart_no, row.treated_on.strftime("%Y-%m"))
    return res


async def _billing_settled(session, p, event_type, ctx):
    """HIS 일마감 집계 → 수익 전표(현금·카드미수/외래수익). 일자별 멱등."""
    # 🔴 HIS 실제 형태: {date, summary: {data: {totalRevenue, byPaymentMethod: {card: N, cash: M, ...}}}}
    #    종전엔 summary 최상위의 리스트([{paymentMethod,_sum}])를 가정해 실제 페이로드를 전부 '수납 0'으로
    #    건너뛰고 APPLIED 처리했다 — 외래 수납 매출 전표가 0건이 되는 무음 결함(2026-10-09 검토 A·CRITICAL).
    #    봉투(data) 유무·딕셔너리/리스트 두 형태를 모두 받는다.
    summary = p.get("summary") or {}
    if isinstance(summary.get("data"), dict):
        summary = summary["data"]
    bpm = summary.get("byPaymentMethod") or {}
    items = (bpm.items() if isinstance(bpm, dict) else
             (((m.get("paymentMethod") or m.get("method")),
               (m.get("_sum") or {}).get("paidAmount", m.get("amount", 0))) for m in bpm))
    cash = card = 0.0
    excluded: dict[str, float] = {}
    for method, amt in items:
        amt = float(amt or 0)
        m = (method or "").lower()
        if m in ("voucher", "deposit"):
            # 검진권(voucher)은 voucher.* 이벤트가 따로 전기하고, 예치금(deposit)은 이미 받은 돈이라
            # 현금으로 잡으면 이중·과대 계상된다(2차 검토 A). 회계 처리는 HIS 계약 결정 대기.
            excluded[m] = excluded.get(m, 0) + amt
            continue
        if _is_card_method(method):
            card += amt
        else:                                    # cash·transfer·unknown(원장 미기록)·mixed
            cash += amt
    if excluded:
        logger.warning("일마감 결제수단 제외(검진권·예치금)", extra={"ctx": {
            "date": p.get("date"), **{k: round(v) for k, v in excluded.items()}}})
    total = cash + card
    if total <= 0:
        if float(summary.get("totalRevenue") or 0) > 0 and not excluded:
            # 총수익은 있는데 수단별 합이 0 — 형태가 또 바뀐 것. APPLIED로 삼키지 않고 DEAD로 드러낸다.
            raise ValidationError("일마감 수단별 합계 0인데 totalRevenue>0 — 페이로드 형태 확인 필요")
        return {"skipped": "수납 0"}
    # 대변(수익)=차변 라운딩 합으로 산출 — round(cash)+round(card) != round(cash+card)로 인한
    # 차대 불균형(ValidationError→그날 수익전표 유실)을 구조적 방지. 각 레그 선라운딩·합산.
    lines = []
    debit_total = 0
    if cash:
        r = round(cash)
        lines.append(VoucherLineDTO(_ACC_CASH, "DEBIT", r))
        debit_total += r
    if card:
        r = round(card)
        lines.append(VoucherLineDTO(_ACC_CARD_AR, "DEBIT", r))
        debit_total += r
    lines.append(VoucherLineDTO(_ACC_REVENUE, "CREDIT", debit_total))
    # 정정(#2 correctionOf+revision) 지원 — v≥2면 직전분 역분개 후 재전기, 없으면 현행+무음유실 가드(§1)
    await _post_with_correction(
        session, source_module="his.billing", base_ref=_bounded_ref("settle", str(p.get('date')), limit=_BASE_REF_MAX),
        version=_revision(p), lines=lines,
        voucher_on=_d(p.get("date")) or date.today(), memo=f"HIS 일마감 {p.get('date')}",
        expected_debit=debit_total)
    return {"date": p.get("date"), "cash": cash, "card": card, "total": total}


async def _billing_adjusted(session, p, event_type, ctx):
    """HIS 정정/환불(실시간) → 역분개(수익 차감/현금 환급). billingId·유형별 멱등."""
    amount = float(p.get("amount", 0) or 0)
    if amount <= 0:
        return {"skipped": "금액 0"}
    bid, kind = p.get("billingId"), p.get("type", "CANCEL")
    # ⚠ HIS는 취소 금액을 `paidAmount ?? totalAmount`로 보내 미수납(PENDING) 청구 취소도 청구액이 온다 —
    #    ERP는 실수납 여부를 알 수 없어 HIS 계약 수정(`?? 0`) 대기(결정 대기함 his-billing-cancel-amount).
    # 환불 대변 계정 — 결제수단별(H-4). CARD면 카드미수(1132) 취소여야 함(현금 시재 미변동). method
    # 필드가 오면 즉시 1132 라우팅, 없으면 현행 현금(1110·하위호환). _billing_settled의 CARD 분리와 대칭.
    method = (p.get("paymentMethod") or p.get("method") or "").upper()
    credit_acct = _ACC_CARD_AR if _is_card_method(method) else _ACC_CASH
    # 전표 키에 정정 버전(없으면 이벤트 ref)을 넣는다 — HIS는 부분환불을 여러 번 허용하고 건마다 새
    # 이벤트를 보내는데, 키가 `adjust:{bid}:{type}` 고정이라 2회차부터 기존 전표가 반환돼 무음 유실됐다
    # (검토 A·F). 환불은 누적이라 직전 건을 역분개하지 않는다(_post_with_correction 아님).
    # 이벤트 ref 우선 — HIS revision은 outbox 행 수+1이라 연속 환불에서 같아질 수 있다(ref는 건마다 유일)
    uniq = ctx.get("source_ref") or p.get("revision") or "1"
    await _vouchers.create(session, source_module="his.billing",
                           source_ref=_bounded_ref(f"adjust:{bid}", f"{kind}:{uniq}"),
                           voucher_on=_kst_date(p.get("at")) or date.today(),
                           lines=[VoucherLineDTO(_ACC_REVENUE, "DEBIT", round(amount)),
                                  VoucherLineDTO(credit_acct, "CREDIT", round(amount))],
                           memo=f"HIS 정정/환불 {kind} {bid}")
    return {"billing_id": bid, "type": kind, "amount": amount, "credit": credit_acct}


# 처분(장부 제거) 확정 힌트만 — RETIRED·불용은 HIS에서 되돌릴 수 있는 상태라 처분 전표(비가역)로 보내지
# 않는다(2차 검토 E — 결정 대기 his-equipment-out-of-service 전까지 상태만 기록).
_DISPOSAL_HINTS = ("DISPOS", "SCRAP", "제각", "폐기", "매각")


async def _equipment_status_changed(session, p, event_type, ctx):
    """장비 상태 변경 → 제각/폐기면 FixedAsset DISPOSED(감가중지). 그 외 변경 없음."""
    asset_no = p.get("assetNumber") or p.get("erpAssetCode")
    if not asset_no:
        raise ValidationError("assetNumber 없음")
    asset = await session.scalar(select(FixedAsset).where(FixedAsset.asset_no == asset_no))
    if asset is None:
        return {"skipped": f"미등록 자산: {asset_no}"}
    raw = (p.get("status") or "").upper()
    if any(h in raw or h in (p.get("status") or "") for h in _DISPOSAL_HINTS):
        if asset.status == "DISPOSED":
            return {"asset_no": asset_no, "status": "DISPOSED", "idempotent": True}
        # 처분 전표까지 — 상태만 바꾸면 1710·1810이 재무상태표에 영구히 남고, 처분일까지 감가 현행화도
        # 없으며, 이후 수동 처분은 '이미 처분'으로 막혀 바로잡을 수 없었다(2026-10-09 검토 E).
        from erp.finance.asset_service import AssetService
        on = _kst_date(p.get("changedAt") or p.get("occurredAt")) or date.today()
        out = await AssetService().dispose(session, asset_id=asset.id, disposed_on=on,
                                           disposal_amount=float(p.get("disposalAmount") or 0))
        return {"asset_no": asset_no, "status": "DISPOSED", "disposal": out}
    return {"asset_no": asset_no, "his_status": p.get("status"), "note": "IN_USE 유지"}


async def _equipment_maintenance(session, p, event_type, ctx):
    """장비 유지보수 → 유지보수비 전표(관리비용 5510/미지급금 2110). 자산·시점별 멱등."""
    cost = float(p.get("cost", 0) or 0)
    asset_no = p.get("assetNumber") or p.get("erpAssetCode") or "unknown"
    if cost <= 0:
        return {"skipped": "비용 0"}
    await _vouchers.create(session, source_module="his.equipment",
                           source_ref=_bounded_ref(f"maint:{asset_no}", str(p.get('performedAt'))),
                           voucher_on=_d(p.get("performedAt")) or date.today(),
                           lines=[VoucherLineDTO("5510", "DEBIT", round(cost)),
                                  VoucherLineDTO(_ACC_AP_MAINT, "CREDIT", round(cost))],
                           memo=f"장비 유지보수 {asset_no} {p.get('maintenanceType') or ''}")
    return {"asset_no": asset_no, "cost": cost, "type": p.get("maintenanceType")}


def _shift_minutes(start: str | None, end: str | None) -> int:
    try:
        sh, sm = (start or "").split(":")[:2]
        eh, em = (end or "").split(":")[:2]
        mins = (int(eh) * 60 + int(em)) - (int(sh) * 60 + int(sm))
        return mins + 1440 if mins < 0 else mins   # 야간 교대(자정 넘김)
    except (ValueError, AttributeError):
        # 파싱 불가 교대는 0분 처리하되 가시화 — 근로시간/80h 규제 집계 무음 누락 방지.
        logger.warning("교대 시간 파싱 실패(0분 처리)",
                       extra={"ctx": {"start": start, "end": end}})
        return 0


async def _staff_schedule(session, p, event_type, ctx):
    """근무표 변경 → AttendanceSummary 월 집계(근무일·야간·근무분). employeeNumber/staffId 매핑."""
    emp = None
    if p.get("employeeNumber"):
        emp = await session.scalar(select(Employee).where(
            Employee.employee_no == p["employeeNumber"]))
    if emp is None and p.get("staffId"):     # staffId = clinicUserId = external_user_id
        emp = await session.scalar(select(Employee).where(
            Employee.external_user_id == p["staffId"]))
    if emp is None:
        raise ValueError(f"직원 미상: {p.get('employeeNumber') or p.get('staffId')}")
    agg: dict = {}
    for sc in p.get("schedules") or []:
        d = _d(sc.get("date"))
        if not d:
            continue
        ym = f"{d.year:04d}-{d.month:02d}"
        a = agg.setdefault(ym, {"days": 0, "night": 0, "minutes": 0})
        a["days"] += 1
        if (sc.get("shiftType") or "").upper().startswith("N"):
            a["night"] += 1
        a["minutes"] += _shift_minutes(sc.get("startTime"), sc.get("endTime"))
    for ym, a in agg.items():               # schedule_changed = 해당월 현재 근무표(치환)
        summ = await session.scalar(select(AttendanceSummary).where(
            AttendanceSummary.employee_id == emp.id, AttendanceSummary.yyyymm == ym))
        if summ is None:
            summ = AttendanceSummary(employee_id=emp.id, yyyymm=ym)
            session.add(summ)
        summ.work_days, summ.night_shift_count, summ.total_work_minutes = (
            a["days"], a["night"], a["minutes"])
    return {"employee": emp.employee_no, "months": list(agg)}


async def _staff_attendance(session, p, event_type, ctx):
    """HIS 타임클럭 출퇴근 (W3.1 요청1) → 일자행 upsert → 기존 월 재집계 파이프라인.

    이벤트 멱등은 인박스 ref(att:사번:분:type) UNIQUE가 보장 — 여기선 일자행 갱신만.
    규칙: CLOCK_IN=최초(이른) 시각, CLOCK_OUT=최종(늦은) 시각, correctionOf=덮어쓰기.
    자정 넘김 퇴근(12시 이전 OUT)은 전일 미퇴근 행에 페어링(야간 교대).
    """
    emp_no, at_raw = p.get("employeeNumber"), p.get("at")
    # HIS 실송신은 IN/OUT 축약형(2026-06-11 실측) — 스펙 표기와 함께 관용 수용
    clock_type = {"IN": "CLOCK_IN", "OUT": "CLOCK_OUT"}.get(
        (p.get("type") or "").upper(), p.get("type"))
    if not emp_no or not at_raw or clock_type not in ("CLOCK_IN", "CLOCK_OUT"):
        raise ValidationError("employeeNumber/at/type(CLOCK_IN|CLOCK_OUT|IN|OUT) 필수")
    at = datetime.fromisoformat(at_raw)
    # clock_in/out은 KST 벽시계로 다룸(다운스트림 .hour 휴리스틱). 입력이 aware면 KST 벽시계로 환산해
    # naive 통일 — PG timestamptz가 UTC-aware로 되읽혀 naive↔aware 비교 오류 나던 것 방지(PG 레인 적발).
    if at.tzinfo is not None:
        at = at.astimezone(KST).replace(tzinfo=None)
    work_on = at.date()

    async def _day_row(d: date) -> GroupwareAttendanceStaging | None:
        return await session.scalar(select(GroupwareAttendanceStaging).where(
            GroupwareAttendanceStaging.source_ref == f"his:{emp_no}:{d.isoformat()}"))

    row = await _day_row(work_on)
    # 자정 넘김 퇴근 — 전일 IN 있고 OUT 없는 행이 있으면 그 행에 페어링
    if clock_type == "CLOCK_OUT" and at.hour < 12 and (row is None or row.clock_in is None):
        prev = await _day_row(work_on - timedelta(days=1))
        if prev is not None and prev.clock_in is not None and prev.clock_out is None:
            row, work_on = prev, work_on - timedelta(days=1)
    if row is None:
        row = GroupwareAttendanceStaging(
            source_ref=f"his:{emp_no}:{work_on.isoformat()}",
            external_user_id=p.get("clinicUserId") or f"his:{emp_no}",
            employee_no=emp_no, work_on=work_on, status="NEW")
        session.add(row)

    correction = bool(p.get("correctionOf"))
    # 되읽힌 기존 값(PG=진짜 UTC aware)을 KST로 환산해 naive 통일 후 naive at과 min/max — 라벨-strip이면
    # -9h 왜곡값이 min()에 잡혀 중복 출근 이벤트 시 저장값 자체가 오염된다(v1.257.2).
    prev_in, prev_out = as_kst_wallclock(row.clock_in), as_kst_wallclock(row.clock_out)
    if clock_type == "CLOCK_IN":
        row.clock_in = at if (prev_in is None or correction) else min(prev_in, at)
    else:
        row.clock_out = at if (prev_out is None or correction) else max(prev_out, at)
    row.status = "NEW"      # 월 재집계(consume_attendance_staging) 트리거
    return {"day": work_on.isoformat(), "type": clock_type}


async def _staff_qualification(session, p, event_type, ctx):
    """자격/면허 변경 → hr.License upsert. userId=clinicUserId→external_user_id 매핑."""
    uid = p.get("userId")
    emp = await session.scalar(select(Employee).where(Employee.external_user_id == uid)) \
        if uid else None
    if emp is None:
        raise ValueError(f"직원 미상(userId): {uid}")
    cred = str(p.get("credentialId") or p.get("name") or "").strip()
    lic = await session.scalar(select(License).where(
        License.employee_id == emp.id, License.license_no == cred))
    expires = _d(p.get("expiresAt"))
    if lic is None:
        session.add(License(employee_id=emp.id, license_type=(p.get("type") or "")[:40],
                            license_no=cred[:40], expires_on=expires))
        return {"created": cred}
    lic.expires_on = expires
    if p.get("type"):
        lic.license_type = p["type"][:40]
    return {"updated": cred}


async def _cssd_po_approved(session, p, event_type, ctx):
    """CSSD 발주 승인 → 구매 약정 기록(정보성). 회계 인식(미지급금)은 입고/송장 단계 — 전표 없음."""
    return {"po": p.get("poNo"), "supplier": p.get("supplierId"),
            "total": p.get("totalAmount"), "lines": len(p.get("lines") or [])}


def _care_month(p: dict) -> str | None:
    """청구 진료월 해소 — payload 변형(careMonth/careYm/reviewYm/claimYm). YYYYMM→YYYY-MM 정규화."""
    cm = (p.get("careMonth") or p.get("careYm") or p.get("reviewYm")
          or p.get("claimYm") or p.get("claimMonth"))
    if cm is None:
        return None
    cm = str(cm)
    if len(cm) == 6 and cm.isdigit():
        return f"{cm[:4]}-{cm[4:6]}"
    return cm[:7] or None


async def _claim_submitted(session, p, event_type, ctx):
    """보험청구 제출 → 미수금 인식(보험미수금/외래수익) + 제출 청구 캡처(이중소스 정합). 청구건별 멱등."""
    amt = float(p.get("claimedAmount", 0) or 0)
    cid = p.get("claimId") or p.get("claimNumber")
    if amt <= 0:
        return {"skipped": "금액 0"}
    await _vouchers.create(session, source_module="his.claim",
                           source_ref=_bounded_ref("claim:submit", str(cid)),
                           voucher_on=date.today(),
                           lines=[VoucherLineDTO(_ACC_INS_AR, "DEBIT", round(amt)),
                                  VoucherLineDTO(_ACC_REVENUE, "CREDIT", round(amt))],
                           memo=f"보험청구 제출 {p.get('claimNumber')}")
    # 제출 청구 캡처(claim_id 멱등) — MEDRLT 청구액과 진료월 단위 대사용. care_month 미동봉 시 NULL.
    from erp.claims.models import ClaimSubmission
    if cid is not None and not await session.scalar(
            select(ClaimSubmission.id).where(ClaimSubmission.claim_id == str(cid))):
        session.add(ClaimSubmission(
            claim_id=str(cid), claim_number=p.get("claimNumber"), care_month=_care_month(p),
            claimed_amt=round(amt),
            submitted_on=_d(p.get("submittedAt") or p.get("occurredAt")) or date.today()))
    return {"claim": cid, "ar": amt}


async def _claim_reviewed(session, p, event_type, ctx):
    """심사 결과 → 삭감 손실 인식(삭감손실/보험미수금). 삭감액>0만 전표. 청구건별 멱등.

    이벤트 카탈로그 v1 강화 필드: status·approvedAmount(심사확정 보험미수금 AR)·reducedAmount(삭감손실)
    ·rejectReason. 표준 항등식 claimed=approved+reduced에서 submit(AR=claimed) 후 삭감 전표로 AR=approved
    정합. reducedAmount 미제공 시 claimedAmount−approvedAmount로 보정(정합 확보·안전 폴백).
    """
    cid = p.get("claimId") or p.get("claimNumber")
    status = (p.get("status") or "").upper()
    approved = float(p.get("approvedAmount", 0) or 0)
    reduced = float(p.get("reducedAmount", 0) or 0)
    claimed = float(p.get("claimedAmount", 0) or 0)
    base_ref = _bounded_ref("claim:review", str(cid), limit=_BASE_REF_MAX)
    # 같은 청구의 재심사 동시 처리 직렬화(리스너+폴러가 각각 v1을 읽고 둘 다 v2를 추론해 뒤엣것이 무음
    # no-op 되던 경합 — 2026-10-09 독립검토). 결정·전표 상태를 읽기 **전에** 청구 단위 advisory 잠금.
    await _try_lock_claim(session, base_ref)   # 비차단 — 못 잡으면 FAILED·재시도(3회차)
    sub = None
    if cid is not None:
        from erp.claims.models import ClaimSubmission
        sub = await session.scalar(
            select(ClaimSubmission).where(ClaimSubmission.claim_id == str(cid)))
        # 리뷰 페이로드에 claimedAmount 누락 시 제출 청구액(ClaimSubmission)으로 보정 — 아래 상한(min)이
        # claimed>0 게이트라, 누락되면 직접 제공된 reducedAmount가 무상한 전기돼 1140 음수·삭감손실
        # 과대(전체검토 HIGH). 제출 청구액을 상한 기준으로 삼아 방지.
        if claimed <= 0 and sub is not None:
            claimed = float(sub.claimed_amt or 0)
    # 삭감액 미제공 시 청구−확정으로 보정(정합) — 단 **전액삭감(REJECTED/DENIED)이거나 확정액이
    # 실재(approved>0)할 때만**. status가 승인계열인데 approvedAmount가 데이터결함으로 누락되면
    # claimed−0 = 전액을 삭감손실로 오계상(AR 소각)하므로 보정하지 않고 판정 보류(안전 방향).
    if reduced <= 0 and claimed > 0 and (status in ("REJECTED", "DENIED") or approved > 0):
        reduced = round(claimed - approved, 2)
    # 삭감액 상한 — 청구액 초과 불가(보험미수금 1140 음수·삭감손실 과대 방지)
    if reduced > 0 and claimed > 0:
        reduced = min(reduced, claimed)

    rev = _revision(p)
    post_gl = True
    # 2026-10-09 시나리오 S-B: HIS는 재심사(claim.reviewed 재발행)에 revision/correctionOf를 싣지 않고
    # 이벤트 ref에 금액만 박아 보낸다(`claim:review:<id>:APPROVED:500000:0`). 종전엔 이것을 v1로 취급해
    # ① 삭감 0 재심사는 "삭감 없음"으로 APPLIED — 5910·1140은 직전 삭감 그대로인데 decided_amt만
    # 500,000으로 바뀌어 GL과 보조원장이 어긋났고 ② 삭감액이 바뀐 재심사는 "기존 전표와 금액 불일치"로
    # DEAD였다. 명시 revision이 없고 이미 심사 결정이 반영된 청구면 **직전 유효 결정의 정정**으로 보고
    # revision = 현재 유효 버전 + 1을 추론해 기존 정정 경로(_post_with_correction)를 탄다. 같은 금액
    # 재수신은 멱등 no-op. 명시 revision(HIS가 추후 동봉)은 그대로 우선한다.
    explicit = (p.get("revision") is not None or p.get("correctionVersion") is not None
                or bool(p.get("correctionOf")))
    if not explicit and cid is not None:
        max_v, net_loss, has_voucher = await _review_ledger_state(session, base_ref)
        if has_voucher or (sub is not None and sub.decided_on is not None):
            current_v = max(max_v, (sub.decided_rev or 1) if sub is not None else 1)
            # 2026-10-09 시나리오 3회차: 늦게 수신된 결정이 이미 반영됐으면 이 이벤트는 낡은 재생(재큐·재전송)이다.
            # 같은 금액이면 조용히 멱등(확정액·결정일도 건드리지 않음), 다르면 최신 결정을 덮지 않고 DEAD.
            if await _newer_review_applied(session, str(cid), ctx.get("event_id")):
                if round(reduced) == round(net_loss):
                    return {"claim": cid, "status": status,
                            "note": "늦게 수신된 심사 결과가 이미 반영됨 — 낡은 재수신 무시(멱등)"}
                raise ValidationError(
                    "이 심사 결과보다 늦게 수신된 같은 청구의 심사 결과가 이미 반영돼, 낡은 결과로 덮지 않았습니다 "
                    f"— 심사팀이 최신 결정을 확인한 뒤 {_RESOLVE_HINT}하세요(정정이 필요하면 HIS에 revision을 "
                    "실어 재발행 요청)",
                    {"claim": cid, "incoming_reduced": round(reduced), "booked_reduced": round(net_loss)})
            if reduced <= 0 and approved <= 0:
                # 금액 판정 불가(승인액·삭감액 모두 없음) — 직전 삭감을 역분개하면 AR을 근거 없이 되살리고,
                # 그냥 두면 decided_amt만 지워진다. 어느 쪽도 안전하지 않아 DEAD로 가시화.
                raise ValidationError(
                    "HIS 재심사 이벤트에 approvedAmount·reducedAmount가 모두 없어 정정 금액을 판정할 수 "
                    "없습니다 — HIS가 금액을 채워 재발행해야 합니다(ERP는 직전 결정 유지)",
                    {"claim": cid, "status": status})
            if round(reduced) == round(net_loss):
                rev, post_gl = current_v, False      # 같은 결정 재수신 — GL 불변(멱등)
            else:
                # 이의신청 인정(claims 모듈 복원 전표 DR1140/CR5910 `appeal-adj-{id}`)이 GL을 이미 움직였으면
                # 재심사 금액으로 다시 역분개하면 1140 이중 복원·5910 음수가 된다(독립검토 HIGH). HIS 청구와
                # 통보서 조정건 사이에 FK가 없어(#621) 그 이의가 이 청구 것인지 ERP가 알 수 없다.
                # 2026-10-09 시나리오 5회차: 날짜 가드(오탐 — 이의를 닫은 날 모든 재심사 DEAD)·id+정확 금액 가드
                # (복수 조정·부분 인정·연속 이의·금액 불일치를 놓쳐 이중 복원)가 모두 추측이라 실패했다. 추측을
                # 그만두고 **삭감을 줄이는(복원) 방향**은 운영자 확인 전까지 보류(fail-closed)한다. 삭감 증가·
                # 같은 금액·명시 revision은 이의 복원과 겹치지 않아 종전대로 자동 반영. 확인 표식은 관리자 >
                # 연동 인박스 '확인 후 반영'(apply-confirmed EP)만 싣는다 — 다른 가드(낡은 재생·충돌·잠금)는 그대로.
                if reduced < net_loss and not p.get("_erpConfirmedRestore"):
                    raise ValidationError(
                        f"{RESTORE_HOLD_PREFIX}(revision 없음)는 ERP 이의신청 인정으로 이미 복원됐을 수 있어 "
                        "자동 반영하지 않습니다 — 심사팀이 이의 결과와 대조해, 아직 반영되지 않았으면 관리자 > "
                        "연동 인박스에서 '확인 후 반영', 이미 반영됐으면 '처리 완료(사유)'로 종결하세요",
                        {"claim": cid, "incoming_reduced": round(reduced),
                         "booked_reduced": round(net_loss)})
                rev = current_v + 1                  # 금액이 바뀐 재심사 = 정정

    # 제출 청구 결정 마킹(변동대가 미결정 풀 정렬) — 실제 삭감(5910)과 동일 신호(claim.reviewed)로
    # 결정을 표시해 추정 풀에서 제외 → 이중차감 제거. 삭감 유무 무관(전액승인도 결정됨). 조기반환 전 수행.
    # 정정(revision ≥ 기존)이면 확정액을 갱신한다 — 최초 리뷰만 반영해 인식 AR 기준이 낡아, 정정 승인액
    # 입금이 1140 대신 2141로 오분리됐다(검토 B). 확정액은 approved, 없으면 claimed−reduced.
    # 4회차 검토: 같은 금액 재수신(post_gl=False·멱등)은 결정일을 바꾸지 않는다 — 결정일은 실제로 GL을 움직인
    # 결정의 날짜다(변동대가 풀·지급 대사의 기준). 5회차: 이의 가드는 더 이상 결정일을 쓰지 않는다(복원 방향은
    # 운영자 확인으로 보류).
    if sub is not None and (sub.decided_on is None or (post_gl and rev >= (sub.decided_rev or 1))):
        # KST 날짜 — HIS는 toISOString()(UTC)이라 앞 10자리는 KST 00~09시 결정을 전날로 잡는다(4회차)
        sub.decided_on = _kst_date(p.get("reviewedAt") or p.get("occurredAt")) or date.today()
        sub.decided_amt = (round(approved) if approved else
                           (round(claimed - reduced) if reduced > 0 and claimed > 0 else None))
        sub.decided_rev = rev
    if not post_gl:
        return {"claim": cid, "status": status, "approved": approved,
                "note": "동일 심사 결과 재수신 — 멱등"}
    if reduced <= 0:
        if rev >= 2:
            # 정정본이 '삭감 0'(전액 승인)이면 직전 삭감손실을 역분개해야 한다 — 종전엔 여기서 반환해
            # v1 삭감이 그대로 남았다(5910 과대·1140 과소·검토 B).
            await _post_with_correction(
                session, source_module="his.claim", base_ref=base_ref,
                version=rev, lines=[], voucher_on=date.today(), expected_debit=0,
                memo=f"심사 정정 {p.get('claimNumber')} 삭감 0")
            return {"claim": cid, "status": status, "approved": approved, "note": "정정 — 삭감 역분개"}
        return {"claim": cid, "status": status, "approved": approved, "note": "삭감 없음"}
    # 정합 경고(차단 아님) — 표준 항등식 claimed=approved+reduced 위반은 가시화(데이터 품질)
    if approved > 0 and claimed > 0 and abs(approved + reduced - claimed) > 1:
        logger.warning("claim.reviewed 금액 항등식 불일치",
                       extra={"ctx": {"claim": cid, "claimed": claimed,
                                      "approved": approved, "reduced": reduced}})
    # 심사결과 정정(#2 correctionOf+revision) 지원 — v≥2면 직전 삭감분 역분개 후 재전기, 없으면 §2 가드
    await _post_with_correction(
        session, source_module="his.claim", base_ref=base_ref,
        version=rev,
        lines=[VoucherLineDTO(_ACC_CLAIM_LOSS, "DEBIT", round(reduced)),
               VoucherLineDTO(_ACC_INS_AR, "CREDIT", round(reduced))],
        voucher_on=date.today(), expected_debit=round(reduced),
        memo=(f"심사 {status or ''} {p.get('claimNumber')} "
              f"삭감 {p.get('rejectReason') or ''}").strip())
    return {"claim": cid, "reduced": reduced, "approved": approved, "status": status}


async def _cost_period_closed(session, p, event_type, ctx):
    """HIS 원가 기간마감 통지(정보성·이벤트 카탈로그 v1 신규) — 해당 기간 소스데이터 확정 신호.

    ERP 원가 스냅샷(CostAllocationSnapshot)은 관리자 입력(재료·간접비) 기반 별도 산출이라, 이 통지만으로
    자동 산출/전표는 하지 않는다. 비가역 회계처리 없이 확인·요약 반환(멱등·DEAD 방지). 페이로드 스키마
    확정(HIS 카탈로그) 후 스냅샷 트리거 등으로 확장. period 필드는 변형 수용(period/yyyymm/periodCode).
    """
    period = (p.get("period") or p.get("yyyymm") or p.get("periodCode")
              or p.get("closedPeriod") or "")
    return {"cost_period_closed": period or None, "closed_at": p.get("closedAt"),
            "note": "정보성 확인 — ERP 원가 스냅샷은 관리자 입력 기반 별도 산출"}


def _doc_id(p: dict):
    """제증명/발급문서 식별(멱등 근거) — certificate.issued·eapproval.document_issued 양쪽 필드 수용."""
    return (p.get("documentId") or p.get("docId") or p.get("certificateId")
            or p.get("certId") or p.get("id"))


def _doc_fee(p: dict) -> int:
    """발급 수수료 — amount·fee·feeAmount 어휘 수용."""
    return round(float(p.get("amount") or p.get("fee") or p.get("feeAmount") or 0))


async def _certificate_issued(session, p, event_type, ctx):
    """제증명(진단서·소견서 등) 발급 수수료 → 4120 제증명수입(HIS e결재 연계·진료수익 4110과 분리).

    HIS 문서 재점검: 발급(issuance)은 수수료를 확정만 하고 billing/outbox를 발행하지 않아 **이 이벤트가
    제증명 수입 ERP 연계의 유일한 경로**(누락 시 4120 수입 회계 누락). certificate.issued(구)·
    eapproval.document_issued(정정·필수경로) 동일 처리. 결제수단별 현금(1110)/카드미수(1132) 차변·4120
    대변. 문서 식별로 멱등(양 이벤트 중복수신에도 1회 전기). 금액 0/음수는 스킵.
    """
    cid = _doc_id(p)
    amount = _doc_fee(p)
    if amount <= 0:
        return {"skipped": "금액 0", "document_id": cid}
    method = (p.get("paymentMethod") or p.get("method") or "").upper()
    debit_acct = _ACC_CARD_AR if method == "CARD" else _ACC_CASH
    await _vouchers.create(
        session, source_module="his.certificate", source_ref=_bounded_ref("cert", str(cid)),
        voucher_on=_d(p.get("issuedAt") or p.get("at")) or date.today(),
        lines=[VoucherLineDTO(debit_acct, "DEBIT", amount),
               VoucherLineDTO(_ACC_CERT_REVENUE, "CREDIT", amount)],
        memo=f"제증명 수입 {p.get('certificateType') or p.get('documentType') or ''} {cid}".strip())
    return {"document_id": cid, "amount": amount, "account": _ACC_CERT_REVENUE}


async def _document_adjusted(session, p, event_type, ctx):
    """제증명 발급 취소/환불(eapproval.document_adjusted) → 4120 수입 취소(역분개).

    발급 후 취소·환불 시 원 수입을 되돌린다: Dr 4120 제증명수입 / Cr 현금(1110)·카드미수(1132)(환불 수단).
    adjustmentId로 멱등(별도 전표). 환불액(refundAmount/amount) 0/음수는 스킵. 원 발급건이 미전기여도
    (금액 0 스킵됐던 경우) 역분개만 독립 성립 — 회계는 실제 환불 현금흐름 기준.
    """
    adj_id = p.get("adjustmentId") or p.get("adjId") or p.get("id")
    doc_id = _doc_id(p)
    amount = round(float(p.get("refundAmount") or p.get("amount") or p.get("fee") or 0))
    if amount <= 0:
        return {"skipped": "환불액 0", "adjustment_id": adj_id, "document_id": doc_id}
    method = (p.get("paymentMethod") or p.get("method") or "").upper()
    credit_acct = _ACC_CARD_AR if method == "CARD" else _ACC_CASH
    await _vouchers.create(
        session, source_module="his.certificate", source_ref=_bounded_ref("cert-adj", str(adj_id or doc_id)),
        voucher_on=_d(p.get("adjustedAt") or p.get("at")) or date.today(),
        lines=[VoucherLineDTO(_ACC_CERT_REVENUE, "DEBIT", amount),
               VoucherLineDTO(credit_acct, "CREDIT", amount)],
        memo=f"제증명 발급취소/환불 {doc_id or ''} {adj_id or ''}".strip())
    return {"adjustment_id": adj_id, "document_id": doc_id, "refund": amount,
            "account": _ACC_CERT_REVENUE}


async def _preauth_order_approved(session, p, event_type, ctx):
    """사전승인 고가오더(preauth.order_approved) → 관리회계 기록(GL 미전기·약정 단계).

    사전승인=약정이라 GL 전기는 시기상조(실제 비용은 발주/입고 시 발생). preauth_orders에 카테고리·금액을
    기록하고 category_summary 리포트로 집계. orderRef로 멱등(재수신 시 최신값 갱신)."""
    from erp.finance.models import PreauthOrder
    ref = p.get("orderRef") or p.get("orderId") or p.get("id")
    if not ref:
        raise ValidationError("사전승인 오더 식별(orderRef/orderId) 누락")
    row = await session.scalar(select(PreauthOrder).where(PreauthOrder.order_ref == str(ref)))
    if row is None:
        row = PreauthOrder(order_ref=str(ref))
        session.add(row)
    row.category = (p.get("category") or "UNCLASSIFIED")[:40]
    row.amount = round(float(p.get("amount") or p.get("estimatedAmount") or 0))
    row.dept_code = p.get("deptCode") or p.get("departmentCode")
    row.approver = (p.get("approver") or p.get("approvedBy") or "")[:80] or None
    row.approved_on = _d(p.get("approvedAt") or p.get("at")) or date.today()
    row.note = (p.get("note") or p.get("orderName") or "")[:300] or None
    # 승인 채널 — PORTAL 유형 명시(HIS 문서 보완). 정상 승인은 사후승인 미결 아님.
    ch = (p.get("channel") or p.get("approvalChannel") or "STANDARD").upper()
    row.channel = "PORTAL" if ch == "PORTAL" else "STANDARD"
    row.order_type = (p.get("orderType") or p.get("clinicalType") or "")[:40] or None
    row.post_approved = True
    await session.flush()
    return {"preauth_order": str(ref), "category": row.category, "amount": float(row.amount),
            "channel": row.channel, "note": "관리회계 기록(GL 미전기·약정)"}


async def _preauth_emergency_bypass(session, p, event_type, ctx):
    """응급 사전승인 우회(preauth.emergency_bypass) → 관리회계 인지 + 사후승인 미결 표시(규제·원가·감사).

    응급 상황에서 사전승인 없이 발행된 마약류·수혈·고가검사 등을 ERP가 인지할 유일한 경로(HIS 문서 보완).
    preauth_orders에 channel=EMERGENCY_BYPASS·post_approved=False(사후승인 미결)로 기록 — category_summary가
    미결 건수로 노출해 사후 통제 유도. GL 미전기(약정 단계 동일). orderRef 멱등(사후승인 이벤트가 오면
    _preauth_order_approved가 동일 ref를 post_approved=True로 갱신)."""
    from erp.finance.models import PreauthOrder
    ref = p.get("orderRef") or p.get("orderId") or p.get("id")
    if not ref:
        raise ValueError("응급우회 오더 식별(orderRef/orderId) 누락")
    row = await session.scalar(select(PreauthOrder).where(PreauthOrder.order_ref == str(ref)))
    if row is None:
        row = PreauthOrder(order_ref=str(ref))
        session.add(row)
    row.category = (p.get("category") or "UNCLASSIFIED")[:40]
    row.amount = round(float(p.get("amount") or p.get("estimatedAmount") or 0))
    row.dept_code = p.get("deptCode") or p.get("departmentCode")
    row.approver = (p.get("approver") or p.get("orderedBy") or "")[:80] or None
    row.approved_on = _d(p.get("bypassedAt") or p.get("at")) or date.today()
    row.order_type = (p.get("orderType") or p.get("clinicalType") or "")[:40] or None
    row.note = (p.get("reason") or p.get("note") or "응급 POST_FACTO 우회")[:300]
    row.channel = "EMERGENCY_BYPASS"
    row.post_approved = bool(p.get("postApproved", False))   # 사후승인 완료 통보면 True
    await session.flush()
    return {"preauth_order": str(ref), "category": row.category, "amount": float(row.amount),
            "order_type": row.order_type, "channel": row.channel,
            "post_approved": row.post_approved, "note": "응급우회 인지(규제·원가·감사·사후승인 미결)"}


async def _pharmacy_dispensed(session, p, event_type, ctx):
    """약품 조제완료 → 약제 재고 출고 미러(품목별, 음수 수량). FIFO와 분리(D-4)."""
    n = 0
    for it in p.get("items") or []:
        code = it.get("medicationCode")
        if not code:
            continue
        session.add(HisStockMirror(
            item_code=code, name=it.get("name"), movement_type="DISPENSE",
            qty=-float(it.get("dispensedQty", 0) or 0), ref=p.get("dispensingId"),
            source="PHARMACY", moved_on=date.today()))
        n += 1
    return {"dispensing": p.get("dispensingId"), "items": n}


async def _materials_stock(session, p, event_type, ctx):
    """HIS 재고 이벤트 → 미러 적재(ERP FIFO와 분리, D-4). 재고 대사·가시성. erpItemCode 매핑 보존."""
    code = p.get("erpItemCode") or p.get("code")
    if not code:
        raise ValueError("품목코드 없음")
    bal = p.get("balanceAfter")
    session.add(HisStockMirror(
        item_code=code, erp_item_code=p.get("erpItemCode"), name=p.get("name"),
        movement_type=p.get("type") or "ADJUST", qty=float(p.get("qty", 0) or 0),
        balance_after=float(bal) if bal is not None else None, ref=p.get("ref"),
        source="CSSD" if event_type == "stock.changed" else "INVENTORY", moved_on=date.today()))
    return {"item": code, "type": p.get("type"), "qty": p.get("qty"),
            "mapped": p.get("erpItemCode") is not None}


async def _voucher_vendor(session, p, event_type, ctx):
    """검진권 딜러 거래처 upsert (HIS vendor.synced/changed) — B-G1-A.

    dealerId 기준 scm.Supplier upsert(external_vendor_code 매칭). dealerType으로 세무 구분
    (DOMESTIC=원천징수·OVERSEAS=외화). status=TERMINATED면 거래처 비활성(is_active=False).
    해외 딜러는 business_no 없음(마이그 0070 nullable). 국내 taxId는 HIS 추가 예정(DB-1).
    """
    from erp.scm.models import Supplier
    dealer_id, name = p.get("dealerId"), p.get("name")
    if not dealer_id or not name:
        raise ValueError("dealerId/name 필수")
    row = await session.scalar(
        select(Supplier).where(Supplier.external_vendor_code == str(dealer_id)))
    if row is None:
        row = Supplier(external_vendor_code=str(dealer_id))
        session.add(row)
    row.name = name
    row.vendor_type = p.get("vendorType") or "VOUCHER_DEALER"
    row.dealer_type = p.get("dealerType")        # DOMESTIC|OVERSEAS (세무 구분 SoT)
    row.tier = p.get("tier")
    _cr = p.get("commissionRate")
    row.commission_rate = float(_cr) if _cr is not None else None
    row.country, row.currency = p.get("country"), p.get("currency")
    row.contact = p.get("email")
    # 컴플라이언스·계층 보존(0071) — 국내 유치업 등록번호는 법정 추적 대상
    row.license_number = p.get("licenseNumber")
    row.contract_ref = p.get("contractRef")
    row.parent_external_code = p.get("parentId")
    row.is_active = (p.get("status") != "TERMINATED")
    # 세무·송금 마스터(B2-a) — 민감정보는 AES-GCM 컬럼암호화(0078). 은행명만 평문.
    bank = p.get("bankInfo") if isinstance(p.get("bankInfo"), dict) else {}
    row.bank_name = bank.get("bank")
    if row.id is None:                       # aad는 행 PK 바인딩 → 신규 행은 id 확보 후 암호화
        await session.flush()
    from erp.common import crypto

    def _enc(col: str, v):
        return crypto.encrypt_column(str(v), aad=crypto.column_aad(col, row.id)) if v else None
    row.tax_id_enc = _enc("scm.suppliers.tax_id", p.get("taxId"))
    row.bank_account_enc = _enc("scm.suppliers.bank_account", bank.get("account"))
    row.bank_swift_enc = _enc("scm.suppliers.bank_swift", bank.get("swift"))
    return {"dealer_id": dealer_id, "dealer_type": row.dealer_type, "active": row.is_active}


_ACC_WITHHOLDING = "2150"   # 예수금(원천징수) — 정산 원천징수 분리(DB-1)


async def _resolve_posting_rule(session, line_type, dealer_type):
    """전기 규칙 조회 — 딜러유형 구체 매칭 우선, 없으면 전체(NULL) 규칙."""
    from erp.finance.models import VoucherPostingRule
    rows = (await session.execute(
        select(VoucherPostingRule).where(
            VoucherPostingRule.line_type == line_type,
            VoucherPostingRule.active.is_(True))
        .order_by(VoucherPostingRule.priority, VoucherPostingRule.id))).scalars().all()
    if dealer_type:
        for r in rows:
            if r.dealer_type == dealer_type:
                return r
    for r in rows:
        if r.dealer_type is None:
            return r
    return None     # 구체·NULL 둘 다 없으면 미설정(타 딜러유형 규칙 fuzzy 적용 금지 — DEAD 가시화)


async def _corp_default_gl(session, currency: str) -> str | None:
    """통화별 기본 법인계좌의 GL 코드 — 외화 자금 레그 라우팅용(원화 미혼입)."""
    from erp.finance.models import CorporateAccount
    return await session.scalar(select(CorporateAccount.gl_account_code).where(
        CorporateAccount.currency == (currency or "").upper(),
        CorporateAccount.is_default.is_(True),
        CorporateAccount.is_active.is_(True)))


async def _voucher_line(session, p, event_type, ctx):
    """검진권 거래/정산 회계 (B-G1-B/C) — 관리자 전기규칙(VoucherPostingRule) 기반.

    lineType[+dealerType]로 규칙 조회 → 차·대 전기. 수수료 방향(비용/수익)·정산 AP/AR 등
    기관·계약별 차이는 /admin 규칙으로 설정(하드코딩 아님). skip=무전표(resale 등),
    reverse_on_negative=음수금액 차대반전(clawback). 멱등=전표 source_ref(ctx 주입).
    외화 라인(KRW 환산 불가)은 보류=DEAD(settlement은 netPayableKrw라 통과).
    """
    # settlement 이벤트는 lineType 미동봉이어도 event_type으로 보강(누락 시 통째 DEAD 방지)
    lt = (p.get("lineType") or ("settlement" if event_type == "voucher.settled" else "")).lower()
    rule = await _resolve_posting_rule(session, lt, p.get("dealerType") or p.get("toDealerType"))
    if rule is None:
        raise NotImplementedError(f"전기규칙 미설정: voucher/{lt} — /admin 규칙 설정 필요")
    if rule.skip or not (rule.debit_account and rule.credit_account):
        return {"lineType": lt, "skipped": "무전표(규칙)"}

    amount_keys = [k.strip() for k in (rule.amount_source or "").split(",") if k.strip()]
    cur = (p.get("currency") or "KRW").upper()

    # 금액 — 첫 존재 키. *krw 접미 키(예: netPayableKrw)는 이미 환산된 KRW.
    amt = 0.0
    amt_is_krw = False
    for k in amount_keys:
        if p.get(k) is not None:
            amt = float(p[k])
            amt_is_krw = k.lower().endswith("krw")
            break

    # 외화 처리 — 원화로 섞지 않고 통화별 법인 외화계좌(GL)로 자금 레그 라우팅.
    fx_gl = krw_gl = None
    if cur != "KRW":
        # 법인계좌를 환율보다 먼저 확인 — 계좌 미설정을 환율 보류로 먼저 보이면, 운영자가 환율을 지정한 뒤에야
        # 계좌 문제가 드러나고 재처리가 지정을 지워 같은 환율을 다시 넣어야 했다(2026-10-10 2차 검토)
        fx_gl = await _corp_default_gl(session, cur)
        if fx_gl is None:                         # 외화계좌 미등록 → 설정 유도(원화 혼입 방지)
            raise NotImplementedError(
                f"외화({cur}) 법인계좌 미설정 — /finance-corporate-accounts 등록 필요")
        if not amt_is_krw:                        # 외화 금액 → 라인레벨 fxRate로 KRW 환산
            fx_rate, _ = _effective_fx_rate(p)
            if not fx_rate:                       # 환율 미동봉 — 보류(DEAD 보존)
                # ⚠ 문구 주의: ERP가 방식을 못 정한 게 아니라 **송신측이 fxRate를 안 보낸 것**이다.
                # 종전 "환율 확정 후"는 ERP 내부 미결로 오독돼 상대가 재송신하지 않고 대기했다.
                # ERP에는 시점 환율 테이블이 없다(forex_service도 환율을 입력받는다) → 폴백 불가.
                raise NotImplementedError(
                    f"외화({cur}) {lt} 보류 — payload에 `fxRate`(거래시점 환율) 미동봉. "
                    f"송신측이 fxRate를 포함해 재송신하면 자동 처리됩니다(또는 관리자 > 연동 인박스에서 "
                    f"근거 환율로 '환율 지정 후 반영').")
            amt = amt * fx_rate
        krw_gl = (await _corp_default_gl(session, "KRW")) or _ACC_CASH

    dr, cr = rule.debit_account, rule.credit_account
    if amt < 0 and rule.reverse_on_negative:      # clawback 등 음수 — 차대 반전
        dr, cr, amt = cr, dr, -amt
    amt = round(amt)
    if amt <= 0:
        return {"lineType": lt, "skipped": "금액 0"}

    def _route(acc):       # 외화: 원화 자금계좌(krw_gl) 레그를 통화별 외화계좌(fx_gl)로 치환
        return fx_gl if (fx_gl and acc == krw_gl) else acc

    raw_ref = ctx.get("source_ref") or f"voucher:{lt}:{p.get('serial') or p.get('allocationId') or ''}"
    ref = _bounded_ref(raw_ref)   # 인박스 ref는 최대 120자 — 전표 키 64자 상한(시나리오 S-A)
    on = _d(p.get("paidAt") or p.get("usedAt") or p.get("at") or p.get("occurredAt")) or date.today()
    lines = [VoucherLineDTO(_route(dr), "DEBIT", amt), VoucherLineDTO(_route(cr), "CREDIT", amt)]
    wh = 0
    if lt == "settlement":      # 원천징수(DB-1) — HIS 동봉값 우선, 없으면 시점 요율 재계산(3-line)
        wh, wh_acc = await _settlement_withholding(
            session, p, on, amt, p.get("dealerType") or p.get("toDealerType"))
        wh = max(0, min(wh, amt))   # 원천징수가 지급액 초과 불가(net 음수 라인 방지)
        if wh > 0:              # Dr 미지급 / Cr 예수금(원천징수, 원화) + Cr 자금(net, 외화면 외화계좌)
            lines = [VoucherLineDTO(_route(dr), "DEBIT", amt),
                     VoucherLineDTO(_route(cr), "CREDIT", amt - wh),
                     VoucherLineDTO(wh_acc, "CREDIT", wh)]
    memo = f"검진권 {lt} {p.get('distributorId') or p.get('serial') or ''}".strip()
    if cur != "KRW":            # 외화 추적 — 통화·환율·외화계좌 GL 표기
        memo = f"{memo} [{cur}@{_effective_fx_rate(p)[1] or 'netKrw'}→{fx_gl}]".strip()
    await _vouchers.create(
        session, source_module="his.voucher", source_ref=ref, voucher_on=on, lines=lines,
        memo=memo)
    if lt == "settlement":                            # DB-4 — 지급 회신 아웃박스 enqueue(멱등)
        await _enqueue_settlement_callback(session, p, ref, amt, wh, raw_ref=raw_ref)
    return {"lineType": lt, "dr": dr, "cr": cr, "amount": float(amt), "withholding": wh, "ref": ref}


async def _resolve_withholding_rate(session, on_date, dealer_type):
    """시점 적용 원천징수 요율 — 시행일 구간 내 활성 행. 딜러유형 구체 매칭 우선."""
    from sqlalchemy import or_

    from erp.finance.models import WithholdingRate
    rows = (await session.execute(
        select(WithholdingRate).where(
            WithholdingRate.active.is_(True),
            WithholdingRate.effective_from <= on_date,
            or_(WithholdingRate.effective_to.is_(None),
                WithholdingRate.effective_to >= on_date))
        .order_by(WithholdingRate.effective_from.desc(),
                  WithholdingRate.id.desc()))).scalars().all()    # 동률 시 결정성
    if dealer_type:
        for r in rows:
            if r.dealer_type == dealer_type:
                return r
    for r in rows:
        if r.dealer_type is None:
            return r
    return None


async def _settlement_withholding(session, p, on, amt, dealer_type):
    """정산 원천징수액·예수금 계정 — HIS 동봉(withholdingKrw/Tax) 우선, 없으면 시점 요율 재계산."""
    for k in ("withholdingKrw", "withholdingTax"):
        if p.get(k) is not None:
            return round(float(p[k])), _ACC_WITHHOLDING
    rate = await _resolve_withholding_rate(session, on, dealer_type)
    if rate is None:
        return 0, _ACC_WITHHOLDING                    # 요율 미설정 = 무원천징수(현행 2-line)
    return round(amt * float(rate.rate_pct) / 100), (rate.withholding_account or _ACC_WITHHOLDING)


async def _enqueue_settlement_callback(session, p, ref, amt, withholding=0, raw_ref=None):
    """정산 지급 회신(ERP→HIS settlement.paid) 아웃박스 적재 — event_id 멱등(재처리 무중복)."""
    from erp.finance.models import DealerSettlementCallback
    src = raw_ref or ref      # settlementId 폴백은 원 ref 끝 토큰(축약 해시가 아니라)
    sid = p.get("settlementId") or (src.rsplit(":", 1)[-1] if src else None)
    if not sid:
        return
    event_id = f"erp:settle-paid:{sid}"
    if await session.scalar(select(DealerSettlementCallback.id).where(
            DealerSettlementCallback.event_id == event_id)):
        return                                        # 이미 적재(재처리) — 무중복
    # 회신 환율은 HIS가 보낸 것만 — 관리자 지정 환율을 HIS 값처럼 되돌려 보내지 않는다(출처 혼동·2026-10-10 검토).
    # 지급액은 KRW(amount_paid_krw)로 이미 확정돼 회신된다.
    fx = p.get("fxRate")
    session.add(DealerSettlementCallback(
        event_id=event_id, settlement_id=str(sid), distributor_id=p.get("distributorId"),
        amount_paid_krw=amt, withholding_krw=withholding if withholding else None,
        fx_rate=float(fx) if fx is not None else None, erp_voucher_ref=ref))


# ERP 계약 documentType → 계약 엔티티. CONSENT 등 HIS 환자도메인 통지는 ERP 무관(무시).
_ERP_SIGN_ENTITY = {"ERP_MEDICAL_CONTRACT": "medical_contract",
                    "ERP_OUTSOURCING_CONTRACT": "outsourcing_contract"}


def _erp_contract_id(p) -> int | None:
    """ERP 봉투 개시 시 동봉한 ERP 계약 참조 해소. erpContractId(권장) 또는
    externalSignerRef='erpcontract:<entity>:<id>' 패턴."""
    cid = p.get("erpContractId")
    if cid is not None:
        try:
            return int(cid)
        except (TypeError, ValueError):
            return None
    ref = p.get("externalSignerRef") or ""
    if ref.startswith("erpcontract:"):
        tail = ref.rsplit(":", 1)[-1]
        return int(tail) if tail.isdigit() else None
    return None


async def _sign_completed(session, p, event_type, ctx):
    """계약 전자서명 완료 미러 (e-sign #6, HIS sign.completed) — HIS=서명 SoT, ERP는 미러.

    ERP는 **자기 계약(documentType ERP_*)** 봉투만 미러. CONSENT 등 HIS 환자도메인 통지는 무시(no-op).
    payload(HIS): documentType·signatureRequestId·status·completedAt·certSerial·auditCertUri·
    assuranceLevel·tsaToken·document{issueNo,url,expiresAt}. 멱등(인박스 event_id dedup).
    완료본(PDF) URL+토큰 회수→Attachment 미러는 아웃바운드 개시(링크 확정) 후속.
    """
    entity = _ERP_SIGN_ENTITY.get((p.get("documentType") or "").upper())
    if entity is None:
        return   # ERP 계약 외(CONSENT 등) — ERP 무관, 무시(APPLIED)
    from erp.hr.models import MedicalStaffContract
    from erp.scm.models import OutsourcingContract
    model = {"medical_contract": MedicalStaffContract,
             "outsourcing_contract": OutsourcingContract}[entity]
    cid = _erp_contract_id(p)
    if cid is None:
        raise ValueError("ERP 계약 서명 통지에 erpContractId/externalSignerRef 누락")
    row = await session.get(model, cid)
    if row is None:
        raise ValueError(f"계약 미존재: {entity}#{cid}")
    row.sign_envelope_id = p.get("signatureRequestId") or row.sign_envelope_id
    row.sign_status = (p.get("status") or "COMPLETED").upper()
    row.signed_at = _dt(p.get("completedAt")) or row.signed_at   # 안전 파싱(L-1·실패 시 기존값 유지)
    row.sign_cert_serial = p.get("certSerial") or row.sign_cert_serial
    row.sign_assurance = p.get("assuranceLevel") or row.sign_assurance
    doc = p.get("document") or {}
    # 추적 ref는 영구 식별자만 — auditCertUri(영구) 우선, 없으면 기존 issueNo 유지.
    # ⚠ doc.url(24h 단기 HMAC 토큰)은 저장/노출 금지(만료·토큰 유출 위험) — 완료본은 attachment에 보관.
    row.sign_doc_ref = p.get("auditCertUri") or row.sign_doc_ref
    # 완료본 회수(§2) — 인박스 트랜잭션·행잠금에 15s 네트워크 GET을 묶지 않도록 아웃박스에 enqueue만
    # (전체검토 2026-06-29 Tier2). 실제 회수는 job_esign_fetch가 별도 트랜잭션에서 수행. 의료진 계약만 미러.
    doc_url = doc.get("url")
    if doc_url and entity == "medical_contract" and not row.sign_doc_attachment_id:
        await _enqueue_esign_fetch(session, entity_type=entity, contract_id=cid,
                                   doc_url=doc_url, issue_no=doc.get("issueNo"))
    # 서명완료 → 발효 트리거(APPROVED→ACTIVE·발효일 도래 시) — 위수탁·의료 계약 공통.
    # 발효일 미도래는 배치/수동 발효 대기(정상). 그 외 ValidationError(승계 충돌 등)는 로그로 가시화.
    if row.sign_status == "COMPLETED" and row.status == "APPROVED":
        from erp.errors import ValidationError as _VE
        if entity == "outsourcing_contract":
            from erp.scm.contract_service import OutsourcingContractService
            svc = OutsourcingContractService()
        else:   # medical_contract
            from erp.hr.contract_service import MedicalContractService
            svc = MedicalContractService()
        try:
            await svc.activate(session, actor=None, contract_id=cid)
        except _VE as exc:
            logger.info("e-sign 발효 보류", extra={"ctx": {"entity": entity, "contract_id": cid,
                                                       "reason": str(exc)}})


async def _enqueue_esign_fetch(session, *, entity_type, contract_id, doc_url, issue_no) -> None:
    """완료본 회수 아웃박스 enqueue — (entity_type, contract_id) 멱등. 재완료 통지 시 실패건 재시도 허용."""
    from erp.integrations.models import EsignFetchOutbox
    existing = await session.scalar(select(EsignFetchOutbox).where(
        EsignFetchOutbox.entity_type == entity_type,
        EsignFetchOutbox.contract_id == contract_id))
    if existing is None:
        session.add(EsignFetchOutbox(entity_type=entity_type, contract_id=contract_id,
                                     doc_url=doc_url, issue_no=issue_no, status="PENDING"))
    elif existing.status in ("FAILED", "DEAD"):
        existing.doc_url, existing.issue_no = doc_url, issue_no
        existing.status, existing.retry_count, existing.next_retry_at, existing.error = \
            "PENDING", 0, None, None


# ── 입원 정산 3종 (HIS 요청 2026-07-23·마이그 0224) ─────────────────────────
_ACC_PATIENT_AR = "1131"      # 환자미수금 (receivable_service와 동일)
_ACC_INPATIENT_REVENUE = "4115"   # 입원수익 (외래 4110과 분리·0224 신설)


_INTERIM_METHOD_ACC = {"CASH": _ACC_CASH, "TRANSFER": _ACC_CASH, "CARD": _ACC_CARD_AR}
# 계좌이체는 1110 현금성자산(보통예금 별도 계정 없음 — 계좌별 구분은 자금계좌 화면 소관) · 카드는 1132 카드미수금
# (카드사별은 전표 적요·원장 줄의 cardCompany로 — 계정은 하나). HIS 요청서 2026-10-22 R1.


def _interim_method(m) -> str:
    u = str(m or "").upper()
    if u not in _INTERIM_METHOD_ACC:
        raise ValidationError(f"중간정산 결제 수단 미지원: {u or '(없음)'} — HIS 계약은 CASH|CARD|TRANSFER")
    return u


class _InterimOrderPending(RuntimeError):
    """순서 뒤바뀜(환불이 수납보다 먼저 도착) — FAILED로 두고 30분마다 재시도·재시도 횟수 미소모·수신 72시간 뒤 DEAD."""


async def _interim_voucher_lines(session, ref: str) -> dict | None:
    from erp.finance.models import Voucher, VoucherLine
    v = await session.scalar(select(Voucher).where(Voucher.source_module == "his.billing", Voucher.source_ref == ref))
    if v is None:
        return None
    out: dict = {}
    for ln in (await session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == v.id))).all():
        out[(ln.account_code, ln.side)] = out.get((ln.account_code, ln.side), 0) + round(float(ln.amount))
    return out


async def _interim_upsert(session, p, event_type=None) -> "object":
    """중간정산 원장 upsert — interim_billing_id 멱등(recorded/paid/voided 공용).

    상태는 되돌리지 않는다: PAID·VOID 회차에 늦게 온(재전송) GENERATED 본문이 수납액·상태를 0·GENERATED로 덮던 경로를
    막는다 — 회차 칸(기간·금액·근거)만 갱신.
    """
    from erp.patient.models import InterimBilling
    ib_id, chart_no = p.get("interimBillingId"), p.get("chartNo")
    if not ib_id or not chart_no:
        raise ValueError("interimBillingId/chartNo 필수")
    chart_no = await resolve_inbound_chart(session, str(chart_no), event_type=event_type)
    row = await session.scalar(select(InterimBilling).where(
        InterimBilling.interim_billing_id == str(ib_id)))
    if row is None:
        row = InterimBilling(interim_billing_id=str(ib_id), chart_no=str(chart_no)[:20])
        session.add(row)
    row.admission_id = str(p.get("admissionId") or "")[:64] or row.admission_id
    row.chart_no = str(chart_no)[:20]
    row.sequence_no = p.get("sequenceNo")
    row.period_start, row.period_end = _d(p.get("periodStart")), _d(p.get("periodEnd"))
    row.total_amount = float(p.get("totalAmount") or 0)
    row.insurance_amount = float(p.get("insuranceAmount") or 0)
    row.copay_amount = float(p.get("copayAmount") or 0)
    for col, key in (("days", "days"), ("daily_rate", "dailyRate"), ("copay_rate", "copayRate")):
        if p.get(key) is not None:
            setattr(row, col, p[key])
    if p.get("priceBasis"):
        row.price_basis = str(p["priceBasis"])[:20]
    incoming = (p.get("status") or "GENERATED").upper()
    if not (row.status in ("PAID", "VOID") and incoming == "GENERATED"):
        row.paid_amount = float(p.get("paidAmount") or 0)
        row.paid_at = _dt(p.get("paidAt"))
        row.status = incoming
    row.updated_at = datetime.now(KST)
    await session.flush()
    return row


async def _interim_recorded(session, p, event_type, ctx):
    """입원 중간정산 발생 — 원장 적재만(GL 없음).

    HIS 확정(2026-07-24·(가)안): 입원료 매출 정본은 billing.line_recorded(퇴원 시 전액 AR
    인식)로 일원화. interim_recorded는 매출·미수 인식하지 않는다(이중 인식 방지·강등).
    """
    row = await _interim_upsert(session, p, event_type)
    return {"interim_billing_id": row.interim_billing_id,
            "copay": round(float(p.get("copayAmount") or 0)), "gl_posted": False}


async def _interim_paid(session, p, event_type, ctx):
    """입원 중간정산 수납 — 원장 갱신(+게이트 시 현금흐름 GL: DR 수단별 현금성 / CR 1131).

    HIS 확정((가)안): interim_paid는 수납 현금흐름을 유지한다. 재원 중에는 상대계정 1131이
    일시 대변(선수금 성격)이 되고, **ReceivableService.accrue의 DR 1131**(월 단위 본인부담 발생)이
    상계한다. ⚠ 상계 주체는 `_billing_line`이 아니다 — 라인 핸들러는 GL을 전기하지 않고
    billing_lines 적재만 하며, 그것을 원천으로 patient 도메인 엔진이 월 단위로 발생 인식한다
    (게이트도 patient_accrual_enabled로 별개). 이 게이트만 켜면 상계 차변이 오지 않는다.
    ⚠ 중간정산 수납은 HIS 일마감(billing.settled)에 미포함(HIS 실측 확인)이라 이중 현금 없음 —
    일마감의 `summary.data.interimPayments`는 참고 칸이라 `_billing_settled`가 읽지 않는다(R4).
    게이트(his_interim_gl_enabled)는 입원 청구 실가동(stayBilling 플립) 준비 완료 후 ON.

    수단(HIS v4.116.0~·R1): `payments[]` 합계 = paidAmount 검증(불일치·미지원 수단 = DEAD) → 수단별 차변
    (CASH·TRANSFER 1110 / CARD 1132). `payments` 없는 과거·재전송분은 1110(현행)으로 받고 적요에 '수단 미기록'.
    """
    from erp.config import get_settings
    pays = p.get("payments")
    paid = round(float(p.get("paidAmount") or 0))
    by_acc: dict[str, int] = {}
    if pays is not None:
        if not isinstance(pays, list):
            raise ValidationError("payments는 배열이어야 합니다")
        for x in pays:
            if not isinstance(x, dict):
                raise ValidationError("payments 원소는 객체여야 합니다")
            acc = _INTERIM_METHOD_ACC[_interim_method(x.get("method"))]
            amt = round(float(x.get("amount") or 0))
            if amt <= 0:     # 환불은 interim_paid 원장 줄이 아니라 billing.interim_refunded로(R5 계약)
                raise ValidationError("중간정산 수납 원장 줄 금액은 0보다 커야 합니다(환불은 billing.interim_refunded)")
            by_acc[acc] = by_acc.get(acc, 0) + amt
        if sum(by_acc.values()) != paid:
            raise ValidationError(f"중간정산 수납 원장 줄 합계 {sum(by_acc.values())} ≠ paidAmount {paid} — HIS 확인 필요")
    from erp.patient.models import InterimBilling
    if await session.scalar(select(InterimBilling.status).where(
            InterimBilling.interim_billing_id == str(p.get("interimBillingId") or ""))) == "VOID":
        raise ValidationError("무효 회차에 수납 이벤트 — HIS 확인 필요(무효 뒤 재생성은 새 interimBillingId 계약)")
    row = await _interim_upsert(session, p, event_type)
    if pays is not None:
        row.payments = pays
    if p.get("paidBy"):
        row.paid_by = str(p["paidBy"])[:64]
    posted = False
    if get_settings().his_interim_gl_enabled and paid > 0:
        legs = by_acc or {_ACC_CASH: paid}
        ref = _bounded_ref("interim:paid", str(row.interim_billing_id))
        prior = await _interim_voucher_lines(session, ref)
        want = {**{(a, "DEBIT"): v for a, v in legs.items() if v}, (_ACC_PATIENT_AR, "CREDIT"): paid}
        if prior is not None and prior != want:
            # 재전송의 수단·금액이 이미 전기된 전표와 다르면 조용히 무시하지 않는다(전표 멱등이 새 줄을 버림·검토 MED)
            raise ValidationError(f"중간정산 수납 재전송이 기존 전표와 다름(기존 {sorted(prior.items())}) — 정정 계약 필요")
        cards = sorted({str(x.get("cardCompany")) for x in (pays or []) if x and x.get("cardCompany")})
        memo = (f"입원 중간정산 수납 {row.chart_no} #{row.sequence_no or ''}(재원 중 1131 선수 성격)"
                + ("" if pays is not None else " · 수단 미기록(HIS 2026-10-22 이전분)")
                + (f" · 카드사 {','.join(cards)}" if cards else ""))
        await _vouchers.create(
            session, source_module="his.billing",
            source_ref=ref,
            voucher_on=_kst_date(p.get("paidAt")) or datetime.now(KST).date(),   # KST 날짜(검토 A)
            lines=[*(VoucherLineDTO(acc, "DEBIT", amt) for acc, amt in sorted(legs.items()) if amt),
                   VoucherLineDTO(_ACC_PATIENT_AR, "CREDIT", paid)],
            memo=memo[:200])
        posted = True
    return {"interim_billing_id": row.interim_billing_id, "paid": paid, "gl_posted": posted,
            "methods": sorted(by_acc) if by_acc else None}


async def _interim_voided(session, p, event_type, ctx):
    """입원 중간정산 무효(HIS v4.116.0 신규·R3) — 원장 상태 VOID 표시만(발생이 무전표라 GL 없음).

    수납된 회차는 무효 불가(HIS 계약) — ERP가 수납을 받은 회차에 무효가 오면 DEAD로 드러낸다(환불 이벤트 몫).
    """
    from erp.patient.models import InterimBilling
    existing = await session.scalar(select(InterimBilling).where(
        InterimBilling.interim_billing_id == str(p.get("interimBillingId") or "")))
    if existing is not None and float(existing.paid_amount or 0) > 0:
        raise ValidationError("수납된 중간정산 회차에 무효 이벤트 — 환불(billing.interim_refunded)로 처리해야 합니다")
    row = await _interim_upsert(session, {**p, "status": "VOID", "paidAmount": 0, "paidAt": None}, event_type)
    row.voided_at = _dt(p.get("voidedAt")) or datetime.now(KST)
    row.void_reason = (str(p.get("voidReason") or "")[:200]) or None
    return {"interim_billing_id": row.interim_billing_id, "status": "VOID", "gl_posted": False}


async def _interim_refunded(session, p, event_type, ctx):
    """입원 중간정산 환불(R5 — ERP 제안 계약·HIS 구현 전) — 환불 원장 + 게이트 시 역분개 DR 1131 / CR 수단별.

    본문 = 회차 칸 + `refund: {refundId, amount, method, reason, refundedBy, refundedAt}`. 부분 환불 여러 건 허용 —
    누적 환불 ≤ 수납액(회차 행 잠금). refund_id 멱등(같은 금액·수단 재전송은 무시, 다르면 DEAD). 퇴원 전·후 같은 분개:
    퇴원 후 과납은 1131 대변 잔액이라 DR 1131이 그것을 없앤다. 수납 이벤트가 아직 안 온 회차면 수납을 기다린다(30분 간격·최대 72시간).
    역분개는 그 회차 수납 전표가 있을 때만(게이트 OFF 시절 수납은 환불 원장만).
    """
    from erp.config import get_settings
    from erp.patient.models import InterimBilling, InterimBillingRefund
    rf = p.get("refund") or {}
    rid, ib_id = str(rf.get("refundId") or ""), str(p.get("interimBillingId") or "")
    if not rid or not ib_id:
        raise ValidationError("refund.refundId/interimBillingId 필수")
    method = _interim_method(rf.get("method"))
    amount = round(float(rf.get("amount") or 0))
    if amount <= 0:
        raise ValidationError("환불 금액은 0보다 커야 합니다")
    dup = await session.scalar(select(InterimBillingRefund).where(InterimBillingRefund.refund_id == rid[:64]))
    if dup is not None:
        if round(float(dup.amount)) != amount or dup.method != method:
            raise ValidationError(f"같은 refundId의 금액·수단이 다름(기존 {round(float(dup.amount))} {dup.method})")
        return {"refund_id": rid, "skipped": "이미 반영"}
    # 행 잠금 — 다른 refundId 부분 환불 둘이 동시에 누적 한도를 통과하지 않게(검토 MED)
    row = await session.scalar(select(InterimBilling).where(InterimBilling.interim_billing_id == ib_id)
                               .with_for_update())
    if row is None or row.status == "GENERATED":
        raise _InterimOrderPending("수납 반영 전 회차의 환불 — billing.interim_paid 수신 뒤 자동 재시도")
    if row.status != "PAID":
        raise ValidationError(f"{row.status} 회차의 환불 — HIS 확인 필요")
    done = float(await session.scalar(select(func.coalesce(func.sum(InterimBillingRefund.amount), 0)).where(
        InterimBillingRefund.interim_billing_id == ib_id)) or 0)
    if done + amount > float(row.paid_amount or 0):
        raise ValidationError(f"누적 환불 {round(done + amount)} > 수납액 {round(float(row.paid_amount or 0))}")
    session.add(InterimBillingRefund(
        refund_id=rid[:64], interim_billing_id=ib_id, chart_no=row.chart_no, amount=amount, method=method,
        reason=(str(rf.get("reason") or "")[:200]) or None, refunded_by=(str(rf.get("refundedBy") or "")[:64]) or None,
        refunded_at=_dt(rf.get("refundedAt"))))
    await session.flush()
    posted = False
    # 수납 전표가 있을 때만 역분개 — 게이트 OFF 시절 수납(전표 없음)의 환불이 1131·현금을 깨지 않게(검토 MED)
    if get_settings().his_interim_gl_enabled and await _interim_voucher_lines(
            session, _bounded_ref("interim:paid", ib_id)) is not None:
        await _vouchers.create(
            session, source_module="his.billing", source_ref=_bounded_ref("interim:refund", rid),
            voucher_on=_kst_date(rf.get("refundedAt")) or datetime.now(KST).date(),
            lines=[VoucherLineDTO(_ACC_PATIENT_AR, "DEBIT", amount),
                   VoucherLineDTO(_INTERIM_METHOD_ACC[method], "CREDIT", amount)],
            memo=f"입원 중간정산 환불 {row.chart_no} #{row.sequence_no or ''} ({method})"[:200])
        posted = True
    return {"refund_id": rid, "amount": amount, "gl_posted": posted}


async def _discharge_settlement(session, p, event_type, ctx):
    """퇴원 최종 정산 대사 스냅샷 — admission_id upsert(값-멱등 재발행=대체). GL 없음.

    reconciliation.warnings(과납/미납)는 ADMIN 알림으로 가시화(대사 실패 무음 방지).
    """
    from erp.common.models import Notification, User
    from erp.patient.models import InpatientSettlement
    adm_id, chart_no = p.get("admissionId"), p.get("chartNo")
    if not adm_id or not chart_no:
        raise ValueError("admissionId/chartNo 필수")
    chart_no = await resolve_inbound_chart(session, str(chart_no), event_type=event_type)
    row = await session.scalar(select(InpatientSettlement).where(
        InpatientSettlement.admission_id == str(adm_id)))
    if row is None:
        row = InpatientSettlement(admission_id=str(adm_id)[:64], chart_no=str(chart_no)[:20])
        session.add(row)
    rec = p.get("reconciliation") or {}
    row.chart_no = str(chart_no)[:20]
    row.admit_date, row.discharge_date = _d(p.get("admitDate")), _d(p.get("dischargeDate"))
    row.days = p.get("days")
    row.total_charges = float(p.get("totalCharges") or 0)
    row.total_copay = float(p.get("totalCopay") or 0)
    row.already_paid = float(p.get("alreadyPaid") or 0)
    row.balance_due = float(p.get("balanceDue") or 0)
    row.balanced = bool(rec.get("balanced", True))
    row.overpaid = bool(rec.get("overpaid", False))
    row.overpaid_amount = float(rec.get("overpaidAmount") or 0)
    row.unpaid_interims = int(rec.get("unpaidInterims") or 0)
    row.warnings = rec.get("warnings") or []
    row.breakdown = p.get("breakdown")
    row.exported_at = _dt(p.get("exportedAt"))
    row.updated_at = datetime.now(KST)
    await session.flush()
    if row.warnings or row.overpaid or not row.balanced:
        body = (f"퇴원 정산 경고 {row.chart_no} — 잔액 {int(row.balance_due):,}원"
                f"{'·과납 ' + format(int(row.overpaid_amount), ',') + '원' if row.overpaid else ''}"
                f"{'·미수납 중간정산 ' + str(row.unpaid_interims) + '건' if row.unpaid_interims else ''}")
        admin_ids = (await session.execute(select(User.id).where(
            User.role == "ADMIN", User.is_active.is_(True)))).scalars().all()
        for uid in admin_ids:   # 인박스 소비 트랜잭션 내 — 실패 시 이벤트 재시도와 함께 원자적
            session.add(Notification(user_id=uid, type="INPATIENT_SETTLEMENT",
                                     title="퇴원 정산 대사 경고", body=body[:500]))
    return {"admission_id": row.admission_id, "balance_due": float(row.balance_due),
            "warnings": len(row.warnings or [])}


async def _lis_billing(session, p, event_type, ctx):
    """LIS 검사 청구 캡처 — E-3(A) LIS 수량·ERP 산정. testCode→EDI 매핑·fee lookup·멱등(revision)."""
    from erp.integrations.lis_billing_service import LisBillingService
    return await LisBillingService().capture(session, p, event_type=event_type)


HANDLERS = {
    ("assets", "equipment.acquired"): _equipment_acquired,
    ("assets", "equipment.status_changed"): _equipment_status_changed,
    ("assets", "equipment.maintenance"): _equipment_maintenance,
    ("materials", "stock.changed"): _materials_stock,
    ("materials", "inventory.changed"): _materials_stock,
    ("materials", "pharmacy.dispensed"): _pharmacy_dispensed,
    ("hr", "staff.created"): _staff,
    ("hr", "staff.changed"): _staff,
    ("hr", "staff.resigned"): _staff,
    ("hr", "staff.schedule_changed"): _staff_schedule,
    ("hr", "staff.qualification_changed"): _staff_qualification,
    ("hr", "staff.attendance_recorded"): _staff_attendance,
    ("finance", "vendor.synced"): _voucher_vendor,
    ("finance", "vendor.changed"): _voucher_vendor,
    ("finance", "voucher.line_recorded"): _voucher_line,
    ("finance", "voucher.settled"): _voucher_line,   # payload.lineType='settlement' 규칙 적용
    ("finance", "billing.line_recorded"): _billing_line,
    ("finance", "billing.settled"): _billing_settled,
    ("finance", "billing.adjusted"): _billing_adjusted,
    ("finance", "billing.interim_recorded"): _interim_recorded,   # 입원 중간정산 발생(0224)
    ("finance", "billing.interim_paid"): _interim_paid,           # 입원 중간정산 수납(0224·수단 0266)
    ("finance", "billing.interim_voided"): _interim_voided,       # 입원 중간정산 무효(HIS v4.116.0·0266)
    ("finance", "billing.interim_refunded"): _interim_refunded,   # 입원 중간정산 환불(ERP 제안 계약·0266)
    ("finance", "billing.discharge_settlement"): _discharge_settlement,  # 퇴원 정산 대사(0224)
    ("finance", "claim.submitted"): _claim_submitted,
    ("finance", "claim.reviewed"): _claim_reviewed,
    ("finance", "cost.period_closed"): _cost_period_closed,   # 이벤트 카탈로그 v1 신규(정보성)
    ("finance", "certificate.issued"): _certificate_issued,   # 제증명수입 4120(구·호환)
    ("finance", "eapproval.document_issued"): _certificate_issued,   # 제증명 발급수수료 유일 연계경로(필수)
    ("finance", "eapproval.document_adjusted"): _document_adjusted,  # 발급취소/환불 → 4120 역분개
    ("procurement", "preauth.order_approved"): _preauth_order_approved,  # 고가오더 관리회계(GL 미전기)
    ("procurement", "preauth.emergency_bypass"): _preauth_emergency_bypass,  # 응급우회 인지(규제·감사)
    ("procurement", "cssd.po.approved"): _cssd_po_approved,
    ("sign", "sign.completed"): _sign_completed,
    ("lis", "lis.billing"): _lis_billing,   # LIS 검사 청구 캡처(E-3 A·산정 ERP)
}


async def _notify_inbound_dead(session, dead: list) -> None:
    """인박스 DEAD 관리자 알림(배치당 1건) — 종전엔 화면을 직접 열기 전엔 아무도 몰랐다(검토 F·prod DEAD 13건).
    본문엔 이벤트 유형·id만(페이로드 값 없음)."""
    from erp.common.models import Notification, User
    kinds: dict[str, int] = {}
    for ev in dead:
        kinds[ev.event_type] = kinds.get(ev.event_type, 0) + 1
    summary = ", ".join(f"{k} {v}건" for k, v in sorted(kinds.items()))
    for uid in (await session.scalars(select(User.id).where(
            User.role == "ADMIN", User.is_active.is_(True)))).all():
        session.add(Notification(
            user_id=uid, type="INBOUND_DEAD", title=f"HIS 연동 이벤트 처리 실패(DEAD) {len(dead)}건",
            body=f"{summary} — 연동 관리 화면에서 사유 확인 후 재처리·HIS 문의"))


class InboundConsumer:
    MAX_RETRY = 5   # 일시실패 재처리 한도 — 초과 시 DEAD(가시화)

    async def consume(self, session: AsyncSession, *, limit: int = 200) -> dict:
        from datetime import timedelta, timezone

        from sqlalchemy import and_, or_
        now = datetime.now(timezone.utc)
        # NEW + 재시도 도래한 FAILED(일시실패) 함께 소비 — 영구 정체 방지(P0-9).
        # FOR UPDATE SKIP LOCKED — 실시간 리스너+폴링 동시 호출/멀티워커 시 행 중복소비 차단(P1-7).
        # (SQLite 테스트는 행잠금 미지원이라 무시됨 — 로직 동일)
        events = (await session.execute(
            select(HisInboundEvent).where(or_(
                HisInboundEvent.status == "NEW",
                and_(HisInboundEvent.status == "FAILED",
                     or_(HisInboundEvent.next_retry_at.is_(None),
                         HisInboundEvent.next_retry_at <= now))))
            .order_by(HisInboundEvent.id).limit(limit)
            .with_for_update(skip_locked=True))).scalars().all()
        ctx: dict = {}
        applied = failed = 0
        prereview_targets: set[tuple[str, str]] = set()   # (chart_no, care_month) — 이벤트 기반 재평가
        dead: list = []
        for ev in events:
            handler = HANDLERS.get((ev.domain, ev.event_type))
            # ctx는 배치 공유(부서 캐시 겸용) — 실패한 이벤트의 savepoint가 롤백돼도 그 안에서 캐시에 넣은
            # 부서 id가 남아, 같은 배치의 다음 이벤트가 존재하지 않는 부서 id로 직원을 만들었다(검토 F).
            snap = dict(ctx)
            try:
                if handler is None:
                    raise NotImplementedError(
                        f"handler 미구현(Wave 후속): {ev.domain}/{ev.event_type}")
                ctx["source_ref"] = ev.source_ref        # 핸들러 멱등키(전표 source_ref용)
                ctx["event_id"] = ev.id                  # 수신 순서 판정(낡은 재심사 재생 차단·3회차)
                async with session.begin_nested():       # 건별 savepoint 격리
                    result = await handler(session, ev.payload or {}, ev.event_type, ctx)
                if isinstance(result, dict) and result.get("_prereview"):
                    prereview_targets.add(result["_prereview"])
                ev.status, ev.error, ev.next_retry_at = "APPLIED", None, None
                applied += 1
                snap = None
            except NotImplementedError as exc:           # 핸들러 미구현 — 영구 실패(재시도 무의미)
                ev.status, ev.error = "DEAD", str(exc)[:300]
                failed += 1
            except ValidationError as exc:                # 마감기간·차대불균형 등 영구 실패 —
                # 재시도해도 자기치유 불가. 5회(75분) 재시도 후 DEAD 대신 즉시 DEAD로 가시화
                # (마감월 지각 이벤트가 조용히 매몰되던 것 방지 — 역분개/개방월 처리 유도).
                ev.status, ev.error, ev.next_retry_at = "DEAD", str(exc)[:300], None
                failed += 1
                logger.warning("inbound 영구실패(즉시 DEAD)", extra={"ctx": {
                    "id": ev.id, "evt": ev.event_type, "err": str(exc)[:120]}})
            except ClaimLockBusy as exc:                 # 다른 워커가 같은 청구 처리 중 — 짧은 백오프 재시도
                # 2026-10-09 시나리오 3회차: 일시 경합이라 retry_count를 늘리지 않는다(경합만으로 DEAD 매몰 방지).
                ev.status, ev.error = "FAILED", str(exc)[:300]
                ev.next_retry_at = now + timedelta(minutes=1)
                failed += 1
                logger.info("inbound 청구 잠금 경합 — 재시도", extra={"ctx": {
                    "id": ev.id, "evt": ev.event_type}})
            except _InterimOrderPending as exc:          # 환불이 수납보다 먼저 도착 — 수납을 기다린다
                # HIS 답신(2026-10-22): HIS엔 '수납 전송 성공 뒤 환불 발신' 장치가 없어 수납이 재시도 대기 중이면 환불이 먼저
                # 온다. 일반 재시도(5회·약 50분)로는 수납이 그보다 밀리면 DEAD → 운영자 재큐였다. 재시도 횟수를 쓰지 않고
                # 30분 간격으로 기다리되, 수신 72시간이 지나도 수납이 없으면 DEAD(운영자 확인).
                received = as_kst_wallclock(ev.received_at)        # 두 백엔드 모두 KST 벽시계로 비교(Phase0 규율)
                if received is not None and as_kst_wallclock(now) - received > timedelta(hours=72):
                    ev.status, ev.error, ev.next_retry_at = "DEAD", f"72시간 대기 후에도 {str(exc)[:240]}", None
                    failed += 1
                    logger.warning("inbound 순서 대기 한도 초과(DEAD)", extra={"ctx": {
                        "id": ev.id, "evt": ev.event_type}})
                else:
                    ev.status, ev.error = "FAILED", str(exc)[:300]
                    ev.next_retry_at = now + timedelta(minutes=30)
                    failed += 1
                    logger.info("inbound 수납 대기(환불 선도착) — 30분 뒤 재시도", extra={"ctx": {
                        "id": ev.id, "evt": ev.event_type}})
            except PendingRefError as exc:                # 미등록 patientRef(신규환자) — 보류·재시도
                # 핸들러 savepoint는 롤백됨 → 보류 큐 적재는 외곽(본 세션)에서 수행해 배치와 함께 커밋.
                # 증분 대사표 이관 후 재시도 시 자기치유. DEAD로 매몰하지 않도록 retry_count 무증가.
                await record_pending(session, exc.patient_ref, event_type=ev.event_type)
                ev.status = "FAILED"
                ev.error = str(exc)[:300]
                ev.next_retry_at = now + timedelta(hours=6)   # 증분 대사표 도착 주기 고려(긴 백오프)
                failed += 1
                logger.info("미등록 patientRef 보류", extra={"ctx": {
                    "id": ev.id, "evt": ev.event_type}})
            except Exception as exc:  # noqa: BLE001 — 일시실패 격리·재시도
                ev.retry_count += 1
                ev.error = str(exc)[:300]
                if ev.retry_count >= self.MAX_RETRY:      # 한도 초과 → DEAD(운영자 확인)
                    ev.status, ev.next_retry_at = "DEAD", None
                else:                                     # 선형 백오프 재시도
                    ev.status = "FAILED"
                    ev.next_retry_at = now + timedelta(minutes=5 * ev.retry_count)
                failed += 1
                logger.warning("inbound 적용 실패",
                               extra={"ctx": {"id": ev.id, "evt": ev.event_type,
                                              "retry": ev.retry_count,
                                              "err": str(exc)[:120]}})  # 절단(페이로드 값 로그 방지)
            if snap is not None:                      # 실패 — 이 이벤트가 오염시킨 캐시 원복
                ctx.clear()
                ctx.update(snap)
            if ev.status == "DEAD":
                dead.append(ev)
        if dead:
            await _notify_inbound_dead(session, dead)
        return {"applied": applied, "failed": failed, "processed": len(events),
                # limit 도달 = 더 처리할 이벤트가 남았을 수 있음(다음 호출에서 소비) — 무음 캡 방지.
                "truncated": len(events) == limit,
                "prereview_targets": sorted(prereview_targets)}
