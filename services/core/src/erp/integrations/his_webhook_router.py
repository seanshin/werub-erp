"""HIS 아웃바운드 웹훅 수신 게이트웨이 (마스터플랜 Wave 대비 골격).

X-Integration-Key 검증 → integration.his_inbound_events 멱등 적재(source_ref UNIQUE) → 200.
도메인별 consumer는 Wave 회신(payload 스키마 확정) 후 추가. 현재는 수신·격리만.
"""
import hashlib
import hmac
import logging
import re
import time
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.policy import require_scope
from erp.common.router import CurrentUser, get_current_user
from erp.config import get_settings
from erp.db import get_session
from erp.errors import ForbiddenError, ValidationError
from erp.integrations.models import HisInboundEvent

logger = logging.getLogger("erp.integrations.his_webhook")
router = APIRouter(prefix="/integrations/his", tags=["integrations"])


def _parse_ts_epoch(ts: str) -> float | None:
    """X-Timestamp → epoch초. unix epoch(권장) 또는 ISO8601(S-2 송신 형식) 수용."""
    try:
        return float(ts)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def inbound_signature_reason(raw: bytes, signature: str | None, timestamp: str | None,
                             secret: str | None, *, now_epoch: float,
                             max_skew: int) -> str | None:
    """인박스 본문 HMAC+ts 검증 (S-2 동형: signed = X-Timestamp + "." + rawBody).

    통과 시 None, 실패 시 사유. 서명 입력은 X-Timestamp 헤더 문자열 verbatim(바이트 일치),
    신선도는 파싱 epoch 기준. 서명 형식은 'sha256=<hex>'·bare '<hex>' 모두 수용(HIS 발신은
    접두 없는 hex). 상수시간 비교.
    """
    if not secret:
        return "시크릿 미설정"
    if not signature or not timestamp:
        return "서명/타임스탬프 누락"
    ts_epoch = _parse_ts_epoch(timestamp)
    if ts_epoch is None:
        return "타임스탬프 형식 오류"
    if abs(now_epoch - ts_epoch) > max_skew:
        return "신선도 창 초과(재생 의심)"
    signed = timestamp.encode("utf-8") + b"." + raw
    expected_hex = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    # 포맷 호환 — 'sha256=' 접두 유무 모두 수용(HIS 발신부는 접두 없는 hex, S-2는 접두 포함)
    got = signature[7:] if signature.startswith("sha256=") else signature
    if not hmac.compare_digest(expected_hex, got.strip()):
        return "서명 불일치"
    return None


async def _inbound_nonce_fresh(signature: str, *, window: int) -> bool:
    """서명 단일소비(재생 방지 심화) — Redis NX 1회. eapproval `_assert_callback_nonce` 동형.

    신선도 창(max_skew) 내 동일 서명 재전송을 차단(특히 _UPDATABLE_EVENTS 재처리로 상태를 되돌리는
    창내 재생). 서명은 이미 본문+ts의 HMAC이라 재생은 동일 서명 → NX 실패. redis 불가 시 True(가용성
    우선 — ts 신선도가 하드 상한). 반환 True=처음 본 서명(통과), False=재생.
    """
    from erp.common.redis_client import get_redis
    key = "inbound:sig:" + hashlib.sha256(signature.encode()).hexdigest()
    try:
        fresh = await get_redis().set(key, "1", nx=True, ex=window + 60)
    except Exception as exc:  # noqa: BLE001 — redis 장애 시 수신 차단하지 않음(가용성)
        logger.warning("인박스 nonce 검사 생략(redis 불가)", extra={"ctx": {"err": str(exc)[:120]}})
        return True
    return bool(fresh)


_LIST_STATUSES = ("NEW", "APPLIED", "FAILED", "DEAD", "RESOLVED")

