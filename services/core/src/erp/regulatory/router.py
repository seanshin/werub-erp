"""Regulatory 라우터 — 시점 단가 조회·공시 관리 (design §4.3)."""
import uuid
from collections import Counter
from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.policy import require_scope
from erp.common.ratelimit import SlidingWindowLimiter
from erp.common.router import CurrentUser, get_current_user
from erp.db import get_session
from erp.errors import ValidationError
from erp.regulatory.accreditation_service import AccreditationService
from erp.regulatory.infection_service import InfectionService
from erp.regulatory.models import DrugStdCode, PatientSafetyIncident
from erp.regulatory.radiation_service import RadiationService
from erp.regulatory.safety_service import PatientSafetyService
from erp.regulatory.service import FeeLookupService, NoticeService

router = APIRouter(tags=["regulatory"])
_fees = FeeLookupService()
_notices = NoticeService()


def _require(user: CurrentUser, resource: str, action: str):
    """단일 권한 게이트 위임 (M5 수렴)."""
    return require_scope(user, resource, action)


@router.get("/regulatory/drug-std")
async def lookup_drug_std(q: str, user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """의약품 표준코드 마스터 조회 — 바코드(표준코드)/제품코드 정확일치, 그 외 상품명 검색.

    워크스테이션에서 ERP 미등록 의약품 바코드 스캔 시 식별용(심평원 표준코드 마스터·마이그 0163).
    마스터는 scripts/import_drug_std_codes.py로 적재 — 미적재면 빈 결과(정직 반환). 표준코드는
    regulatory 도메인 소유라 이 라우터에 둔다(scm→regulatory 경계 위반 방지). RBAC는 워크스테이션
    접근을 위해 scm.items read 유지 — 소비 주체(재고 워크스테이션)에 맞춘 인가.
    """
    _require(user, "scm.items", "read")
    q = (q or "").strip()
    if len(q) < 2:
        raise ValidationError("검색어는 2자 이상이어야 합니다")
    exact = (await session.scalars(
        select(DrugStdCode).where(
            (DrugStdCode.std_code == q) | (DrugStdCode.product_code == q)).limit(10))).all()
    rows = exact or (await session.scalars(
        select(DrugStdCode).where(DrugStdCode.name.ilike(f"%{q}%"))
        .order_by(DrugStdCode.name).limit(10))).all()
    return {"count": len(rows), "match": "exact" if exact else "name", "items": [
        {"std_code": r.std_code, "name": r.name, "company": r.company, "spec": r.spec,
         "product_code": r.product_code, "ingredient_code": r.ingredient_code,
         "rx_otc": r.rx_otc, "special_class": r.special_class, "atc": r.atc,
         "canceled_on": r.canceled_on} for r in rows]}


@router.get("/regulatory/fees")
async def get_fee(code: str, code_type: str, at: date,
                  user: CurrentUser = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)):
    """시점 단가 조회 (FR-35) — 전 모듈 공용. 규칙: valid_from <= at < valid_to."""
    _require(user, "regulatory.master", "read")
    fee = await _fees.get_price(session, code=code, code_type=code_type, at_date=at)
    return {"code": fee.code, "code_type": fee.code_type, "name": fee.name,
            "unit_price": str(fee.unit_price), "price_cap": str(fee.price_cap or ""),
            "valid_from": fee.valid_from, "valid_to": fee.valid_to}


