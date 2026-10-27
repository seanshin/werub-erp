"""셀프서비스 BI(Metabase) 자동 로그인 브리지 — ERP 인증 → Metabase 세션 발급·쿠키 주입.

Metabase OSS는 헤더 기반 SSO(auth.proxy)를 지원하지 않으므로(Enterprise 전용), ERP 인증된 사용자가
이 엔드포인트를 거치면 **Metabase 공용계정 세션을 발급해 `metabase.SESSION` 쿠키로 주입**하고 /erp/mb/ 로
리다이렉트한다 → 별도 Metabase 로그인 없이 진입. 접근 자체는 nginx /erp/* 게이트(ERP 세션)로 제한된다.

⚠ 공용계정(shared) 모델 — Metabase 내 사용자 식별은 단일. 운영 시 per-user 필요하면 Enterprise SSO 검토.
"""
import logging

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from erp.common.policy import BI_ROLES as _BI_ROLES
from erp.common.router import CurrentUser, get_current_user
from erp.config import get_settings
from erp.errors import ForbiddenError, ValidationError

logger = logging.getLogger("erp.integrations.bi_sso")
router = APIRouter(prefix="/integrations/bi", tags=["integrations"])

# 셀프서비스 BI 접근 역할 = 공용 BI_ROLES(policy) — Grafana(auth/bi-check)·사이드바와 동일 기준.


@router.get("/metabase-login")
async def metabase_login(user: CurrentUser = Depends(get_current_user)):
    """ERP 인증 사용자 → Metabase 공용계정 세션 발급 → metabase.SESSION 쿠키 주입 → /erp/mb/ 리다이렉트.

    ⚠ 인가 게이트(전체검토 2026-07-03): 공용(shared) 세션 발급이라 인증만으론 부족 — 경영 역할만
    허용(사이드바 노출 조건과 대칭). 그 외 역할은 403(임의 사용자의 셀프서비스 BI 세션 획득 차단)."""
    if user.role not in _BI_ROLES:
        raise ForbiddenError("셀프서비스 BI 접근 권한이 없습니다(경영 역할 전용)")
    s = get_settings()
    if not (s.metabase_sso_user and s.metabase_sso_password):
        raise ValidationError("Metabase SSO 미설정(METABASE_SSO_USER/PASSWORD)")
    base = s.metabase_internal_base.rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(f"{base}/api/session",
                              json={"username": s.metabase_sso_user,
                                    "password": s.metabase_sso_password})
        r.raise_for_status()
        sid = r.json()["id"]
    logger.info("metabase SSO 세션 발급", extra={"ctx": {"erp_user": user.user_id}})
    resp = RedirectResponse(url="/erp/mb/", status_code=302)
    # Metabase 세션 쿠키 — /erp/mb/ 하위에만(httpOnly·Secure·Lax). 브라우저가 /erp/mb/* 요청에 전달.
    resp.set_cookie("metabase.SESSION", sid, httponly=True, secure=True,
                    samesite="lax", path="/erp/mb")
    return resp
