"""수가 마스터 동기화 (FR-34) — design §3.2 동기화 배치 규칙.

① 코드별 "구 버전 닫기 + 신 버전 INSERT"는 단일 트랜잭션 (호출자 소유 — UoW)
② 멱등키: fee_sync_runs.source_version UNIQUE — 동일 버전 재실행 no-op
③ 비정상 대량 변동 시 자동 반영 보류 (NFR 외부 호출 보안 — 데이터 무결성)
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import DateTime, Integer, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from erp.db import Base, BigIntPK
from erp.errors import ValidationError
from erp.regulatory.models import FeeMaster

logger = logging.getLogger("erp.regulatory.sync")

ANOMALY_CHANGE_RATIO = 0.5      # 기존 활성 코드의 50% 초과 변경 시 보류
MIN_ACTIVE_FOR_ANOMALY = 20     # 모수가 작으면(초기 적재 등) 이상 변동 판정 제외


class FeeSyncRun(Base):
    __tablename__ = "fee_sync_runs"
    __table_args__ = {"schema": "regulatory"}

    run_id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    source_version: Mapped[str] = mapped_column(String(64), unique=True)  # 멱등키
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_count: Mapped[int | None] = mapped_column(Integer)


@dataclass
class FeeUpdate:
    code: str
    code_type: str
    name: str
    unit_price: float
    valid_from: date
    price_cap: float | None = None
    criteria_text: str | None = None


class FeeSyncService:
    async def apply(self, session: AsyncSession, *, source_version: str,
                    updates: list[FeeUpdate], force: bool = False) -> int:
        """동기화 적용 — 멱등 + close-then-open + 이상 변동 보류."""
        # ② 멱등: 이미 처리된 source_version이면 no-op
        existing = (await session.execute(
            select(FeeSyncRun).where(FeeSyncRun.source_version == source_version)
        )).scalar_one_or_none()
        if existing is not None:
            logger.info("fee_sync 멱등 스킵", extra={"ctx": {"source_version": source_version}})
            return 0

        # ③ 이상 변동 방어 — 활성 코드 대비 변경 비율
        active_count = (await session.execute(
            select(func.count()).select_from(FeeMaster).where(FeeMaster.valid_to.is_(None))
        )).scalar_one()
        if (not force and active_count >= MIN_ACTIVE_FOR_ANOMALY
                and len(updates) / active_count > ANOMALY_CHANGE_RATIO):
            raise ValidationError(
                "비정상 대량 변동 — 자동 반영 보류 (관리자 검토 후 force 재실행)",
                {"updates": len(updates), "active": active_count})

        applied = 0
        for u in updates:
            applied += await self._apply_one(session, u)

        session.add(FeeSyncRun(source_version=source_version,
                               finished_at=datetime.now(timezone.utc), row_count=applied))
        return applied

    async def _apply_one(self, session: AsyncSession, u: FeeUpdate) -> int:
        """① close-then-open — 동일 세션(단일 Tx) 내에서 처리."""
        current = (await session.execute(
            select(FeeMaster).where(
                FeeMaster.code == u.code, FeeMaster.code_type == u.code_type,
                FeeMaster.valid_to.is_(None))
        )).scalar_one_or_none()

        if current is not None:
            if current.valid_from >= u.valid_from:
                # 과거로 소급하는 신 버전은 수동 검토 대상 — 자동 반영 금지
                logger.warning("소급 버전 스킵", extra={"ctx": {"code": u.code,
                               "current_from": str(current.valid_from),
                               "new_from": str(u.valid_from)}})
                return 0
            if float(current.unit_price) == u.unit_price and current.name == u.name:
                return 0  # 변경 없음
            current.valid_to = u.valid_from  # 구 버전 닫기 (exclusive)

        session.add(FeeMaster(code=u.code, code_type=u.code_type, name=u.name,
                              unit_price=u.unit_price, price_cap=u.price_cap,
                              criteria_text=u.criteria_text,
                              valid_from=u.valid_from, valid_to=None, source="API"))
        return 1
