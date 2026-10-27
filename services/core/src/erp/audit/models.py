"""audit 스키마 — append-only (DB 레벨 REVOKE는 0001 마이그레이션, design §3.4)."""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from erp.db import Base, BigIntPK


class AuditLog(Base):
    """민감 데이터 변경 이력 — 누가/언제/무엇을 (NFR Audit ①)."""
    __tablename__ = "audit_log"
    __table_args__ = {"schema": "audit"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    actor: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(32))   # CREATE|UPDATE|DELETE|APPROVE|REPROCESS...
    entity: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccessLog(Base):
    """민감정보 조회·반출 접속기록 — 법정 2년 보존 (NFR Audit ②④)."""
    __tablename__ = "access_log"
    __table_args__ = {"schema": "audit"}

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    actor: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(16))   # VIEW | EXPORT | PRINT
    entity: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64))
    route: Mapped[str | None] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
