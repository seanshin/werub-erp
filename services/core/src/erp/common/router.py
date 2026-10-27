"""인증·알림 라우터 — design §4.3."""
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.auth_service import AuthService
from erp.common.models import Notification, User
from erp.common.redis_client import get_redis
from erp.common.security import decode_access_token
from erp.common.sso_service import HisSsoService
from erp.config import get_settings
from erp.db import get_session
from erp.errors import ForbiddenError, TokenError

router = APIRouter(tags=["common"])
_auth = AuthService()
_sso = HisSsoService(redis=get_redis())  # SSO jti 재생 방지(C-3)에 Redis 사용


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class HisSsoRequest(BaseModel):
    token: str   # HIS가 발급한 JWT (his.example.org 로그인 후 핸드오프)


class RefreshRequest(BaseModel):
    refresh_token: str


# 작업대 출입증 허용 EP — 작업대 화면(apps/web /ws·inventory-workstation)이 실제로 부르는 것만.
# /inventory 전체를 열면 출입증 발급·폐기(scm.items write), NRV·원가흐름 변경까지 열렸다(2차 검토 G).
_WS_GET = re.compile(r"^/api/v1/inventory/(items|items/lookup|expiring|shortage|purchase-orders)/?$")
_WS_POST = re.compile(
    r"^/api/v1/inventory/(purchase-orders/\d+/receive|movements/receive|movements/issue|"
    r"narcotics/entries|stock-takes|stock-takes/\d+/apply)/?$")


def _ws_allowed(method: str, path: str) -> bool:
    """(메서드, 경로) 쌍으로 판정 — 경로만 보면 GET 전용인 /inventory/items에 POST(품목 마스터 생성)가
    열렸다(3차 검토 G)."""
    if method in ("GET", "HEAD"):
        return bool(_WS_GET.match(path))
    if method == "POST":
        return bool(_WS_POST.match(path))
    return False


class CurrentUser(BaseModel):
    user_id: int
    role: str
    dept_id: int | None
    license_flag: bool


