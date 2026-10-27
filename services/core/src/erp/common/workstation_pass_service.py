"""재고 워크스테이션 일일 출입증(알바) — 당일 PIN 발급·검증.

관리자가 PIN 발급 → 알바가 /erp/ws에서 PIN 입력 → WORKSTATION 제한 세션(당일 만료).
전체 ERP 접근 불가(policy: scm.items/po r·w·narcotics w만). PIN 본문은 hash만 저장.
"""
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common import security
from erp.common.models import WorkstationPass
from erp.config import get_settings
from erp.errors import TokenError, ValidationError
from erp.common.kst import kst_iso

_KST = ZoneInfo("Asia/Seoul")


def _end_of_today() -> datetime:
    now = datetime.now(_KST)
    return now.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(timezone.utc)


class WorkstationPassService:
    async def create(self, session: AsyncSession, *, actor: int | None,
                     label: str | None = None) -> tuple[WorkstationPass, str]:
        """일일 출입증 발급 — 6자리 PIN 반환(1회 노출, hash만 저장)·당일 23:59 만료."""
        pin = f"{secrets.randbelow(1_000_000):06d}"
        wp = WorkstationPass(pin_hash=security.hash_password(pin), label=label,
                             created_by=actor, valid_until=_end_of_today())
        session.add(wp)
        await session.flush()
        return wp, pin

    async def _active(self, session: AsyncSession) -> list[WorkstationPass]:
        now = datetime.now(timezone.utc)
        return list((await session.execute(
            select(WorkstationPass).where(WorkstationPass.revoked.is_(False),
                                          WorkstationPass.valid_until > now))).scalars())

    async def login(self, session: AsyncSession, *, pin: str) -> dict:
        """PIN 검증 → WORKSTATION 제한 토큰(당일 만료) 발급. 어느 출입증인지 비노출."""
        for wp in await self._active(session):
            if security.verify_password(pin, wp.pin_hash):
                now = datetime.now(timezone.utc)
                token = jwt.encode({"sub": str(wp.id), "role": "WORKSTATION", "tv": 0,
                                    "iat": now, "exp": wp.valid_until},
                                   get_settings().jwt_secret, algorithm="HS256")
                return {"access_token": token, "valid_until": kst_iso(wp.valid_until)}
        raise TokenError()

    async def list_active(self, session: AsyncSession) -> list[dict]:
        return [{"id": w.id, "label": w.label, "valid_until": w.valid_until,
                 "created_at": w.created_at} for w in await self._active(session)]

    async def revoke(self, session: AsyncSession, *, pass_id: int) -> None:
        wp = await session.get(WorkstationPass, pass_id)
        if wp is None:
            raise ValidationError("존재하지 않는 출입증입니다")
        wp.revoked = True
