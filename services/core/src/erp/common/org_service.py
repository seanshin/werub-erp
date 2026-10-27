"""기관 설정 서비스 — 공식 문서 발급기관 표기의 단일 소스.

우선순위: DB(common.org_settings.name) > env ORG_NAME > None(호출부 폴백 "본 의료기관").
단일행(id=1) upsert — 관리화면(/admin/org-settings)에서 ADMIN이 설정.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.models import OrgSettings
from erp.config import get_settings
from erp.errors import ValidationError

_EDITABLE = ("name", "representative", "biz_reg_no", "care_org_no", "address", "tel", "domain",
             "fiscal_year_start_month")


class OrgSettingsService:
    async def get(self, session: AsyncSession) -> OrgSettings | None:
        return (await session.execute(
            select(OrgSettings).where(OrgSettings.id == 1))).scalar_one_or_none()

    async def upsert(self, session: AsyncSession, *, actor: int, **fields) -> OrgSettings:
        """단일행(id=1) 갱신 — name 필수(공백 불가), 나머지 공백→NULL 정규화."""
        name = (fields.get("name") or "").strip()
        if not name:
            raise ValidationError("기관 정식명칭(name)은 필수입니다")
        row = await self.get(session)
        if row is None:
            row = OrgSettings(id=1, name=name)
            session.add(row)
        if fields.get("fiscal_year_start_month") is not None:
            m = int(fields["fiscal_year_start_month"])
            if not (1 <= m <= 12):
                raise ValidationError("회계연도 시작월은 1~12여야 합니다")
            fields["fiscal_year_start_month"] = m
        for f in _EDITABLE:
            if f in fields:
                v = (fields[f] or "").strip() if isinstance(fields[f], str) else fields[f]
                setattr(row, f, (v or None) if f != "name" else name)
        if "extra" in fields:      # 기타 커스텀 항목 [{key,value}] — 빈 키 제거·타입안전·상한(재검토)
            items = fields["extra"] or []
            cleaned = [{"key": str(e.get("key") or "").strip()[:80],
                        "value": str(e.get("value") or "").strip()[:500]}
                       for e in items if isinstance(e, dict) and str(e.get("key") or "").strip()]
            row.extra = cleaned[:50] or None
        row.updated_by = actor
        await session.flush()
        return row

    async def fiscal_month_would_change(self, session: AsyncSession,
                                        new_month: int | None) -> bool:
        """시작월이 실제로 바뀌는지 — 결산 존재 가드(finance 계층)를 앱 계층에서 판단하기 위한 헬퍼.

        common은 순수 커널이라 finance.vouchers 직접 조회 불가 → 변경 여부만 반환하고,
        결산 전표 존재 확인·차단은 호출부(admin_router)에서 수행(멱등키 기간 정합·적대검토 MED/HIGH).
        """
        if new_month is None:
            return False
        row = await self.get(session)
        cur = (row.fiscal_year_start_month if row is not None else 1) or 1
        return int(new_month) != cur

    async def get_phase0_cutover(self, session: AsyncSession) -> bool:
        """Phase0 가명화 컷오버 런타임 플래그(DB) — 미설정 행이면 False."""
        row = await self.get(session)
        return bool(row.phase0_cutover_enabled) if row is not None else False

    async def set_phase0_cutover(self, session: AsyncSession, *, enabled: bool,
                                 actor: int | None) -> bool:
        """컷오버 플래그 토글 — org_settings 단일행에 직접 기록(name 불변). 행 없으면 오류."""
        row = await self.get(session)
        if row is None:
            raise ValidationError("기관 설정(org_settings)이 없습니다 — /admin/org 먼저 설정하세요")
        row.phase0_cutover_enabled = bool(enabled)
        row.updated_by = actor
        await session.flush()
        return row.phase0_cutover_enabled


SSO_AUDIENCE = "erp-sso"
# 공개판: 관리자가 정하지 않은 항목은 안전한 쪽(token 방식 차단 · aud 강제)으로 본다.
# 시험 공통 설정(tests/conftest.py)만 기존 시험을 위해 False 로 돌린다.
SAFE_DEFAULTS = True   # HIS cookie 핸드오프 토큰의 aud(HIS erp-sso 발급부와 동일 문자열)


async def sso_policy(session: AsyncSession) -> dict:
    """HIS SSO 실효 정책 — DB 결정(org_settings.sso_policy) + env(HIS_JWT_AUDIENCE) 합성.

    - token_allowed: token 방식(URL·본문으로 HIS 메인 토큰) 수락 여부. 미결정=차단(공개판 SAFE_DEFAULTS).
    - audience: 강제할 aud. env가 있으면 env, 없으면 'erp-sso' — 관리자가 명시적으로 끈 경우에만 None(미검증).
    - require_jti: aud 강제 시 jti도 필수 — erp-sso 토큰은 항상 jti를 실으므로 정상 경로 무영향,
      jti 없는 토큰(=HIS 메인 토큰)의 반복 수락을 막는다.
    - undecided: 관리자가 아직 정하지 않은 항목 목록(화면 경고용).
    """
    row = (await session.execute(
        select(OrgSettings.sso_policy).where(OrgSettings.id == 1))).scalar_one_or_none()
    pol = row if isinstance(row, dict) else {}
    env_aud = (get_settings().his_jwt_audience or "").strip() or None
    token_mode = pol.get("token_mode")
    enforce = pol.get("enforce_audience")
    enforced = (enforce is not False) if SAFE_DEFAULTS else bool(enforce)
    audience = env_aud or (SSO_AUDIENCE if enforced else None)
    undecided = [k for k, v in (("token_mode", token_mode), ("enforce_audience", enforce))
                 if v is None and not (k == "enforce_audience" and env_aud)]
    allowed = (token_mode == "allow") if SAFE_DEFAULTS else (token_mode != "block")
    return {"token_allowed": allowed, "token_mode": token_mode,
            "enforce_audience": enforce, "audience": audience,
            "audience_source": "env" if env_aud else ("db" if enforced else None),
            "require_jti": audience is not None, "undecided": undecided}


async def set_sso_policy(session: AsyncSession, *, token_mode: str | None,
                         enforce_audience: bool | None, actor: int | None) -> dict:
    """SSO 정책 결정 기록 — None은 '변경 안 함'. org_settings 행이 없으면 오류(기관 설정 선행)."""
    if token_mode is not None and token_mode not in ("allow", "block"):
        raise ValidationError("token_mode는 allow 또는 block이어야 합니다")
    row = (await session.execute(
        select(OrgSettings).where(OrgSettings.id == 1))).scalar_one_or_none()
    if row is None:
        raise ValidationError("기관 설정(org_settings)이 없습니다 — /admin/org 먼저 설정하세요")
    pol = dict(row.sso_policy or {})
    if token_mode is not None:
        pol["token_mode"] = token_mode
    if enforce_audience is not None:
        pol["enforce_audience"] = bool(enforce_audience)
    row.sso_policy = pol          # JSON 컬럼은 새 객체를 대입해야 변경 감지된다
    row.updated_by = actor
    await session.flush()
    return await sso_policy(session)


async def org_logo_data_uri(session: AsyncSession) -> str | None:
    """기관 로고 이미지(첨부 entity_type=org)를 인라인 data: URI로 — 공식 문서 PDF 헤더 표기용.

    이미지 첨부가 없거나 본문 복원 실패 시 None(로고 없이 렌더). 최신 이미지 사용.
    """
    import base64

    from erp.common.attachment_service import AttachmentService
    svc = AttachmentService()
    rows = await svc.list_for(session, entity_type="org", entity_id=1)
    img = next((a for a in reversed(rows)
                if (a.content_type or "").startswith("image/")), None)
    if img is None:
        return None
    try:
        data = svc.read_bytes(img)
    except Exception:      # noqa: BLE001 — 파일 미존재 등은 로고 생략(문서 발급 계속)
        return None
    return f"data:{img.content_type};base64,{base64.b64encode(data).decode()}"


async def org_display_name(session: AsyncSession) -> str | None:
    """문서 표기용 기관명 — DB 설정 > env ORG_NAME > None(호출부가 폴백 표기)."""
    row = (await session.execute(
        select(OrgSettings.name).where(OrgSettings.id == 1))).scalar_one_or_none()
    if row and row.strip():
        return row.strip()
    env = (get_settings().org_name or "").strip()
    return env or None
