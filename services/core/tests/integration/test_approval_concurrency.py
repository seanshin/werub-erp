"""결재선 동시성 통합 테스트 (PostgreSQL 전용) — approval_engine.act() 행잠금(with_for_update).

sqlite는 FOR UPDATE를 무시해 이중승인(더블클릭/재시도) 경합을 재현 못 함. 실 PG에서 동일 인스턴스에
동시 승인 2건을 일으켜 (a) 정확히 1건만 APPROVED, (b) 나머지는 PENDING 소멸로 반려됨을 검증.
미검증 시 두 트랜잭션이 같은 PENDING 스냅샷을 읽어 마지막 단계를 둘 다 확정 → 후처리 이중적용(HIGH).

실행: RUN_PG_INTEGRATION=1 + DATABASE_URL=postgresql+asyncpg://... (마이그 head 적용).
"""
import asyncio
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from erp.common.approval_engine import ApprovalEngine
from erp.common.models import ApprovalInstance, ApprovalRule, User
from erp.errors import ValidationError

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_PG_INTEGRATION"),
    reason="PG 통합 테스트 — RUN_PG_INTEGRATION=1 + DATABASE_URL 필요")

engine = ApprovalEngine()
_DOC = "ITEST_APPROVAL"


@pytest.fixture
async def pg_sm():
    eng = create_async_engine(os.environ["DATABASE_URL"])
    sm = async_sessionmaker(eng, expire_on_commit=False)
    yield sm
    await eng.dispose()


async def _seed(sm) -> tuple[int, int, int]:
    """룰(단일 ROLE=MANAGER 단계) + 작성자·승인자 유저 → (doc_id, creator_id, approver_id)."""
    async with sm() as s:
        if not await s.scalar(select(ApprovalRule).where(ApprovalRule.doc_type == _DOC)):
            s.add(ApprovalRule(doc_type=_DOC, min_amount=0, max_amount=None,
                               steps=[{"type": "ROLE", "value": "MANAGER"}], active=True))
        creator = User(email="itest-appr-creator@x.example", password_hash="x", role="STAFF")
        approver = User(email="itest-appr-approver@x.example", password_hash="x", role="MANAGER")
        s.add_all([creator, approver])
        await s.flush()
        cid, aid = creator.id, approver.id
        # doc_id는 인스턴스 PK 충돌 회피용 유니크 값 — 기존 최대 doc_id+1
        maxid = await s.scalar(select(ApprovalInstance.doc_id).where(
            ApprovalInstance.doc_type == _DOC).order_by(ApprovalInstance.doc_id.desc()))
        doc_id = (maxid or 0) + 1
        inst = await engine.build(s, doc_type=_DOC, doc_id=doc_id, amount=1000,
                                  created_by=cid)
        assert inst is not None
        await s.commit()
        return doc_id, cid, aid


async def test_concurrent_approve_single_winner(pg_sm):
    """동시 승인 2건 → 정확히 1건만 APPROVED, 나머지는 반려(PENDING 소멸)."""
    doc_id, _cid, approver_id = await _seed(pg_sm)

    async def one():
        async with pg_sm() as s:
            inst = await engine.act(s, doc_type=_DOC, doc_id=doc_id, actor=approver_id,
                                    approve=True)
            await s.commit()
            return inst.status

    results = await asyncio.gather(*[one() for _ in range(2)], return_exceptions=True)
    approved = [r for r in results if r == "APPROVED"]
    rejected = [r for r in results if isinstance(r, ValidationError)]
    assert len(approved) == 1              # 승자 1
    assert len(rejected) == 1              # 패자 1 — "진행 중인 결재가 없습니다"

    async with pg_sm() as s:
        # 인스턴스는 최종 APPROVED 하나뿐 — 이중 확정 없음
        insts = (await s.scalars(select(ApprovalInstance).where(
            ApprovalInstance.doc_type == _DOC, ApprovalInstance.doc_id == doc_id))).all()
        assert len(insts) == 1 and insts[0].status == "APPROVED"