@router.get("/regulatory/fees/stats")
async def fee_stats(user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    """코드 마스터 종류별 건수 (약가/수가/치료재료) — 현행 유효."""
    from datetime import date as _date

    from sqlalchemy import func, or_, select

    from erp.regulatory.models import FeeMaster
    _require(user, "regulatory.master", "read")
    ref = _date.today()
    rows = (await session.execute(
        select(FeeMaster.code_type, func.count())
        .where(FeeMaster.valid_from <= ref,
               or_(FeeMaster.valid_to.is_(None), FeeMaster.valid_to > ref))
        .group_by(FeeMaster.code_type))).all()
    label = {"DRUG": "약가", "TARIFF": "수가", "MATERIAL": "치료재료", "NONCOVERED": "비급여"}
    return {"items": [{"code_type": t, "label": label.get(t, t), "count": c} for t, c in rows]}


@router.get("/regulatory/fees/search")
async def search_fees(q: str | None = None, code_type: str | None = None,
                      as_of: date | None = None, limit: int = 30,
                      user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """수가/약제 마스터 — 검색어(제품명·주성분·코드) 또는 종류별 목록(검색어 없이 code_type)."""
    from sqlalchemy import or_, select

    from erp.regulatory.models import FeeMaster
    _require(user, "regulatory.master", "read")
    terms = [t for t in (q or "").split() if t][:5]
    if not terms and not code_type:        # 종류 선택 없이 전체조회 방지(442K)
        return {"q": q, "count": 0, "items": []}
    ref = as_of or date.today()
    stmt = select(FeeMaster).where(
        FeeMaster.valid_from <= ref,
        or_(FeeMaster.valid_to.is_(None), FeeMaster.valid_to > ref))
    if terms:
        stmt = stmt.where(
            or_(*[or_(FeeMaster.name.ilike(f"%{t}%"), FeeMaster.code.ilike(f"%{t}%"),
                      FeeMaster.criteria_text.ilike(f"%{t}%")) for t in terms]))
    if code_type:
        stmt = stmt.where(FeeMaster.code_type == code_type)
    stmt = stmt.order_by(FeeMaster.code).limit(min(limit, 100))
    rows = (await session.execute(stmt)).scalars().all()
    return {"q": q, "count": len(rows), "items": [
        {"code": f.code, "code_type": f.code_type, "name": f.name,
         "unit_price": float(f.unit_price), "price_cap": float(f.price_cap) if f.price_cap
         is not None else None, "criteria": f.criteria_text, "valid_from": f.valid_from,
         "benefit_type": f.benefit_type,
         "reward_rate": float(f.reward_rate) if f.reward_rate is not None else None}
        for f in rows]}


@router.get("/regulatory/fees/export.csv")
async def export_code_notice_map(code_type: str | None = None,
                                 user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """코드→notice_type 매핑 export (weve AI 사전심사 #2) — 현행 유효 코드 CSV 스트림."""
    from fastapi.responses import StreamingResponse
    from sqlalchemy import or_, select

    from erp.audit.service import AuditService
    from erp.regulatory.models import FeeMaster
    _require(user, "regulatory.master", "read")
    await AuditService().record_access(   # P1-9 — 마스터 반출 접속기록(수가/약가 CSV)
        session, actor=user.user_id, action="EXPORT", entity="regulatory.fee_master",
        route="/regulatory/fees/export.csv")
    ref = date.today()
    stmt = (select(FeeMaster.code, FeeMaster.code_type, FeeMaster.name)
            .where(FeeMaster.valid_from <= ref,
                   or_(FeeMaster.valid_to.is_(None), FeeMaster.valid_to > ref))
            .order_by(FeeMaster.id))
    if code_type:
        stmt = stmt.where(FeeMaster.code_type == code_type)

    async def gen():
        yield "code,code_type,notice_types,name\r\n"
        result = await session.stream(stmt)
        async for code, ct, name in result:
            nt = "|".join(_CODE_TYPE_TO_NOTICE.get(ct, _DEFAULT_NOTICE_TYPES))
            nm = name or ""
            if nm[:1] in ("=", "+", "-", "@"):    # CSV 수식주입 방어(Excel)
                nm = "'" + nm
            safe = nm.replace('"', '""')
            yield f'{code},{ct},{nt},"{safe}"\r\n'

    return StreamingResponse(gen(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="fee_code_notice_map.csv"'})


class NoticeCreate(BaseModel):
    notice_type: str
    title: str
    body_text: str
    effective_from: date
    effective_to: date | None = None
    urgent_flag: bool = False


@router.post("/regulatory/notices", status_code=201)
async def create_notice(body: NoticeCreate,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user, "regulatory.notices", "write")
    notice = await _notices.create(session, notice_type=body.notice_type, title=body.title,
                                   body_text=body.body_text, effective_from=body.effective_from,
                                   effective_to=body.effective_to, created_by=user.user_id,
                                   urgent_flag=body.urgent_flag)
    return {"erp_ref_id": str(notice.erp_ref_id), "revision_no": notice.current_rev}


@router.put("/regulatory/notices/{erp_ref_id}")
async def revise_notice(erp_ref_id: uuid.UUID, body: NoticeCreate,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user, "regulatory.notices", "write")
    rev = await _notices.revise(session, erp_ref_id=erp_ref_id, title=body.title,
                                body_text=body.body_text, effective_from=body.effective_from,
                                effective_to=body.effective_to, created_by=user.user_id,
                                urgent_flag=body.urgent_flag)
    return {"erp_ref_id": str(erp_ref_id), "revision_no": rev.revision_no}


@router.get("/regulatory/notices/search")
async def search_notices(q: str, as_of: date | None = None, notice_type: str | None = None,
                         limit: int = 20, user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """공시 전문검색 (AI 사전심사 P0 — retrieval). 청구 키워드/진료일로 유효 고시·심사기준."""
    from erp.regulatory.notice_search import NoticeSearchService
    _require(user, "regulatory.master", "read")
    items = await NoticeSearchService().search(
        session, q=q, as_of=as_of, notice_type=notice_type, limit=min(limit, 50))
    return {"q": q, "count": len(items), "items": items}


@router.get("/regulatory/notices/ai-search")
async def ai_search_notices(q: str, limit: int = 5, as_of: date | None = None,
                            notice_type: str | None = None,
                            user: CurrentUser = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """AI 공시 검색 (P0) — weve `/api/notice/search`(진료일·type·active_only 필터). 오류 시 로컬 fallback."""
    import asyncio

    from erp.config import get_settings
    from erp.regulatory.notice_search import NoticeSearchService
    _require(user, "regulatory.master", "read")
    s = get_settings()
    # 사전심사 기본 필터: 심사기준·수가 고시, 진료일(as_of) 시행, 폐지 제외
    types = [notice_type] if notice_type else ["REVIEW_CRITERIA", "TARIFF"]
    if s.weve_ai_api_key:
        # 외부 AI 전송 전 비식별 게이트(Phase0 감사 MED) — 검색어에 차트번호/주민/전화 추정 시
        # 외부 전송 없이 로컬 검색으로 강등(ai_diagnose는 차단, 검색은 UX상 로컬 폴백이 적합).
        from erp.regulatory.filter_gate import pii_reference_sets, scan as _scan_pii
        _charts, _emps = await pii_reference_sets(session)
        if _scan_pii(q, known_chart_nos=_charts, known_employee_nos=_emps).blocked:
            items = await NoticeSearchService().search(session, q=q, as_of=as_of,
                                                       limit=min(limit, 10))
            return {"q": q, "source": "local", "count": len(items), "items": items,
                    "note": "개인식별정보 추정 검색어 — 외부 AI 미전송(로컬 검색)"}
    if s.weve_ai_api_key:
        try:
            from erp.integrations.weve_rag import WeveRagClient
            # base가 publisher용 /api/notice를 포함하면 RAG 루트로 정규화(/ollama)
            rag_base = s.weve_ai_api_base.rstrip("/")
            if rag_base.endswith("/api/notice"):
                rag_base = rag_base[: -len("/api/notice")]
            client = WeveRagClient(rag_base, s.weve_ai_api_key)
            rows = await asyncio.to_thread(
                client.search, q, n_results=min(limit, 10), notice_type=types,
                effective_on=as_of.isoformat() if as_of else None, active_only=True)
            return {"q": q, "source": "weve", "count": len(rows), "items": [
                {"title": (r.get("metadata") or {}).get("title"),
                 "notice_type": (r.get("metadata") or {}).get("notice_type"),
                 "score": r.get("score"), "snippet": (r.get("text") or "")[:200],
                 "effective_from": (r.get("metadata") or {}).get("effective_from")}
                for r in rows]}
        except Exception as exc:  # noqa: BLE001 — weve 장애·설정 오류 시 로컬 fallback
            # 폴백 사유를 남기고 응답에도 싣는다 — 종전엔 허용 호스트 위반 같은
            # **설정 오류**도 조용히 로컬 검색으로 바뀌어, AI 검색이 한 번도 안 된 것을 아무도 몰랐다.
            import logging
            logging.getLogger("erp.ai").warning("공시 AI 검색 실패 — 로컬 검색으로 폴백", extra={
                "ctx": {"error": type(exc).__name__, "detail": str(exc)[:120]}})
            items = await NoticeSearchService().search(session, q=q, as_of=as_of,
                                                       limit=min(limit, 10))
            return {"q": q, "source": "local", "count": len(items), "items": items,
                    "fallback_reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
    items = await NoticeSearchService().search(session, q=q, as_of=as_of, limit=min(limit, 10))
    return {"q": q, "source": "local", "count": len(items), "items": items}


# 청구 코드 code_type → 관련 notice_type 필터 (weve 합의 매핑). 미등재/미상은 기본값.
_CODE_TYPE_TO_NOTICE = {
    "TARIFF": ["REVIEW_CRITERIA", "TARIFF"],
    "DRUG": ["DRUG_MATERIAL", "REVIEW_CRITERIA"],
    "MATERIAL": ["DRUG_MATERIAL", "REVIEW_CRITERIA"],
}
_DEFAULT_NOTICE_TYPES = ["REVIEW_CRITERIA", "TARIFF"]


async def _notice_types_for_code(session, code: str) -> list[str]:
    from sqlalchemy import select

    from erp.regulatory.models import FeeMaster
    ct = await session.scalar(select(FeeMaster.code_type).where(FeeMaster.code == code).limit(1))
    return _CODE_TYPE_TO_NOTICE.get(ct, _DEFAULT_NOTICE_TYPES)


class AiDiagnoseBody(BaseModel):
    code: str
    description: str | None = None
    service_date: date | None = None
    context: str | None = None


# 외부(weve) 표본에서 프론트가 쓰는 필드만 선별 — RAG 응답 무필터 패스스루 방지(내부/디버그 키 차단)
_SAFE_REP_KEYS = ("recommendation", "note", "reasoning", "disclaimer", "evidence_citations")


def _aggregate_diagnoses(oks: list[dict], attempted: int) -> dict:
    """근거검색 N표본 다수결 집계 — 위험도 다수결(동률=보수적 높은위험), 근거 과반 없으면 보류.

    근거검색(LLM RAG) 비결정성 완화. 대표 표본(인용 多→score 高)을 세부내역으로 채택.
    risk_score는 전체가 아니라 '승리 위험도(top)와 같은 표본(reps)'만 평균 — 동률로 보수적
    HIGH가 채택되면 HIGH 표본 점수만 반영해 위험을 과소표시하지 않는다.
    attempted=시도 횟수, samples=성공 표본. 성공이 시도 과반 미만이면 degraded(부분 실패) 표시.
    """
    degraded = len(oks) * 2 < attempted     # 성공 표본이 시도 과반 미만 → 다수결 신뢰 저하
    if not oks:
        return {"available": False, "note": "근거검색 일시 오류",
                "attempted": attempted, "samples": 0}
    ev = [r for r in oks if r.get("has_evidence", bool(r.get("evidence_citations")))]
    if len(ev) * 2 < len(oks):     # 과반이 근거 없음 → 보류(일부만이면 자동 폐기)
        return {"available": True, "held": True, "risk_level": None,
                "note": "근거 고시 없음 — 판단 보류(로컬 룰만 적용)",
                "attempted": attempted, "samples": len(oks), "degraded": degraded}
    _rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    votes = Counter((r.get("risk_level") or "LOW") for r in ev)
    top = max(votes.items(), key=lambda kv: (kv[1], _rank.get(kv[0], 0)))[0]
    reps = [r for r in ev if (r.get("risk_level") or "LOW") == top]
    rep = max(reps, key=lambda r: (len(r.get("evidence_citations") or []),
                                   r.get("risk_score") or 0))
    avg = round(sum((r.get("risk_score") or 0) for r in reps) / len(reps), 2)
    safe_rep = {k: rep[k] for k in _SAFE_REP_KEYS if k in rep}
    return {"available": True, "held": False, **safe_rep,
            "risk_level": top, "risk_score": avg,
            "attempted": attempted, "samples": len(oks),       # samples=성공, attempted=시도
            "evidence_samples": len(ev),                       # 근거 보유 표본(votes 모집단)
            "risk_votes": dict(votes), "degraded": degraded}


# 외부 LLM(weve) 증폭 방어 — 사용자별 호출 폭주 제한(M2). 단일 호출이 N표본 동시호출이므로 보수적.
_diag_limiter = SlidingWindowLimiter()


@router.post("/regulatory/notices/ai-diagnose")
async def ai_diagnose(body: AiDiagnoseBody, user: CurrentUser = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """AI 삭감위험 평가 (P0.5/P1) — weve diagnose. 근거 없으면 '판단 보류'(휴먼인더루프·비식별)."""
    import asyncio

    from erp.config import get_settings
    from erp.errors import ValidationError
    from erp.regulatory.filter_gate import scan as _scan_pii
    _require(user, "regulatory.master", "read")
    s = get_settings()
    # 외부 LLM 증폭 방어 — 사용자별 rate-limit(인증·권한 통과 후, 비싼 외부호출 전 조기 차단)
    _diag_limiter.check(
        f"diag:{user.user_id}",
        max_calls=int(getattr(s, "weve_diagnose_rate_max", 20) or 20),
        window_s=float(getattr(s, "weve_diagnose_rate_window", 60.0) or 60.0))
    if not s.weve_ai_api_key:   # 키 미설정 = 외부 전송 없음 → PII 전수로드 전에 조기 반환(불필요 부하 회피)
        return {"available": False, "note": "AI 평가 미연결(weve 키 미설정)"}
    # 외부 AI 전송 전 비식별 게이트 — description·context 자유문에 PII 추정 시 차단.
    # 실 차트번호·사번 사전 대조(known_) 주입 — 숫자전용 차트번호는 정규식만으론 미포착(외부 LLM 유출).
    if body.description or body.context:
        from erp.regulatory.filter_gate import pii_reference_sets
        _charts, _emps = await pii_reference_sets(session)
        if _scan_pii(f"{body.description or ''}\n{body.context or ''}",
                     known_chart_nos=_charts, known_employee_nos=_emps).blocked:
            raise ValidationError("개인식별정보 추정 입력 — AI 평가 전송 차단(비식별 입력 필요)")
    rag_base = s.weve_ai_api_base.rstrip("/")
    if rag_base.endswith("/api/notice"):
        rag_base = rag_base[: -len("/api/notice")]
    ntypes = await _notice_types_for_code(session, body.code)  # 청구코드 type→notice_type
    # 다수결 안정화 — weve(LLM RAG) 비결정성 완화: N회 동시 호출 후 집계.
    import logging
    _log = logging.getLogger("erp.ai")
    samples = max(1, int(getattr(s, "weve_diagnose_samples", 3) or 3))
    timeout = float(getattr(s, "weve_diagnose_timeout", 30.0) or 30.0)
    from erp.integrations.weve_rag import WeveRagClient
    try:
        client = WeveRagClient(rag_base, s.weve_ai_api_key)
    except Exception:  # noqa: BLE001 — 설정 오류(allowlist 위반 등) 비노출
        _log.warning("ai-diagnose 클라이언트 생성 실패 code=%s", body.code)
        return {"available": False, "note": "근거검색 설정 오류", "attempted": 0, "samples": 0}

    def _one():
        return client.diagnose(
            code=body.code, description=body.description, context=body.context,
            service_date=body.service_date.isoformat() if body.service_date else None,
            notice_type=ntypes)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[asyncio.to_thread(_one) for _ in range(samples)],
                           return_exceptions=True),
            timeout=timeout)
    except asyncio.TimeoutError:
        _log.warning("ai-diagnose 전체 타임아웃(%.0fs) code=%s", timeout, body.code)
        return {"available": False, "note": "근거검색 응답 지연", "attempted": samples, "samples": 0}
    oks: list[dict] = []
    for r in results:
        if isinstance(r, dict):
            oks.append(r)
        elif isinstance(r, BaseException):   # 예외 본문/스택·내부 URL 비노출 — 타입명만 기록
            _log.warning("ai-diagnose weve 호출 실패 code=%s: %s", body.code, type(r).__name__)
    return _aggregate_diagnoses(oks, samples)


@router.get("/regulatory/notices")
async def list_notices(user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session),
                       page: int = 1, size: int = 50):
    """공시 목록 (FR-51) — 현행 revision 기준."""
    _require(user, "regulatory.master", "read")
    from sqlalchemy import select

    from erp.regulatory.models import Notice, NoticeRevision
    size = min(size, 200)
    notices = (await session.execute(
        select(Notice).order_by(Notice.created_at.desc())
        .offset((page - 1) * size).limit(size))).scalars().all()
    # 분석 M1 — N+1 제거: 현행 revision IN-batch
    rev_map = {}
    if notices:
        for rev in (await session.execute(
                select(NoticeRevision).where(
                    NoticeRevision.erp_ref_id.in_([n.erp_ref_id for n in notices])))).scalars():
            rev_map[(rev.erp_ref_id, rev.revision_no)] = rev
    items = []
    for n in notices:
        rev = rev_map[(n.erp_ref_id, n.current_rev)]
        items.append({"erp_ref_id": str(n.erp_ref_id), "notice_type": n.notice_type,
                      "revision_no": n.current_rev, "title": rev.title,
                      "status": rev.status, "effective_from": rev.effective_from})
    return {"items": items, "page": page, "size": size}


@router.get("/regulatory/nursing-grade/monitor")
async def nursing_grade_monitor(hospital_type: str | None = None,
                                patient_days: float | None = None,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """간호등급 모니터링 (P3) — 간호사 재직일수 자동(휴직 16일+ 제외)·재원일수(수동>저장census>HIS>추정),
    충원 시뮬레이션(상위 진입 필요 인원·1명 이탈 위험). 종별 미지정 시 기관 규제 프로필 사용."""
    from erp.regulatory.nursing_monitor import NursingMonitor
    _require(user, "regulatory.master", "read")
    return await NursingMonitor().monitor(session, hospital_type=hospital_type,
                                          patient_days=patient_days)


# ── 기관 규제 프로필 (종별·허가병상) — 간호등급 등 산정 입력 관리 ──────────────
class InstitutionProfileBody(BaseModel):
    hospital_type: str | None = None
    licensed_beds: int | None = None


@router.get("/regulatory/institution-profile")
async def get_institution_profile(user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    from erp.regulatory.institution_service import InstitutionProfileService
    _require(user, "regulatory.master", "read")
    return await InstitutionProfileService().effective(session)


@router.put("/regulatory/institution-profile")
async def put_institution_profile(body: InstitutionProfileBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    from erp.regulatory.institution_service import InstitutionProfileService
    _require(user, "regulatory.master", "write")
    row = await InstitutionProfileService().upsert(
        session, actor=user.user_id, hospital_type=body.hospital_type,
        licensed_beds=body.licensed_beds)
    return {"hospital_type": row.hospital_type, "licensed_beds": row.licensed_beds,
            "updated_at": row.updated_at, "source": "db"}


# ── 분기 재원환자수 대장 — 간호등급 산정 실측 재원일수(HIS census 도착 전 수동) ──
class NursingCensusBody(BaseModel):
    patient_days: float
    as_of: date | None = None      # 대상 분기 내 임의 날짜(미지정=오늘) → 산정기간 자동 도출
    note: str | None = None


@router.get("/regulatory/nursing-census")
async def list_nursing_census(user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    from erp.regulatory.institution_service import NursingCensusService
    _require(user, "regulatory.master", "read")
    rows = await NursingCensusService().list(session)
    return {"items": [{"id": r.id, "period_start": r.period_start, "period_end": r.period_end,
                       "patient_days": float(r.patient_days), "note": r.note,
                       "entered_at": r.entered_at} for r in rows]}


@router.post("/regulatory/nursing-census")
async def upsert_nursing_census(body: NursingCensusBody,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    from erp.regulatory.institution_service import NursingCensusService
    from erp.regulatory.nursing_grade import quarter_window
    _require(user, "regulatory.master", "write")
    start, end = quarter_window(body.as_of or date.today())
    row = await NursingCensusService().upsert(
        session, actor=user.user_id, period_start=start, period_end=end,
        patient_days=body.patient_days, note=body.note)
    return {"id": row.id, "period_start": row.period_start, "period_end": row.period_end,
            "patient_days": float(row.patient_days)}


@router.delete("/regulatory/nursing-census/{census_id}")
async def delete_nursing_census(census_id: int,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    from erp.regulatory.institution_service import NursingCensusService
    _require(user, "regulatory.master", "write")
    await NursingCensusService().delete(session, census_id=census_id)
    return {"deleted": census_id}


class NurseAttrBody(BaseModel):
    weekly_hours: float | None = None            # 주 근로시간(단시간 환산·NULL=전일제 1.0)
    nursing_exclude_reason: str | None = None    # 산정 제외 사유(간호감독/노조전임/호스피스·NULL=포함)


@router.post("/regulatory/nursing-staff/{employee_id}")
async def set_nursing_staff_attrs(employee_id: int, body: NurseAttrBody,
                                  user: CurrentUser = Depends(get_current_user),
                                  session: AsyncSession = Depends(get_session)):
    """간호사 주 근로시간(FTE)·산정 제외 사유 설정 — 간호등급 산정 정밀화."""
    from erp.regulatory.institution_service import set_nurse_attributes
    _require(user, "regulatory.master", "write")
    emp = await set_nurse_attributes(
        session, actor=user.user_id, employee_id=employee_id, weekly_hours=body.weekly_hours,
        nursing_exclude_reason=body.nursing_exclude_reason)
    return {"employee_id": emp.id, "weekly_hours": float(emp.weekly_hours)
            if emp.weekly_hours is not None else None,
            "nursing_exclude_reason": emp.nursing_exclude_reason}


@router.get("/regulatory/nursing-grade")
async def nursing_grade_calc(patient_days: float, nurse_days: float,
                             hospital_type: str = "HOSPITAL",
                             user: CurrentUser = Depends(get_current_user)):
    """간호등급 계산기 (FR-36, G2) — 고시 산정 규칙. 재원일수 자동 집계는 HIS 연계(M0a) 후."""
    _require(user, "regulatory.master", "read")
    from datetime import date as _date

    from erp.regulatory.nursing_grade import compute_grade, quarter_window
    start, end = quarter_window(_date.today())
    r = compute_grade(patient_days=patient_days, nurse_days=nurse_days,
                      period_start=start, period_end=end, hospital_type=hospital_type)
    return {"period": {"start": r.period_start, "end": r.period_end},
            "avg_patients": r.avg_patients, "avg_nurses": r.avg_nurses,
            "ratio": r.ratio, "grade": r.grade,
            "note": ("고시 제2023-235호 산정 규칙 확정 적용"
                     if r.grade is not None else
                     "비율 확정 산출 — 해당 종별 등급 구간은 산정지침 확인 후 등록 (G2 잔여)")}


@router.get("/regulatory/notices/{erp_ref_id}/dispatch-status")
async def dispatch_status(erp_ref_id: uuid.UUID,
                          user: CurrentUser = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    """AI 색인 발신·필터 상태 (FR-52/53) — 발신 모니터·차단 검토 큐 백엔드."""
    _require(user, "regulatory.notices", "write")
    from sqlalchemy import select

    from erp.regulatory.models import AiDispatchQueue
    rows = (await session.execute(
        select(AiDispatchQueue).where(AiDispatchQueue.erp_ref_id == erp_ref_id)
        .order_by(AiDispatchQueue.id.desc()))).scalars().all()
    return {"items": [
        {"id": q.id, "event_type": q.event_type, "revision_no": q.revision_no,
         "urgency": q.urgency, "filter_status": q.filter_status,
         "filter_detail": q.filter_detail, "dispatch_status": q.dispatch_status,
         "retry_count": q.retry_count, "sent_at": q.sent_at,
         "elapsed_ms": q.elapsed_ms} for q in rows]}


@router.post("/regulatory/dispatch/{queue_id}/approve")
async def approve_dispatch(queue_id: int,
                           user: CurrentUser = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    """BLOCKED 발신 건 관리자 승인 (FR-53) — 승인은 현재 content_hash에 바인딩 (design §5.3).

    발신 직전 본문이 다시 바뀌면 워커가 hash 불일치로 재필터하므로 우회 불가.
    """
    _require(user, "regulatory.notices", "write")
    from sqlalchemy import select

    from erp.audit.service import AuditService
    from erp.errors import ValidationError
    from erp.regulatory.models import AiDispatchQueue, NoticeRevision
    entry = (await session.execute(
        select(AiDispatchQueue).where(AiDispatchQueue.id == queue_id)
    )).scalar_one_or_none()
    if entry is None:
        raise ValidationError("존재하지 않는 발신 건입니다")
    if entry.filter_status != "BLOCKED":
        raise ValidationError("BLOCKED 상태만 승인할 수 있습니다",
                              {"filter_status": entry.filter_status})
    rev = (await session.execute(
        select(NoticeRevision).where(NoticeRevision.erp_ref_id == entry.erp_ref_id,
                                     NoticeRevision.revision_no == entry.revision_no)
    )).scalar_one()
    entry.filter_status = "APPROVED"
    entry.approved_hash = rev.content_hash  # hash 바인딩 — 본문 변경 시 재필터
    entry.dispatch_status = "QUEUED"
    await AuditService().record_audit(session, actor=user.user_id, action="APPROVE",
                                      entity="regulatory.ai_dispatch_queue",
                                      target_id=str(queue_id),
                                      after={"approved_hash": rev.content_hash})
    return {"id": queue_id, "filter_status": entry.filter_status}


@router.post("/regulatory/notices/{erp_ref_id}/revoke")
async def revoke_notice(erp_ref_id: uuid.UUID,
                        user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    _require(user, "regulatory.notices", "write")
    await _notices.revoke(session, erp_ref_id=erp_ref_id, created_by=user.user_id)
    return {"erp_ref_id": str(erp_ref_id), "status": "REPEALED"}


# ── 환자안전사고 보고 (환자안전법 §14) ──────────────────────────
_safety = PatientSafetyService()


class SafetyReportBody(BaseModel):
    occurred_on: date
    incident_type: str                 # NEAR_MISS|ADVERSE|SENTINEL
    description: str
    severity: str = "NONE"
    category: str | None = None
    department_id: int | None = None
    anonymous: bool = False            # 자율보고 — 익명 시 보고자 미기록


@router.post("/regulatory/safety/incidents", status_code=201)
async def report_safety_incident(body: SafetyReportBody,
                                 user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """환자안전사고 자율보고 — 전 직원(익명 가능)."""
    _require(user, "regulatory.safety", "write")
    inc = await _safety.report(
        session, occurred_on=body.occurred_on, incident_type=body.incident_type,
        description=body.description, severity=body.severity, category=body.category,
        department_id=body.department_id,
        reporter_id=None if body.anonymous else user.user_id)
    return {"id": inc.id, "status": inc.status}


@router.get("/regulatory/safety/incidents")
async def list_safety_incidents(status: str | None = None, limit: int = 100,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """환자안전 자율보고 목록.

    사고 경위(`description`)·재발방지 조치(`action`)는 **검토·종결 권한자에게만** 반환한다.
    ⚠ v1.270에서 전 직원에게 열었던 것을 되돌린 것이다. `read` 스코프는 STAFF 포함 전 역할이
    ALL인데, 경위는 자유서술이고 모델 주석도 "비식별 **권장**"일 뿐 강제가 아니다 — 환자 상황·
    부서·시각이 그대로 실린다. 게다가 경위+부서+시각이면 보고자도 특정 가능해서, `reporter_id`만
    가려서는 자율보고 익명성이 지켜지지 않는다. 경위가 필요한 사람은 상태를 전이시키는
    (`approve`) DIRECTOR/ADMIN/MANAGER뿐이므로 거기에 맞춘다.
    통계·목록(발생일·유형·중대도·상태)은 종전대로 전 직원이 본다.
    """
    _require(user, "regulatory.safety", "read")
    from sqlalchemy import select

    from erp.common.policy import Role, ScopeKind, resolve_scope
    from erp.hr.employee_service import user_labels
    can_review = resolve_scope("regulatory.safety", "approve", role=Role(user.role),
                               dept_id=user.dept_id, user_id=user.user_id).kind != ScopeKind.NONE
    q = select(PatientSafetyIncident).order_by(PatientSafetyIncident.id.desc())
    if status:
        q = q.where(PatientSafetyIncident.status == status)
    rows = (await session.execute(q.limit(min(limit, 500)))).scalars().all()
    labels = await user_labels(session, [r.reviewed_by for r in rows]) if can_review else {}
    return {"items": [{"id": r.id, "occurred_on": str(r.occurred_on),
                       "incident_type": r.incident_type, "severity": r.severity,
                       "category": r.category, "status": r.status,
                       "description": r.description if can_review else None,
                       "action": r.action if can_review else None,
                       "reviewed_by_name": labels.get(r.reviewed_by) if can_review else None,
                       "reviewed_at": r.reviewed_at,
                       "narrative_visible": can_review,
                       "anonymous": r.reporter_id is None} for r in rows]}


class SafetyTransitionBody(BaseModel):
    to_status: str                     # REVIEWING|ACTIONED|CLOSED
    action: str | None = None


@router.patch("/regulatory/safety/incidents/{incident_id}")
async def transition_safety_incident(incident_id: int, body: SafetyTransitionBody,
                                     user: CurrentUser = Depends(get_current_user),
                                     session: AsyncSession = Depends(get_session)):
    """검토·조치·종결 — 관리자/부서장(regulatory.safety:approve)."""
    _require(user, "regulatory.safety", "approve")
    inc = await _safety.transition(session, incident_id=incident_id, to_status=body.to_status,
                                   actor=user.user_id, action=body.action)
    return {"id": inc.id, "status": inc.status}


@router.get("/regulatory/safety/stats")
async def safety_stats(start: date, end: date,
                       user: CurrentUser = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    """환자안전위원회 보고 통계 — 유형·중대도·미종결 건수."""
    _require(user, "regulatory.safety", "read")
    return await _safety.stats(session, since=start, until=end)


# ── 방사선 안전관리 (진단용방사선 안전관리규칙) ──────────────────
_radiation = RadiationService()


class RadDoseBody(BaseModel):
    employee_id: int
    period: str           # yyyy-Qn / yyyy-mm
    dose_msv: float
    measured_on: date
    instrument: str | None = None


@router.post("/regulatory/radiation/doses", status_code=201)
async def record_radiation_dose(body: RadDoseBody,
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """방사선 종사자 피폭선량 기록 — 연 누적 한도(50mSv) 초과 경고."""
    _require(user, "regulatory.master", "write")
    return await _radiation.record_dose(
        session, employee_id=body.employee_id, period=body.period, dose_msv=body.dose_msv,
        measured_on=body.measured_on, instrument=body.instrument, actor=user.user_id)


class RadEquipBody(BaseModel):
    equipment_no: str
    name: str
    next_inspection_on: date
    last_inspected_on: date | None = None


@router.post("/regulatory/radiation/equipment", status_code=201)
async def register_radiation_equipment(body: RadEquipBody,
                                       user: CurrentUser = Depends(get_current_user),
                                       session: AsyncSession = Depends(get_session)):
    """방사선 장비 등록 — 정기검사 만기 추적."""
    _require(user, "regulatory.master", "write")
    from erp.regulatory.models import RadiationEquipment
    eq = RadiationEquipment(equipment_no=body.equipment_no, name=body.name,
                            next_inspection_on=body.next_inspection_on,
                            last_inspected_on=body.last_inspected_on)
    session.add(eq)
    await session.flush()
    return {"id": eq.id}


@router.get("/regulatory/radiation/inspections-due")
async def radiation_inspections_due(within_days: int = 30,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """정기검사 만기 임박 장비."""
    _require(user, "regulatory.master", "read")
    return await _radiation.inspections_due(session, as_of=date.today(), within_days=within_days)


@router.get("/regulatory/radiation/doses")
async def list_radiation_doses(year: str | None = None,
                               user: CurrentUser = Depends(get_current_user),
                               session: AsyncSession = Depends(get_session)):
    """연도 종사자별 피폭선량(YTD·한도초과·측정 이력) — 방사선 안전 화면.

    선량은 개인 건강정보 — 원장·관리자는 전체, 부서장은 자기 부서 종사자, 그 밖은 본인 것만 본다.
    """
    from erp.common.models import User
    from erp.common.policy import ScopeKind
    from erp.hr.models import Employee
    scope = _require(user, "regulatory.radiation_dose", "read")
    out = await _radiation.doses_by_employee(session, year=year or str(date.today().year))
    if scope.kind == ScopeKind.ALL:
        return out
    if scope.kind == ScopeKind.DEPT:
        allowed = set((await session.scalars(select(Employee.id).where(
            Employee.department_id == user.dept_id))).all()) if user.dept_id else set()
    else:
        own = await session.scalar(select(User.employee_id).where(User.id == user.user_id))
        allowed = {own} if own else set()
    out["items"] = [i for i in out["items"] if i["employee_id"] in allowed]
    return out


@router.get("/regulatory/radiation/equipment")
async def list_radiation_equipment(user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """전 방사선 장비 목록(정기검사 만기 상태 포함)."""
    _require(user, "regulatory.master", "read")
    return await _radiation.list_equipment(session, as_of=date.today())


# ── 감염관리 지표 (의료기관 인증평가·P3-1) ─────────────────────────
_infection = InfectionService()


class InfectionBody(BaseModel):
    period: str                        # YYYY-MM
    indicator_type: str                # HAI_UTI|HAI_BSI|HAI_SSI|HAI_VAP|HAND_HYGIENE|ANTIBIOTIC|MDRO
    numerator: float
    denominator: float
    multiplier: int = 1000
    department_id: int | None = None
    target: float | None = None
    note: str | None = None


@router.post("/regulatory/infection/indicators", status_code=201)
async def record_infection_indicator(body: InfectionBody,
                                     user: CurrentUser = Depends(get_current_user),
                                     session: AsyncSession = Depends(get_session)):
    """감염관리 지표 입력(월별 멱등) — 감염관리실/관리자."""
    _require(user, "regulatory.infection", "write")
    return await _infection.record(
        session, period=body.period, indicator_type=body.indicator_type,
        numerator=body.numerator, denominator=body.denominator, multiplier=body.multiplier,
        department_id=body.department_id, target=body.target, note=body.note, actor=user.user_id)


@router.get("/regulatory/infection/indicators")
async def list_infection_indicators(indicator_type: str | None = None,
                                    period_from: str | None = None, period_to: str | None = None,
                                    department_id: int | None = None,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """감염관리 지표 목록 — 유형·기간범위·부서 필터(비율·목표대비 상태 포함)."""
    _require(user, "regulatory.infection", "read")
    return {"items": await _infection.list_indicators(
        session, indicator_type=indicator_type, period_from=period_from,
        period_to=period_to, department_id=department_id)}


@router.get("/regulatory/infection/dashboard")
async def infection_dashboard(period: str,
                              user: CurrentUser = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    """월 감염관리 대시보드 — 유형별 당월 지표 + 목표초과(ALERT) 경보."""
    _require(user, "regulatory.infection", "read")
    return await _infection.dashboard(session, period=period)


# ── 의료기관 인증평가 조사항목 관리 (상시준비·P3-2) ────────────────
_accred = AccreditationService()


class AccreditationBody(BaseModel):
    domain: str                        # BASIC|CARE|ADMIN|PERFORMANCE
    item_code: str
    title: str
    standard: str = "ACUTE"
    chapter: str | None = None
    requirement: str | None = None
    evidence_note: str | None = None
    status: str = "GAP"
    owner_dept_id: int | None = None
    note: str | None = None


@router.post("/regulatory/accreditation/items", status_code=201)
async def create_accreditation_item(body: AccreditationBody,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """인증 조사항목 등록 — 품질팀/관리자."""
    _require(user, "regulatory.accreditation", "write")
    it = await _accred.create(session, domain=body.domain, item_code=body.item_code,
                              title=body.title, standard=body.standard, chapter=body.chapter,
                              requirement=body.requirement, evidence_note=body.evidence_note,
                              status=body.status, owner_dept_id=body.owner_dept_id,
                              note=body.note, actor=user.user_id)
    return {"id": it.id, "status": it.status}


class AccreditationPatchBody(BaseModel):
    status: str | None = None
    evidence_note: str | None = None
    owner_dept_id: int | None = None
    requirement: str | None = None
    note: str | None = None
    reviewed: bool = False


@router.patch("/regulatory/accreditation/items/{item_id}")
async def update_accreditation_item(item_id: int, body: AccreditationPatchBody,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """조사항목 준비상태·근거·담당 갱신."""
    _require(user, "regulatory.accreditation", "write")
    it = await _accred.update(session, item_id=item_id, status=body.status,
                              evidence_note=body.evidence_note, owner_dept_id=body.owner_dept_id,
                              requirement=body.requirement, note=body.note, reviewed=body.reviewed)
    return {"id": it.id, "status": it.status,
            "last_reviewed_on": str(it.last_reviewed_on) if it.last_reviewed_on else None}


@router.get("/regulatory/accreditation/items")
async def list_accreditation_items(domain: str | None = None, status: str | None = None,
                                   standard: str = "ACUTE",
                                   user: CurrentUser = Depends(get_current_user),
                                   session: AsyncSession = Depends(get_session)):
    """조사항목 목록 — 영역·상태 필터."""
    _require(user, "regulatory.accreditation", "read")
    return {"items": await _accred.list_items(session, domain=domain, status=status,
                                              standard=standard)}


@router.get("/regulatory/accreditation/summary")
async def accreditation_summary(standard: str = "ACUTE",
                                user: CurrentUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """인증 준비 요약 — 영역별 상태 카운트·준비율."""
    _require(user, "regulatory.accreditation", "read")
    return await _accred.summary(session, standard=standard)