# 상세 화면 마스킹 — 환자·인적 식별자·토큰은 값 대신 길이만(조치 판단엔 키 존재·형태면 충분)
# 상세 화면 마스킹 — **허용 목록(fail-closed)**. 판단에 필요한 값(금액·통화·유형·상태·일자·업무 엔티티 참조)만 보이고
# 나머지 값은 전부 길이만 보인다(키 이름은 보인다). 2026-10-10 검토에서 차단 목록을 네 번 넓혔는데 매번 새 누락
# (소문자 붙여쓰기·국내 약어·보험·보호자…)이 나왔다 — 모르는 키를 보여 주는 쪽이 아니라 가리는 쪽으로 실패하게 한다.
_SAFE_TOKENS = frozenset({
    "amount", "amt", "price", "rate", "currency", "qty", "quantity", "count", "total", "sum", "net",
    "gross", "fee", "vat", "krw", "usd", "type", "status", "kind", "category", "mode", "side", "code",
    "line", "revision", "rev", "version", "seq", "serial", "settlement", "distributor", "allocation",
    "dealer", "supplier", "claim", "voucher", "contract", "order", "source", "event", "at", "on",
    "date", "time", "month", "year", "period", "paid", "approved", "reduced", "claimed", "decided",
    "commission", "refund", "purchase", "from", "to", "account", "accounts", "payment", "withholding",
    "billing", "lis", "his", "erp", "document", "level", "assurance", "completed", "expires", "pages",
    "fx", "payable", "supply", "debit", "credit", "care", "performed", "item", "items", "test",
    "signature", "request", "unit", "sub", "is", "has", "of", "per", "start", "end", "due",
    "reduction", "approval", "review", "reviewed", "submitted", "decision",
    # 단독으로는 뜻이 없는 참조 접미사 — 다른 토큰이 전부 허용일 때만 의미가 있다(claimId·lisBillingId)
    # `ref`는 넣지 않는다 — 송신측 참조는 차트번호를 품을 수 있다(목록에 source_ref가 이미 보임·6차 검토)
    "id", "no", "number", "num", "key"})
# 허용 단어가 섞여 있어도 가리는 쪽이 이긴다(예: patientType·dealerName·accountNo·diagCode)
_SENSITIVE_TOKENS = frozenset({
    "name", "nm", "patient", "pt", "pat", "chart", "phone", "mobile", "tel", "hp", "birth", "dob",
    "rrn", "ssn", "resident", "jumin", "email", "addr", "address", "token", "cert", "card", "iban",
    "signer", "license", "visit", "admission", "encounter", "mrn", "pid", "user", "staff", "emp",
    "employee", "diag", "diagnosis", "dx", "insurance", "insured", "policy", "guardian", "caregiver",
    "note", "notes", "memo", "remark", "remarks", "comment", "desc", "description", "reason", "text",
    "symptom", "finding", "findings", "sex", "gender", "age"})
_SUFFIX_TOKENS = frozenset({"id", "no", "number", "num", "key"})
# 값이 문자열이면 가리는 키(마지막 토큰 기준) — 하위가 dict일 때만 펼쳐 본다(`document`: 문자열이면 파일명·제목)
_CONTAINER_ONLY = frozenset({"document", "documents", "items", "item", "list"})
_ERP_CONTRACT_REF = re.compile(r"erpcontract:[a-z_]+:\d+")
_SENSITIVE_STEMS = ("patient", "chartno", "chartid", "birth", "phone", "rrn", "resident", "jumin",
                    "mrn", "email", "taxid", "accountno", "accountnumber")
_KEY_TOKEN = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def _value_visible(key: str) -> bool:
    """키의 **모든 토큰**이 허용 목록에 있을 때만 값을 보인다 — 모르는 단어가 하나라도 있으면 가린다.

    '허용 단어가 하나라도 있으면 보임'은 accountHolder·contractParty·documentTitle·kcdCode처럼 허용 단어에 인적·
    진단 단어가 붙은 키를 통과시켰다(5차 검토). 예외는 사유 **코드**(reductionReasonCode) — 자유 기술이 아니라 코드라서.
    """
    toks = [t.lower() for t in _KEY_TOKEN.findall(key)]
    joined = key.lower().replace("_", "").replace("-", "")
    if any(st in joined for st in _SENSITIVE_STEMS):
        return False
    if joined.endswith("reasoncode"):
        toks = [t for t in toks if t != "reason"]
    if not toks or any(t in _SENSITIVE_TOKENS for t in toks):
        return False
    # 접미사(id·no·ref…)만으로 된 키(bare `id`)는 무엇의 id인지 몰라 가린다 — 환자 도메인 이벤트의 최상위 id일 수 있다
    return all(t in _SAFE_TOKENS for t in toks) and any(t not in _SUFFIX_TOKENS for t in toks)


# 참조의 **토큰 단위** 일치만 — 부분 문자열이면 UUID('3fa2e2e1-…')의 'e2e' 같은 우연 일치로 실제 거래가
# '테스트 추정'에 잡혀 일괄 종결에 선택될 수 있다(2026-10-10 검토).
_TEST_REF = re.compile(
    r"(?:^|[:_\-])(?:e2e|probe|smoke|test|erptest|tzfix|realutc)(?=[:_\-]|$)", re.I)


