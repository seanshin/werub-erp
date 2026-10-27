"""HIS SSO — HIS(Hospital RUN)를 신원 제공자로 ERP 로그인 (design §6.1 확장).

흐름: HIS 로그인 → HIS JWT(HS256, AUTH_SECRET) → ERP가 서명 검증(오프라인) →
이메일 기준 common.users JIT 프로비저닝(역할 매핑) → ERP 자체 JWT 발급.
ERP는 비밀번호를 저장하지 않음(HIS 단일 신원). 기존 로그인의 토큰 발급(_issue_tokens) 재사용.

역할 매핑(최소 권한): HIS ADMIN→ERP ADMIN, 그 외 전원→STAFF.
DIRECTOR/MANAGER는 운영자가 추후 수동 승격(기존 사용자 역할은 SSO가 덮어쓰지 않음).
"""
import logging
import secrets
import uuid

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common import security
from erp.common.auth_service import AuthService
from erp.common.models import User
from erp.config import get_settings
from erp.errors import AuthFailedError, ValidationError

logger = logging.getLogger("erp.common.sso")


class SsoReplayError(AuthFailedError):
    """같은 핸드오프 토큰(jti)의 재사용 — 프리페치·이중요청.

    consumed_by = 그 토큰으로 **실제 세션을 발급받은** user_id(문자열). 직전 요청이 실패했으면 None.
    랜딩은 기존 세션의 사용자와 이것이 같을 때만 통과시킨다(G3 — 직전 요청이 실패했거나 다른
    사용자였는데 남아 있던 이전 세션으로 들여보내던 문제).
    """
    def __init__(self, consumed_by: str | None = None):
        super().__init__("이미 사용된 SSO 토큰입니다 (재생 차단)")
        self.consumed_by = consumed_by


class SsoTokenModeBlockedError(AuthFailedError):
    def __init__(self):
        super().__init__("ERP 관리자가 token 방식 SSO를 차단했습니다 — HIS 관리자에게 ERP 연동 "
                         "핸드오프 방식을 cookie로 바꾸도록 요청하거나, ERP 관리자에게 차단 해제를 요청하세요")


def map_his_role(his_role: str | None) -> str:
    """HIS 역할 → ERP 역할 (최소 권한)."""
    return "ADMIN" if (his_role or "").upper() == "ADMIN" else "STAFF"