def _csrf_check(request: Request) -> None:
    """쿠키 인증 + 상태변경 요청 CSRF 방어 (H-2): ① Origin 화이트리스트 ② double-submit 토큰.

    헤더 인증(Bearer)은 CSRF 불가라 제외. ② csrf_token 쿠키가 있으면 X-CSRF-Token 헤더
    일치 요구(쿠키 없으면 생략 — 기존 세션 무중단 롤아웃). trusted_hosts 미설정 시 ① 생략.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    from urllib.parse import urlparse

    s = get_settings()
    # ① Origin 화이트리스트
    allowed = {h.strip().lower() for h in s.trusted_hosts.split(",") if h.strip()}
    if allowed:
        wo = urlparse(s.web_origin).hostname
        if wo:
            allowed.add(wo.lower())
        origin = request.headers.get("origin")
        oh = urlparse(origin).hostname if origin else None
        if oh is not None and oh.lower() not in allowed:
            raise ForbiddenError("교차 출처 요청이 차단되었습니다 (CSRF)")
    # ② double-submit 토큰 (csrf 쿠키 ↔ 헤더 일치)
    csrf_cookie = request.cookies.get("csrf_token")
    if csrf_cookie and request.headers.get("x-csrf-token") != csrf_cookie:
        raise ForbiddenError("CSRF 토큰이 일치하지 않습니다")


async def get_current_user(
    request: Request,
    authorization: str = Header(default=""),
    access_token: str | None = Cookie(default=None),
    session: AsyncSession = Depends(get_session),
) -> CurrentUser:
    """인증 dependency — token_version 검증 (Redis 우선, design §6.1).

    토큰 출처: Authorization 헤더(API 클라이언트) 우선, 없으면 access_token 쿠키(SSO 브라우저).
    쿠키 인증 시 상태변경 요청은 Origin 검증(H-2).
    """
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
    elif access_token:
        token = access_token
        _csrf_check(request)  # 쿠키 세션 — CSRF Origin 검증
    else:
        raise TokenError("인증이 필요합니다")
    payload = decode_access_token(token)
    # 워크스테이션 일일 출입증 — User 행 없음(tv 우회). 폐기/만료를 매 요청 DB로 확인(폐기 실효화).
    if payload.get("role") == "WORKSTATION":
        from datetime import datetime, timezone

        from erp.common.models import WorkstationPass
        wp = await session.get(WorkstationPass, int(payload["sub"]))
        if wp is None or wp.revoked or wp.valid_until <= datetime.now(timezone.utc):
            raise TokenError("출입증이 폐기되었거나 만료되었습니다 — 다시 입장하세요")
        # 🔴 출입증 세션은 재고 작업대 경로에서만 쓴다(2026-10-09 전체 흐름 검토 G·CRITICAL).
        #    sub=출입증 id인데 CurrentUser.user_id로 들어가, scope 확인 없이 user_id로 users를 읽고
        #    쓰는 본인용 EP(/auth/pin·/auth/passkeys·/auth/me·/notifications·/hr/payrolls/me 등)에서
        #    **같은 번호의 사용자 계정으로 행세**했다 — PIN 교체+패스키 등록으로 그 사용자의 2FA 세션까지.
        #    작업대 화면이 부르는 API는 전부 /inventory 아래다. 그 밖은 403(로그아웃 포함 — 로그아웃은
        #    user_id의 refresh를 폐기해 남의 세션을 끊는다).
        path = request.url.path
        if not _ws_allowed(request.method, path):
            from erp.errors import ForbiddenError
            raise ForbiddenError("작업대 출입증으로는 재고 작업대 기능만 사용할 수 있습니다")
        # user_id는 **음수(-출입증 id)** — 사용자 id와 절대 겹치지 않게. created_by·마약류 취급자·감사 actor에
        # 출입증 id가 그대로 들어가 같은 번호의 사용자(예: admin)가 한 것으로 기록됐다(2차 검토 G).
        return CurrentUser(user_id=-int(payload["sub"]), role="WORKSTATION",
                           dept_id=None, license_flag=True)
    user_id = int(payload["sub"])
    current_tv = await _auth.current_token_version(session, user_id=user_id)
    if payload.get("tv") != current_tv:
        raise TokenError("세션이 만료되었습니다 — 다시 로그인하세요")
    return CurrentUser(user_id=user_id, role=payload["role"],
                       dept_id=payload.get("dept"), license_flag=payload.get("lic", True))


@router.post("/auth/login")
async def login(request: Request, response: Response, body: LoginRequest,
                session: AsyncSession = Depends(get_session)):
    """비밀번호 로그인 — JSON 토큰 반환(API 클라이언트) + httpOnly 쿠키 설정(브라우저 세션)."""
    tokens = await _auth.login(session, email=body.email, password=body.password)
    set_session_cookies(response, tokens, request)
    return tokens


@router.post("/auth/sso/his")
async def sso_his(body: HisSsoRequest, session: AsyncSession = Depends(get_session)):
    """HIS SSO (API) — HIS 발급 JWT를 검증해 ERP 세션 토큰 JSON 발급 (HIS 단일 신원)."""
    return await _sso.login_with_his_token(session, his_token=body.token)


# ── PIN + 패스키(생체) 2FA — HIS SSO와 별개 로그인 ──────────────────
class PinBody(BaseModel):
    pin: str


class TwoFaBeginBody(BaseModel):
    email: EmailStr
    pin: str


class PasskeyRegisterBody(BaseModel):
    token: str
    credential: dict
    name: str | None = None


class TwoFaCompleteBody(BaseModel):
    token: str
    credential: dict


def _2fa():
    from erp.common.auth2fa_service import TwoFactorService
    return TwoFactorService()


@router.post("/auth/pin")
async def set_pin(body: PinBody, user: CurrentUser = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)):
    """PIN 설정/변경 (로그인 사용자) — 2FA 1단계."""
    await _2fa().set_pin(session, user_id=user.user_id, pin=body.pin)
    return {"ok": True}


@router.get("/auth/passkeys")
async def list_passkeys(user: CurrentUser = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    """내 패스키 목록."""
    from erp.common.models import UserCredential
    rows = (await session.execute(
        select(UserCredential).where(UserCredential.user_id == user.user_id)
        .order_by(UserCredential.id.desc()))).scalars().all()
    return {"items": [{"id": c.id, "name": c.name, "created_at": c.created_at,
                       "last_used_at": c.last_used_at} for c in rows]}


@router.post("/auth/passkeys/register/begin")
async def passkey_register_begin(user: CurrentUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session)):
    """패스키 등록 시작 — 등록 옵션 + 서명 토큰 반환(브라우저가 생체로 생성)."""
    from erp.common.models import User
    u = await session.get(User, user.user_id)
    return await _2fa().register_begin(session, user=u)


@router.post("/auth/passkeys/register/complete")
async def passkey_register_complete(body: PasskeyRegisterBody,
                                    user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """패스키 등록 완료 — 생체 생성 결과 검증·저장."""
    from erp.common.models import User
    u = await session.get(User, user.user_id)
    cred = await _2fa().register_complete(session, user=u, token=body.token,
                                          credential=body.credential, name=body.name)
    return {"id": cred.id, "name": cred.name}


@router.delete("/auth/passkeys/{cred_id}")
async def passkey_delete(cred_id: int, user: CurrentUser = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    """패스키 삭제 (본인 것만)."""
    from erp.common.models import UserCredential
    c = await session.get(UserCredential, cred_id)
    if c is None or c.user_id != user.user_id:
        raise ForbiddenError()
    await session.delete(c)
    return {"id": cred_id, "deleted": True}


@router.post("/auth/2fa/begin")
async def two_fa_begin(body: TwoFaBeginBody, session: AsyncSession = Depends(get_session)):
    """2FA 1단계 — email+PIN 검증 후 패스키 인증 옵션 발급. PIN 통과해야만 토큰 발급."""
    from datetime import datetime, timedelta, timezone

    from erp.common.models import User
    if not get_settings().pin_2fa_enabled:
        raise ForbiddenError()
    user = (await session.execute(
        select(User).where(User.email == body.email, User.is_active == True)  # noqa: E712
    )).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    locked = user and user.locked_until and user.locked_until > now
    if user is None:
        from erp.common import security
        security.dummy_verify(body.pin)      # 타이밍 균일화(사용자 열거 방지)
    # 어느 요소 실패인지 노출 금지 — 일반 오류
    if user is None or locked or not _2fa().verify_pin(user, body.pin):
        if user is not None and not locked:
            s = get_settings()
            user.failed_attempts += 1
            if user.failed_attempts >= s.login_max_failures:
                user.locked_until = now + timedelta(minutes=s.login_lockout_minutes)
                user.failed_attempts = 0
        raise TokenError()
    user.failed_attempts = 0
    return await _2fa().auth_begin(session, user=user)


@router.post("/auth/2fa/complete")
async def two_fa_complete(body: TwoFaCompleteBody, request: Request, response: Response,
                          session: AsyncSession = Depends(get_session)):
    """2FA 2단계 — 패스키(생체) 검증 통과 시 ERP 세션 발급."""
    import uuid
    if not get_settings().pin_2fa_enabled:
        raise ForbiddenError()
    user = await _2fa().auth_complete(session, token=body.token, assertion=body.credential)
    tokens = await _auth._issue_tokens(session, user, family_id=str(uuid.uuid4()))
    set_session_cookies(response, tokens, request)
    return tokens


def external_base(request: Request) -> str:
    """리버스 프록시 뒤의 도달 가능한 베이스 URL — 신뢰 호스트만 사용(H-1 open redirect 방어).

    Host가 trusted_hosts(+web_origin host)에 있을 때만 헤더 기반 구성, 아니면 web_origin 강제.
    trusted_hosts 미설정 시 현행 동작(하위호환) — 운영에선 반드시 설정.
    """
    from urllib.parse import urlparse

    s = get_settings()
    host = request.headers.get("host")

    def _build() -> str:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
        return f"{proto}://{host}{prefix}"

    allowed = {h.strip().lower() for h in s.trusted_hosts.split(",") if h.strip()}
    if not allowed:
        # trusted_hosts 미설정 → 현행(헤더 신뢰, 하위호환). 운영에선 반드시 설정.
        return _build() if host else s.web_origin.rstrip("/")
    wo_host = urlparse(s.web_origin).hostname  # web_origin host도 허용
    if wo_host:
        allowed.add(wo_host.lower())
    if host and host.split(":")[0].lower() in allowed:
        return _build()
    return s.web_origin.rstrip("/")  # 미신뢰 호스트 → web_origin 강제


def set_session_cookies(resp, tokens: dict, request: Request) -> None:
    """ERP 세션 토큰을 httpOnly 쿠키로 설정 (프록시 프리픽스 반영) — 리시버·랜딩 공용."""
    import secrets

    s = get_settings()
    secure = s.env == "prod"
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    resp.set_cookie("access_token", tokens["access_token"], httponly=True, secure=secure,
                    samesite="lax", max_age=s.access_token_minutes * 60, path=f"{prefix}/" or "/")
    resp.set_cookie("refresh_token", tokens["refresh_token"], httponly=True, secure=secure,
                    samesite="lax", max_age=s.refresh_token_days * 86400,
                    path=f"{prefix}/api/v1/auth")
    # CSRF double-submit 토큰 — JS 읽기 가능(httpOnly 아님), 클라이언트가 X-CSRF-Token로 재제출
    resp.set_cookie("csrf_token", secrets.token_hex(16), httponly=False, secure=secure,
                    samesite="lax", max_age=s.access_token_minutes * 60, path=f"{prefix}/" or "/")


async def exchange_his_sso(session: AsyncSession, his_sso_token: str,
                           request: Request) -> RedirectResponse:
    """방식 C — HIS 동일호스트 핸드오프 쿠키(his_sso) 소비 → ERP 세션 발급 → his_sso 삭제.

    검증은 token 모드와 동일(HisSsoService). 1회 소비 후 his_sso는 Max-Age=0으로 삭제.
    """
    tokens = await _sso.login_with_his_token(session, his_token=his_sso_token, mode="cookie")
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    resp = RedirectResponse(url=f"{external_base(request)}/dashboard", status_code=302)
    set_session_cookies(resp, tokens, request)
    resp.delete_cookie("his_sso", path=prefix or "/erp")
    return resp


@router.get("/auth/sso/his")
async def sso_his_landing(token: str, request: Request,
                          session: AsyncSession = Depends(get_session)):
    """HIS SSO (브라우저 랜딩, token 모드) — 포털 핸드오프 링크 수신.

    HIS 포털 'ERP' 버튼 → GET {prefix}/api/v1/auth/sso/his?token=<HIS JWT> → 검증 →
    ERP 토큰을 httpOnly 쿠키로 심고 도달 가능한 ERP 루트로 리다이렉트(프록시 프리픽스 반영).
    동일호스트 쿠키 모드(his_sso)는 GET / 랜딩에서 소비 (main.landing).
    """
    tokens = await _sso.login_with_his_token(session, his_token=token)
    resp = RedirectResponse(url=f"{external_base(request)}/dashboard", status_code=302)
    set_session_cookies(resp, tokens, request)
    return resp


@router.get("/auth/me")
async def me(response: Response,
             user: CurrentUser = Depends(get_current_user),
             session: AsyncSession = Depends(get_session)):
    """현재 로그인 사용자 — 쿠키/헤더 인증. 랜딩·프론트 사용자 표시용.

    응답 헤더 X-Erp-User/X-Erp-Role = BI(Grafana) auth.proxy SSO용(사용자별 계정·역할 주입).
    ⚠ **Grafana auth_request 게이트는 /auth/bi-check로 이전**(재검토) — 이 EP는 인증만 확인(전 역할 200)해
    프론트 사용자 표시에 쓰이므로, nginx /erp/bi/_auth는 /auth/bi-check(BI 역할만 200)를 호출해야 함.
    역할 매핑: ADMIN→Admin·MANAGER/DIRECTOR/FINANCE_MANAGER→Editor·그외→Viewer.
    """
    email = (await session.execute(
        select(User.email).where(User.id == user.user_id))).scalar_one_or_none()
    response.headers["X-Erp-User"] = email or f"erp-{user.user_id}"
    response.headers["X-Erp-Role"] = _grafana_role(user.role)
    return {"user_id": user.user_id, "email": email, "role": user.role,
            "dept_id": user.dept_id, "license_flag": user.license_flag}


def _grafana_role(role: str) -> str:
    return {"ADMIN": "Admin", "MANAGER": "Editor", "DIRECTOR": "Editor",
            "FINANCE_MANAGER": "Editor"}.get(role, "Viewer")


@router.get("/auth/bi-check")
async def bi_check(response: Response,
                   user: CurrentUser = Depends(get_current_user),
                   session: AsyncSession = Depends(get_session)):
    """경영 BI(Grafana) auth_request 게이트 — **BI 역할만 200**, 그 외 403(재검토·인가 공백 보완).

    nginx /erp/bi/_auth가 이 EP를 auth_request로 호출 → 403이면 nginx가 /erp/bi/ 접근 차단.
    (기존엔 /auth/me를 사용해 인증만 되면 누구나 Grafana Viewer 접근 가능하던 공백을 닫음.)
    """
    from erp.common.policy import BI_ROLES
    if user.role not in BI_ROLES:
        raise ForbiddenError("경영 BI 접근 권한이 없습니다")
    email = (await session.execute(
        select(User.email).where(User.id == user.user_id))).scalar_one_or_none()
    response.headers["X-Erp-User"] = email or f"erp-{user.user_id}"
    response.headers["X-Erp-Role"] = _grafana_role(user.role)
    return {"ok": True}


@router.post("/auth/refresh")
async def refresh(request: Request, response: Response,
                  body: RefreshRequest | None = None,
                  refresh_token: str | None = Cookie(default=None),
                  session: AsyncSession = Depends(get_session)):
    """refresh 회전 — 토큰 출처: 본문(API 클라이언트) 우선, 없으면 refresh_token 쿠키(SSO 브라우저).

    쿠키 세션이면 새 토큰을 다시 httpOnly 쿠키로 갱신(세션 연장)."""
    raw = (body.refresh_token if body else None) or refresh_token
    if not raw:
        raise TokenError("refresh 토큰이 필요합니다")
    tokens = await _auth.refresh(session, raw_refresh=raw)
    if refresh_token and not (body and body.refresh_token):
        set_session_cookies(response, tokens, request)  # 쿠키 세션 — 쿠키로 갱신
    return tokens


@router.post("/auth/logout")
async def logout(request: Request, response: Response,
                 user: CurrentUser = Depends(get_current_user),
                 session: AsyncSession = Depends(get_session)):
    await _auth.logout(session, user_id=user.user_id)
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    response.delete_cookie("access_token", path=f"{prefix}/" or "/")
    response.delete_cookie("refresh_token", path=f"{prefix}/api/v1/auth")
    return {"ok": True}


@router.get("/menu")
async def my_menu(user: CurrentUser = Depends(get_current_user),
                  session: AsyncSession = Depends(get_session)):
    """현재 역할이 볼 메뉴(그룹·항목) — DB 오버라이드 반영. 프론트 사이드바 렌더용."""
    from erp.common.menu_service import MenuService
    return await MenuService().menu_for(session, role=user.role)


@router.get("/notifications")
async def list_notifications(user: CurrentUser = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session),
                             page: int = 1, size: int = 50):
    size = min(size, 200)
    rows = (await session.execute(
        select(Notification).where(Notification.user_id == user.user_id)
        .order_by(Notification.created_at.desc())
        .offset((page - 1) * size).limit(size)
    )).scalars().all()
    return {"items": [
        {"id": n.id, "type": n.type, "title": n.title, "body": n.body,
         "href": n.href, "read_at": n.read_at, "created_at": n.created_at} for n in rows
    ], "page": page, "size": size}


@router.get("/notifications/unread-count")
async def unread_notification_count(user: CurrentUser = Depends(get_current_user),
                                    session: AsyncSession = Depends(get_session)):
    """미읽음 알림 수 — 헤더 뱃지용(FR-26)."""
    from sqlalchemy import func
    n = await session.scalar(
        select(func.count()).select_from(Notification)
        .where(Notification.user_id == user.user_id, Notification.read_at.is_(None)))
    return {"unread": int(n or 0)}


@router.patch("/notifications/{notification_id}/read")
async def mark_read(notification_id: int,
                    user: CurrentUser = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    await session.execute(
        update(Notification)
        .where(Notification.id == notification_id, Notification.user_id == user.user_id)
        .values(read_at=datetime.now(timezone.utc))
    )
    return {"ok": True}