def _mask_payload(v, key: str = "", depth: int = 0, in_list: bool = False):
    # ERP가 남긴 운영자 조치 기록(환율·확인 사유)은 판단 근거라 그대로 — 최상위만(송신측 _erp* 제거도 최상위라서,
    # 하위의 _erp* 키는 송신측이 넣은 것일 수 있다)
    if depth == 1 and key.startswith("_erp"):
        return v
    if isinstance(v, dict):
        if key and not _value_visible(key) and any(t in _SENSITIVE_TOKENS for t in
                                                   (x.lower() for x in _KEY_TOKEN.findall(key))):
            return f"***(dict {len(v)}개)"     # {"patient": {...}} — 민감 키 아래는 하위 키 이름도 숨김
        return {k: _mask_payload(val, str(k), depth + 1) for k, val in v.items()}
    if isinstance(v, list):
        return [_mask_payload(x, key, depth + 1, in_list=True) for x in v[:50]]
    if v is None or v == "":
        return v
    # 목록 안의 스칼라는 무엇인지 키로 알 수 없다(lineItems: ["홍길동"]) — 항상 가린다(7차 검토). dict 원소는 펼친다.
    if in_list:
        return f"***({len(str(v))}자)"
    if depth == 1 and key == "externalSignerRef" and _ERP_CONTRACT_REF.fullmatch(str(v)):
        return v                          # ERP 계약 연결(erpcontract:<entity>:<id>) — 내부 식별자라 판단 근거
    toks = [t.lower() for t in _KEY_TOKEN.findall(key)] if key else []
    if not key or (toks and toks[-1] in _CONTAINER_ONLY) or not _value_visible(key):
        return f"***({len(str(v))}자)"
    if isinstance(v, str) and len(v) > 200:
        return v[:200] + "…"
    return v


def inbound_action_hint(event_type: str, source_ref: str, error: str | None) -> dict:
    """정체 이벤트의 조치 안내 — 화면이 '무엇을 누를지'를 오류 문구 해석 없이 보여주게(2026-10-10).

    kind는 화면의 버튼 노출에도 쓰인다(RESTORE_HOLD=확인 후 반영·FX_HOLD=환율 지정 후 반영). 나머지는 안내뿐 —
    TEST는 참조 이름 기반 **추정**이라 문구도 '보입니다'로 둔다(판단은 운영자).
    """
    from erp.integrations.inbound_consumer import FX_HOLD_RE, RESTORE_HOLD_PREFIX
    e = error or ""
    et = event_type or ""
    if RESTORE_HOLD_PREFIX in e:
        return {"kind": "RESTORE_HOLD", "text": "심사팀이 이의신청 결과와 대조 — 아직 반영되지 않았으면 '확인 후 반영', "
                "이미 반영됐으면 '처리 완료'"}
    if et.startswith("voucher.") and FX_HOLD_RE.search(e):
        return {"kind": "FX_HOLD", "text": "거래일 환율 근거를 확인해 '환율 지정 후 반영' — 또는 HIS에 fxRate를 넣은 "
                "재송신을 요청"}
    if et.startswith("test.") or _TEST_REF.search(source_ref or ""):
        return {"kind": "TEST", "text": "연동 점검·테스트 이벤트로 보입니다 — 실제 거래가 아니면 '처리 완료'(여러 건 "
                "선택해 한 번에 종결 가능)"}
    if "미설정" in e:
        return {"kind": "ERP_SETUP", "text": "ERP 설정(전기 규칙·법인계좌 등)을 오류 내용대로 등록한 뒤 '재처리'"}
    if "직원 미상" in e:
        return {"kind": "STAFF_UNKNOWN", "text": "해당 직원이 ERP에 아직 없습니다 — HIS 직원 동기화(직원 생성 이벤트) "
                "뒤 '재처리', 존재하지 않는 직원이면 '처리 완료'"}
    if "미구현" in e:
        return {"kind": "UNSUPPORTED", "text": "ERP가 처리하지 않는 이벤트 유형입니다 — '처리 완료'로 종결"}
    if any(k in e for k in ("없음", "누락", "필수")):
        return {"kind": "SENDER_FIX", "text": "HIS가 보낸 값이 빠져 있습니다 — HIS에 보완 재송신을 요청하고, 재송신되지 "
                "않을 건이면 '처리 완료'"}
    return {"kind": "OTHER", "text": "오류 내용대로 원인을 해소한 뒤 '재처리'"}


def _event_row(e: HisInboundEvent) -> dict:
    return {"id": e.id, "domain": e.domain, "event_type": e.event_type,
            "source_ref": e.source_ref, "status": e.status, "retry_count": e.retry_count,
            "next_retry_at": e.next_retry_at, "error": e.error, "received_at": e.received_at,
            "resolved_by": e.resolved_by, "resolved_at": e.resolved_at,
            "resolve_reason": e.resolve_reason,
            "hint": (inbound_action_hint(e.event_type, e.source_ref, e.error)
                     if e.status in ("FAILED", "DEAD") else None)}