class HisSsoService:
    def __init__(self, redis=None):
        self._auth = AuthService(redis=redis)
        self._redis = redis  # SSO jti 재생 방지(C-3)용

    async def _consumer_of(self, jti: str) -> str | None:
        """jti를 소비한 user_id — 첫 요청이 아직 처리 중('-')이면 최대 ~1초 기다린다.

        프리페치와 실제 탐색이 동시에 오면 두 번째가 첫 번째보다 먼저 끝날 수 있다. 기다리지 않으면
        '실패'로 보고 쿠키를 지워, 첫 요청이 방금 심은 세션까지 지운다.
        """
        import asyncio
        for _ in range(5):
            try:
                v = await self._redis.get(f"sso:jti:{jti}")
            except Exception:  # noqa: BLE001
                return None
            v = v.decode() if isinstance(v, bytes) else v
            if v and v != "-":
                return v
            await asyncio.sleep(0.2)
        return None

    async def login_with_his_token(self, session: AsyncSession, *, his_token: str,
                                   mode: str = "token") -> dict:
        """mode: 'cookie'(동일호스트 his_sso 핸드오프) | 'token'(URL·본문으로 받은 토큰)."""
        s = get_settings()
        if not s.his_jwt_secret:
            raise ValidationError("HIS SSO가 구성되지 않았습니다 (HIS_JWT_SECRET 필요)")
        from erp.common.org_service import sso_policy
        pol = await sso_policy(session)
        if mode == "token" and not pol["token_allowed"]:
            # 행위 주체+조치 명시 — HIS 설정을 바꿔야 풀린다(ERP를 고칠 일이 아니다).
            raise SsoTokenModeBlockedError()

        # ① HIS JWT 서명·만료 검증 (오프라인 — 공유 AUTH_SECRET). exp 필수(H-4).
        # C-2: aud 강제(env HIS_JWT_AUDIENCE 또는 관리화면 결정) 시 aud+jti 필수 — jti 없는
        # HIS 메인 토큰의 반복 수락 차단. 미강제 시 종전 동작.
        aud = pol["audience"]
        require = ["exp", "aud", "jti"] if aud else ["exp"]
        try:
            claims = jwt.decode(his_token, s.his_jwt_secret, algorithms=["HS256"],
                                audience=aud, options={"verify_aud": bool(aud), "require": require})
        except jwt.PyJWTError as exc:
            raise AuthFailedError("HIS 토큰이 유효하지 않습니다") from exc

        # C-3: jti 재생 방지 — Redis NX 1회 소비(토큰 exp~60s + 버퍼). redis 불가 시 검사 생략(가용성).
        jti = claims.get("jti")
        if jti and self._redis is not None:
            try:
                fresh = await self._redis.set(f"sso:jti:{jti}", "-", nx=True, ex=120)
            except Exception as exc:  # noqa: BLE001 — redis 장애 시 SSO 차단하지 않음
                logger.warning("jti 재생검사 생략(redis 불가)",
                               extra={"ctx": {"err": str(exc)[:120]}})
                fresh = True
            if not fresh:
                raise SsoReplayError(await self._consumer_of(jti))

        email = claims.get("email")
        if not email:
            raise AuthFailedError("HIS 토큰에 이메일이 없습니다")
        # 허용 이메일 도메인 검증 (C-1) — 미설정 시 전체 허용
        allowed = {d.strip().lower() for d in s.sso_allowed_email_domains.split(",") if d.strip()}
        if allowed and email.split("@")[-1].lower() not in allowed:
            raise AuthFailedError("허용되지 않은 이메일 도메인입니다")
        his_role = claims.get("role")

        # ② JIT 프로비저닝 — 이메일 키. 기존 사용자는 역할 보존(수동 승격 유지)
        # 대소문자 무관 조회(G4) — 대소문자만 다른 이메일이 오면 차단된 계정을 못 찾고 새 활성 계정을
        # 만들던 차단 우회. 퇴사 차단·직원 연결과 같은 비교 규칙.
        from sqlalchemy import func
        matches = (await session.execute(
            select(User).where(func.lower(User.email) == email.strip().lower())
            .order_by(User.id))).scalars().all()
        # fail-closed: 대소문자만 다른 행이 여럿이고 그중 하나라도 비활성이면 거부(H2) — 종전 우회로 생긴
        # 활성 중복 행이 남아 있으면 어느 행이 골라지느냐에 따라 차단이 풀렸다(PG 행 순서 비결정).
        if any(not m.is_active for m in matches):
            raise AuthFailedError(
                "비활성 계정입니다" if len(matches) == 1 else
                "대소문자만 다른 중복 계정이 있어 로그인할 수 없습니다 — ERP 관리자가 사용자 관리에서 "
                "중복 계정을 정리해야 합니다")
        user = matches[0] if matches else None
        await self._assert_not_resigned(session, user=user, email=email)
        if user is None:
            user = User(
                email=email, role=map_his_role(his_role),
                # SSO 사용자는 비밀번호 로그인 불가 — 알 수 없는 무작위 해시
                password_hash=security.hash_password(secrets.token_urlsafe(32)),
                is_active=True)
            session.add(user)
            await session.flush()
            from erp.common.crypto import mask_email
            logger.info("HIS SSO 신규 사용자 생성",
                        extra={"ctx": {"email": mask_email(email), "role": user.role}})
        # JIT/기존 공통 — 직원 연결 (W3.1 ④). 규칙은 common.identity 단일 소스: 재직 행이 정확히 1명일
        # 때만 연결, 연결이 퇴사 행이면 같은 이메일의 재직 행으로 다시 연결(재입사·5차 검토).
        from erp.common.identity import resolve_link
        new_link = await resolve_link(session, current_employee_id=user.employee_id, email=email)
        if new_link != user.employee_id:
            from erp.common.crypto import mask_email
            logger.info("SSO 직원 연결", extra={"ctx": {
                "email": mask_email(email), "employee_id": new_link, "previous": user.employee_id}})
            user.employee_id = new_link

        # ③ ERP 자체 토큰 발급 (기존 로그인과 동일 경로)
        user.failed_attempts = 0
        user.locked_until = None
        tokens = await self._auth._issue_tokens(session, user, family_id=str(uuid.uuid4()))
        # 이 토큰을 누가 소비했는지 기록(발급 성공 뒤) — 재사용 판정이 사용자를 대조할 수 있게(G3)
        if jti and self._redis is not None:
            try:
                await self._redis.set(f"sso:jti:{jti}", str(user.id), ex=120)
            except Exception:  # noqa: BLE001
                pass
        return tokens

    async def _assert_not_resigned(self, session: AsyncSession, *, user, email: str) -> None:
        """퇴사자 SSO 진입 판정 — common.identity.resigned_block_reason(단일 소스). 신규 JIT는 연결
        예정 직원 기준, 기존 계정은 재입사 재연결을 반영한 연결 직원 기준."""
        from erp.common.identity import resigned_block_reason, resolve_link
        emp_id = await resolve_link(
            session, current_employee_id=user.employee_id if user is not None else None, email=email)
        reason = await resigned_block_reason(session, employee_id=emp_id, email=email,
                                             existing_user=user is not None)
        if reason:
            raise AuthFailedError(reason)
