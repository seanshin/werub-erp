"""재무 시뮬레이션 시나리오 CRUD — 사용자 소유 스코프(본인 것만).

기존 localStorage(기기 종속)를 서버 영속으로 전환. (user_id, name) 고유 → 동일 이름 upsert.
"""
import re

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.finance.models import SimScenario

# 제어문자·꺾쇠 제거(보고서/차트 직렬화 시 주입 방지) 후 길이 제한
_NAME_STRIP = re.compile(r"[<>\x00-\x1f]")


class SimScenarioService:
    async def list(self, session: AsyncSession, *, user_id: int) -> list[dict]:
        rows = (await session.execute(
            select(SimScenario).where(SimScenario.user_id == user_id)
            .order_by(SimScenario.updated_at.desc()))).scalars().all()
        return [{"id": r.id, "name": r.name, "drivers": r.drivers} for r in rows]

    async def upsert(self, session: AsyncSession, *, user_id: int, name: str,
                     drivers: dict) -> dict:
        name = _NAME_STRIP.sub("", (name or "").strip())[:80]
        if not name:
            raise ValueError("이름 필수")
        row = (await session.execute(
            select(SimScenario).where(SimScenario.user_id == user_id,
                                      SimScenario.name == name))).scalar_one_or_none()
        if row is None:
            row = SimScenario(user_id=user_id, name=name, drivers=drivers)
            session.add(row)
        else:
            row.drivers = drivers
        await session.flush()
        return {"id": row.id, "name": row.name, "drivers": row.drivers}

    async def delete(self, session: AsyncSession, *, user_id: int, sid: int) -> bool:
        res = await session.execute(
            delete(SimScenario).where(SimScenario.id == sid,
                                      SimScenario.user_id == user_id))
        return res.rowcount > 0