@router.get("/inbound")
async def list_inbound_events(status: str = "FAILED", limit: int = 100,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """인박스 이벤트 조회(운영 가시성) — FAILED/DEAD 정체건 확인·조치, RESOLVED(운영자 종결) 이력. ADMIN."""
    require_scope(user, "admin.staging", "read")
    if status not in _LIST_STATUSES:
        raise ValidationError(f"조회 가능한 상태: {', '.join(_LIST_STATUSES)}(요청 {status})")
    rows = (await session.execute(
        select(HisInboundEvent).where(HisInboundEvent.status == status)
        .order_by(HisInboundEvent.id.desc()).limit(min(limit, 500)))).scalars().all()
    return {"count": len(rows), "items": [_event_row(e) for e in rows]}


@router.get("/inbound/{event_id}")
async def get_inbound_event(event_id: int, user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """이벤트 상세 — 조치 판단용 payload(식별자 마스킹)·조치 안내. ADMIN read.

    종전 화면은 참조·오류만 보여 '테스트 건인지·무슨 거래인지'를 판단할 근거가 없었다(2026-10-10 prod DEAD 13건)."""
    require_scope(user, "admin.staging", "read")
    ev = await session.get(HisInboundEvent, event_id)
    if ev is None:
        raise ValidationError("존재하지 않는 이벤트입니다")
    # 마스킹해도 환자 연결 이벤트의 내용 열람이다 — 접속기록(개인정보 안전성 확보조치 고시 §8)
    from erp.audit.service import AuditService
    await AuditService().record_access(session, actor=user.user_id, action="READ",
                                       entity="integration.his_inbound_events",
                                       target_id=str(ev.id), route="/integrations/his/inbound/{id}")
    return {**_event_row(ev), "payload": _mask_payload(ev.payload or {})}


class ResolveIn(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


@router.post("/inbound/{event_id}/resolve")
async def resolve_inbound_event(event_id: int, body: ResolveIn,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """DEAD 이벤트 운영자 종결(처리 완료) — status→RESOLVED·error 보존·누가/언제/왜 기록·감사. ADMIN.

    2026-10-09 시나리오 3회차: 재심사 추론 정정 가드·정정 충돌 DEAD는 재큐해도 같은 가드에 다시 걸리는데,
    메시지는 '종결하라'면서 종결 수단이 없었다. 원장 대조를 마친 운영자가(청구 심사 전표 금액 변경은 HIS 재발행으로만) 사유와 함께 닫는다 —
    전표·도메인 데이터는 건드리지 않는다(기록만). DEAD에서만 허용(FAILED는 자동 재시도 중)."""
    require_scope(user, "admin.staging", "write")
    reason = _resolve_reason(body.reason)
    ev = (await session.execute(select(HisInboundEvent).where(HisInboundEvent.id == event_id)
                                .with_for_update())).scalar_one_or_none()
    if ev is None:
        raise ValidationError("존재하지 않는 이벤트입니다")
    await _resolve_one(session, ev, reason, user.user_id)
    out = {"id": ev.id, "status": ev.status, "resolved_at": ev.resolved_at,
           "resolve_reason": ev.resolve_reason}
    await session.commit()        # 커밋 후 응답 — 이후 DB 접근 없음
    return out


def _resolve_reason(raw: str | None) -> str:
    reason = (raw or "").strip()
    if not reason:
        raise ValidationError("처리 완료 사유를 입력하세요(공백만은 불가)")
    return reason


async def _resolve_one(session, ev: HisInboundEvent, reason: str, actor: int) -> None:
    if ev.status != "DEAD":
        raise ValidationError(
            f"#{ev.id}: DEAD 이벤트만 처리 완료로 종결할 수 있습니다(현재 {ev.status}) — FAILED는 자동 재시도 중이니 "
            "결과를 기다리거나 '재처리'하세요")
    before = {"status": ev.status, "error": ev.error}
    ev.status = "RESOLVED"
    ev.resolved_by, ev.resolved_at, ev.resolve_reason = actor, datetime.now(UTC), reason
    from erp.audit.service import AuditService
    await AuditService().record_audit(
        session, actor=actor, action="RESOLVE", entity="integration.his_inbound_events",
        target_id=str(ev.id), before=before,
        after={"status": "RESOLVED", "reason": reason, "event_type": ev.event_type})


class ResolveBulkIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=300)


@router.post("/inbound/resolve-bulk")
async def resolve_inbound_bulk(body: ResolveBulkIn, user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """여러 DEAD 이벤트를 한 사유로 종결(테스트·점검 이벤트 정리) — 전부 아니면 전무(하나라도 DEAD가 아니면 422).

    건별 감사는 그대로 남는다(RESOLVE × N). 행은 id 순으로 잠가 동시 요청 교착을 피한다."""
    require_scope(user, "admin.staging", "write")
    reason = _resolve_reason(body.reason)
    ids = sorted(set(body.ids))
    rows = (await session.execute(select(HisInboundEvent).where(HisInboundEvent.id.in_(ids))
                                  .order_by(HisInboundEvent.id).with_for_update())).scalars().all()
    missing = sorted(set(ids) - {e.id for e in rows})
    if missing:
        raise ValidationError(f"존재하지 않는 이벤트: {', '.join(map(str, missing))}")
    bad = [f"#{e.id}({e.status})" for e in rows if e.status != "DEAD"]
    if bad:
        raise ValidationError(f"DEAD가 아닌 이벤트가 섞여 있어 아무것도 종결하지 않았습니다: {', '.join(bad)}")
    for ev in rows:
        await _resolve_one(session, ev, reason, user.user_id)
    out = {"resolved": [e.id for e in rows]}
    await session.commit()
    return out


@router.post("/inbound/{event_id}/requeue")
async def requeue_inbound_event(event_id: int, background: BackgroundTasks,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """정체(FAILED/DEAD) 이벤트 재처리 — status→NEW·재시도 카운터 리셋. ADMIN."""
    require_scope(user, "admin.staging", "write")
    ev = await session.get(HisInboundEvent, event_id)
    if ev is None:
        raise ValidationError("존재하지 않는 이벤트입니다")
    if ev.status not in ("FAILED", "DEAD"):
        # APPLIED 재처리는 전표 키가 바뀐 핸들러(부분환불 등)에서 이중 전기를 낸다(2차 검토 F)
        raise ValidationError(f"FAILED/DEAD만 재처리할 수 있습니다(현재 {ev.status})")
    ev.status, ev.retry_count, ev.next_retry_at, ev.error = "NEW", 0, None, None
    # 운영자 확인 표지는 그 시점의 대조에만 유효 — 재처리는 확인을 다시 받는다(새 dict로 교체해 변경 감지).
    # 확인 후 다른 사유로 실패한 건을 나중에 재처리하면, 그 사이 이의 인정이 전기됐어도 옛 확인으로 복원됐다(6회차).
    # 환율 지정(_erpFxRate)도 같은 이유로 제거 — 재처리는 그 시점 판단을 다시 받는다(2026-10-10).
    if isinstance(ev.payload, dict) and any(str(k).startswith("_erp") for k in ev.payload):
        ev.payload = {k: v for k, v in ev.payload.items() if not str(k).startswith("_erp")}
    out = {"id": ev.id, "status": ev.status}
    # 2026-10-09 시나리오 S-A: 재큐가 신호를 안 보내 worker가 5분 폴링 때까지 NEW로 방치됐다(웹훅 경로는
    # 신호를 보낸다). 커밋을 먼저 해야 신호를 받은 worker가 NEW를 본다 — 커밋 후엔 DB 접근 없음.
    await session.commit()
    background.add_task(_signal_inbound)
    return out


class ApplyConfirmedIn(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


@router.post("/inbound/{event_id}/apply-confirmed")
async def apply_confirmed_inbound_event(event_id: int, body: ApplyConfirmedIn,
                                        background: BackgroundTasks,
                                        user: CurrentUser = Depends(get_current_user),
                                        session: AsyncSession = Depends(get_session)):
    """보류된(DEAD) 삭감 감소 재심사를 운영자 확인 후 반영 — 확인 표식을 실어 재처리. ADMIN.

    2026-10-09 시나리오 5회차: revision 없는 HIS 재심사가 삭감을 줄이면 ERP 이의신청 인정 복원과 겹칠 수 있어
    소비자가 자동 반영하지 않고 DEAD로 보류한다(HIS 청구↔이의 조정건 FK 없음 #621 — ERP가 판정 불가).
    심사팀이 이의 결과와 대조해 '아직 반영 안 됨'을 확인하면 이 EP가 payload에 `_erpConfirmedRestore`
    {by, reason, at}를 실어 NEW로 되돌린다 — 소비자는 표식이 있으면 복원 정정을 전기하되 다른 가드(낡은 재생·
    정정 충돌·청구 잠금)는 그대로 적용한다. 이미 반영됐으면 이 EP가 아니라 '처리 완료(resolve)'로 종결.

    표식은 **이 인박스 행의 payload**에만 있다 — HIS가 같은 ref로 payload를 바꿔 재발행해 행이 갱신되면
    (갱신성 이벤트 경로) 표식이 사라져 다시 보류되고, 금액이 다른 새 재심사(새 ref)도 새로 확인해야 한다.
    확인은 결정 단위라 의도된 동작이다(claim.reviewed는 현재 갱신성 이벤트가 아니라 같은 ref 재수신은 중복)."""
    require_scope(user, "admin.staging", "write")
    reason = (body.reason or "").strip()
    if not reason:
        raise ValidationError("확인 후 반영 사유를 입력하세요(공백만은 불가) — 대조한 이의 결과를 적어 주세요")
    ev = (await session.execute(select(HisInboundEvent).where(HisInboundEvent.id == event_id)
                                .with_for_update())).scalar_one_or_none()
    if ev is None:
        raise ValidationError("존재하지 않는 이벤트입니다")
    if ev.event_type != "claim.reviewed":
        raise ValidationError(
            f"'확인 후 반영'은 보류된 청구 재심사(claim.reviewed)에만 쓸 수 있습니다(현재 {ev.event_type}) "
            "— 다른 이벤트는 원인을 해소한 뒤 '재처리'하거나 '처리 완료(사유)'로 종결하세요")
    from erp.integrations.inbound_consumer import RESTORE_HOLD_PREFIX
    if ev.status == "DEAD" and RESTORE_HOLD_PREFIX not in (ev.error or ""):   # 저장 형태 "ERP-2003: …"
        # 충돌·낡은 재생·옛 가드로 DEAD된 건에 확인을 실으면, 옛 가드 건(이미 이의로 복원된 것)은 이중 복원된다(6회차)
        raise ValidationError(
            "이 이벤트는 '삭감 감소 확인 보류'가 아닌 다른 사유로 DEAD입니다 — 오류 내용대로 원인을 해소한 뒤 "
            "'재처리'하거나 '처리 완료(사유)'로 종결하세요")
    if ev.status != "DEAD":
        raise ValidationError(
            f"DEAD(보류) 이벤트만 확인 후 반영할 수 있습니다(현재 {ev.status}) — FAILED는 자동 재시도 중이니 "
            "결과를 기다리세요")
    now = datetime.now(UTC)
    before = {"status": ev.status, "error": ev.error}
    # 새 dict를 대입해야 JSON 컬럼 변경이 감지된다(제자리 수정은 ORM이 놓친다)
    ev.payload = {**(ev.payload or {}),
                  "_erpConfirmedRestore": {"by": user.user_id, "reason": reason, "at": now.isoformat()}}
    ev.status, ev.retry_count, ev.next_retry_at, ev.error = "NEW", 0, None, None
    from erp.audit.service import AuditService
    await AuditService().record_audit(
        session, actor=user.user_id, action="CONFIRM_APPLY", entity="integration.his_inbound_events",
        target_id=str(ev.id), before=before,
        after={"status": "NEW", "reason": reason, "event_type": ev.event_type})
    out = {"id": ev.id, "status": ev.status}
    await session.commit()        # 커밋 후 신호 — worker가 NEW를 보도록(requeue와 동일)
    background.add_task(_signal_inbound)
    return out


class ApplyFxIn(BaseModel):
    fx_rate: float = Field(gt=0, le=100000)
    reason: str = Field(min_length=1, max_length=300)


@router.post("/inbound/{event_id}/apply-fx")
async def apply_fx_inbound_event(event_id: int, body: ApplyFxIn, background: BackgroundTasks,
                                 user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """환율 미동봉으로 보류된(DEAD) 외화 검진권 전표 라인을 관리자 지정 환율로 반영. ADMIN.

    2026-10-10 prod: 외화(USD) 라인 6건이 6월부터 DEAD — 송신측 fxRate 미동봉이라 ERP가 KRW로 환산할 수 없었고,
    재송신 외엔 출구가 없었다. ERP에는 시점 환율 테이블이 없으므로 **사람이 근거(고시 기관·일자)와 함께**
    환율을 지정한다 — payload `_erpFxRate`{rate, by, reason, at}·감사 FX_APPLY. HIS 동봉 fxRate가 있으면 그것이 우선이고,
    `_erp*` 키는 웹훅 수신 시 제거되어 HIS가 위조할 수 없다. 재처리(requeue)는 지정을 지운다(다시 판단)."""
    require_scope(user, "admin.staging", "write")
    reason = (body.reason or "").strip()
    if not reason:
        raise ValidationError("환율 근거를 입력하세요(예: 서울외국환중개 2026-06-18 매매기준율)")
    ev = (await session.execute(select(HisInboundEvent).where(HisInboundEvent.id == event_id)
                                .with_for_update())).scalar_one_or_none()
    if ev is None:
        raise ValidationError("존재하지 않는 이벤트입니다")
    hint = inbound_action_hint(ev.event_type, ev.source_ref, ev.error)
    if ev.status != "DEAD" or hint["kind"] != "FX_HOLD":
        raise ValidationError(
            "'환율 지정 후 반영'은 환율 미동봉으로 보류된(DEAD) 외화 전표 라인에만 쓸 수 있습니다 — "
            "다른 사유는 오류 내용대로 해소한 뒤 '재처리'하거나 '처리 완료(사유)'로 종결하세요")
    p = ev.payload or {}
    cur = str(p.get("currency") or "KRW").upper()
    if cur == "KRW" or p.get("fxRate"):
        raise ValidationError("이 이벤트는 원화이거나 이미 HIS 환율(fxRate)이 있습니다 — '재처리'하세요")
    now = datetime.now(UTC)
    before = {"status": ev.status, "error": ev.error}
    ev.payload = {**p, "_erpFxRate": {"rate": body.fx_rate, "currency": cur, "by": user.user_id,
                                      "reason": reason, "at": now.isoformat()}}
    ev.status, ev.retry_count, ev.next_retry_at, ev.error = "NEW", 0, None, None
    from erp.audit.service import AuditService
    await AuditService().record_audit(
        session, actor=user.user_id, action="FX_APPLY", entity="integration.his_inbound_events",
        target_id=str(ev.id), before=before,
        after={"status": "NEW", "fx_rate": body.fx_rate, "currency": cur, "reason": reason,
               "event_type": ev.event_type})
    out = {"id": ev.id, "status": ev.status}
    await session.commit()
    background.add_task(_signal_inbound)
    return out


async def _signal_inbound() -> None:
    """커밋 후 background task — worker 리스너에 즉시 소비 트리거. 실패는 무시(폴링 안전망)."""
    from erp.common.redis_client import INBOUND_SIGNAL_CHANNEL, get_redis
    try:
        await get_redis().publish(INBOUND_SIGNAL_CHANNEL, "1")
    except Exception:  # noqa: BLE001 — Redis 장애 시 5분 폴링이 처리
        pass

ALLOWED_DOMAINS = frozenset({"finance", "materials", "assets", "procurement", "hr", "sign"})
# 갱신성 이벤트 — 동일 source_ref 재수신 시 거부 대신 payload 갱신·재처리.
# billing.line_recorded: progressive billing(라인 lineId upsert가 최종 멱등 보장).
# vendor.synced/changed: 딜러 일괄 재동기화가 고정 ref(voucher:dealer:<id>)로 재발행하므로,
#   거부하면 수수료율·등록번호 등 마스터 변경분이 유실됨. dealerId upsert라 재처리 안전.
_UPDATABLE_EVENTS = frozenset({"billing.line_recorded", "vendor.synced", "vendor.changed"})


class WebhookBody(BaseModel):
    """HIS W3 공통 봉투 {event, source, ref, occurredAt, payload}와
    ERP 자체 표기 {event_type, event_id, source_ref} 모두 수용 (alias)."""
    model_config = ConfigDict(populate_by_name=True)

    event_type: str = Field(validation_alias=AliasChoices("event_type", "event"))
    event_id: str | None = Field(default=None,
                                 validation_alias=AliasChoices("event_id", "ref"))
    source_ref: str | None = None
    source: str | None = None
    occurred_at: str | None = Field(default=None,
                                    validation_alias=AliasChoices("occurred_at", "occurredAt"))
    payload: dict = {}

    @field_validator("payload")
    @classmethod
    def _strip_erp_reserved(cls, v: dict) -> dict:
        """ERP 예약 키(`_erp*`)는 외부 입력에서 제거 — `_erpConfirmedRestore`는 운영자 '확인 후 반영'(apply-confirmed)만
        넣을 수 있다. HIS 페이로드가 이 키를 실어 보내면 확인 없이 삭감 복원이 자동 반영됐다(시나리오 6회차)."""
        return {k: val for k, val in (v or {}).items() if not str(k).startswith("_erp")}


@router.post("/{domain}/webhook", status_code=202)
async def his_webhook(domain: str, body: WebhookBody, background: BackgroundTasks,
                      request: Request,
                      x_integration_key: str | None = Header(default=None),
                      x_signature: str | None = Header(default=None),
                      x_timestamp: str | None = Header(default=None),
                      session: AsyncSession = Depends(get_session)):
    """HIS→ERP 도메인 웹훅 수신 — 인증 후 인박스 멱등 적재. consumer는 후속(Wave)."""
    s = get_settings()
    if not s.his_webhook_key:
        raise ForbiddenError("웹훅 게이트웨이 미설정")
    if not x_integration_key or not hmac.compare_digest(x_integration_key, s.his_webhook_key):
        raise ForbiddenError("웹훅 인증 실패")
    # 본문 HMAC+timestamp(S-1/S-2 동형) — 정적 키 외 본문 무결성·재생방지. rawBody는 pydantic
    # 파싱이 캐시한 동일 바이트(Starlette). ENFORCE ON=서명 필수 거부(시크릿 미설정도 fail-closed),
    # OFF=병행수락(서명 오면 검증결과 관측로그). 셋 다 없으면 rawBody 미독(레거시 무영향).
    if s.inbound_webhook_hmac_enforce or s.inbound_webhook_secret or x_signature is not None:
        sig_reason = inbound_signature_reason(
            await request.body(), x_signature, x_timestamp, s.inbound_webhook_secret,
            now_epoch=time.time(), max_skew=s.inbound_webhook_max_skew)
        if s.inbound_webhook_hmac_enforce:
            if sig_reason:
                raise ForbiddenError(f"인박스 본문서명 검증 실패: {sig_reason}")
            # 서명 통과 후 단일소비 nonce — 신선도 창 내 재생 차단(ENFORCE에서만 하드 거부).
            if not await _inbound_nonce_fresh(x_signature, window=s.inbound_webhook_max_skew):
                raise ForbiddenError("인박스 본문서명 검증 실패: 재생(중복 서명)")
        elif x_signature is not None:
            replay = sig_reason is None and not await _inbound_nonce_fresh(
                x_signature, window=s.inbound_webhook_max_skew)
            logger.info("인박스 본문서명 관측(미강제)", extra={"ctx": {
                "domain": domain, "event_type": body.event_type,
                "verdict": "재생(중복 서명)" if replay else (
                    "ok" if sig_reason is None else sig_reason)}})
    if domain not in ALLOWED_DOMAINS:
        raise ForbiddenError(f"미지원 도메인: {domain}")
    ref = body.event_id or body.source_ref or f"{domain}:{body.event_type}"
    # 멱등 — 동일 source_ref 재수신은 기존 반환(중복 적재 금지)
    # 행 잠금 — 소비자가 같은 행을 APPLIED로 커밋하는 사이에 읽으면, 아래 status='NEW' 대입이 읽은 값과
    # 같아 ORM이 UPDATE에서 빼 버려 갱신분이 처리되지 않은 채 APPLIED로 남았다(2026-10-09 검토 F).
    existing = (await session.execute(
        select(HisInboundEvent).where(HisInboundEvent.source_ref == ref))).scalar_one_or_none()
    if existing is not None:
        # 갱신성 이벤트(progressive billing 발생→정산 재발행·취소)는 동일 ref로 내용이
        # 바뀌므로 payload 갱신 + 재처리. 최종 멱등은 라인 레이어(lineId upsert)가 보장.
        if body.event_type in _UPDATABLE_EVENTS:
            # 갱신 대상일 때만 행 잠금 — 단순 중복 수신까지 잠그면 소비 배치가 끝날 때까지 웹훅이 대기했다
            await session.execute(select(HisInboundEvent.id).where(
                HisInboundEvent.id == existing.id).with_for_update())
            # retry_count·next_retry도 리셋 — 과거 DEAD(소진) 이벤트가 갱신 재발행 시
            # 1회 만에 재DEAD되어 갱신분이 유실되는 것 방지(requeue EP와 동일 처리).
            # Core UPDATE로 **무조건** 쓴다(ORM 변경 감지는 같은 값이면 컬럼을 뺀다).
            from sqlalchemy import update as _update
            await session.execute(_update(HisInboundEvent).where(
                HisInboundEvent.id == existing.id).values(
                payload=body.payload, status="NEW", retry_count=0, next_retry_at=None, error=None))
            eid = existing.id
            await session.commit()                 # 커밋 후 202(아래 주석)
            background.add_task(_signal_inbound)   # 실시간 — 재처리분 즉시 소비
            return {"received": True, "id": eid, "requeued": True}
        return {"received": True, "id": existing.id, "duplicate": True}
    ev = HisInboundEvent(domain=domain, event_type=body.event_type, source_ref=ref,
                         payload=body.payload, status="NEW")
    try:
        # 동시성 UNIQUE 경합 — 자체 savepoint만 롤백(session.rollback()은 외부 트랜잭션 오염)
        async with session.begin_nested():
            session.add(ev)
            await session.flush()
    except IntegrityError:
        return {"received": True, "duplicate": True}
    eid = ev.id
    # 응답 **전에** 커밋 — FastAPI는 yield 의존성(get_session)의 커밋을 응답 송신 뒤에 하므로, 커밋 순간
    # PG가 끊기면 HIS는 202를 받고 SENT 처리하는데 이벤트는 사라졌다(2026-10-09 검토 F).
    await session.commit()
    background.add_task(_signal_inbound)   # 실시간 — 커밋 후 worker 즉시 소비 트리거
    return {"received": True, "id": eid, "duplicate": False}
