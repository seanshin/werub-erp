"""결재 불변 원장(블록체인식 해시체인) — append·검증·변조 탐지·문서별 로그."""

from datetime import datetime, timezone

from sqlalchemy import delete, select, update

from erp.common.approval_engine import ApprovalEngine
from erp.common.approval_ledger import ApprovalLedgerService, _signing_key
from erp.common.models import ApprovalLedger, ApprovalLedgerAnchor, ApprovalRule, User

engine = ApprovalEngine()
ledger = ApprovalLedgerService()


async def _flow(session, *, doc_id: int, reject: bool = False):
    """룰+유저 시드 → 상신 → 승인/반려 1단계."""
    if not await session.scalar(select(ApprovalRule).where(ApprovalRule.doc_type == "PO")):
        session.add(ApprovalRule(doc_type="PO", min_amount=0, max_amount=None,
                                 steps=[{"type": "ROLE", "value": "MANAGER"}], active=True))
    if await session.get(User, 11) is None:
        session.add_all([
            User(id=11, email="al-creator@x.example", password_hash="x", role="STAFF"),
            User(id=12, email="al-approver@x.example", password_hash="x", role="MANAGER"),
            User(id=13, email="al-director@x.example", password_hash="x", role="DIRECTOR")])
    await session.flush()
    inst = await engine.build(session, doc_type="PO", doc_id=doc_id, amount=1000,
                              created_by=11)
    await engine.act(session, doc_type="PO", doc_id=doc_id, actor=12,
                     approve=not reject, note="검토 완료")
    return inst


async def test_chain_appends_build_and_act(session):
    """상신(BUILD)·승인(APPROVE)이 전역 단조 체인으로 기록 — prev_hash 연결."""
    await _flow(session, doc_id=1)
    rows = (await session.scalars(select(ApprovalLedger)
                                  .order_by(ApprovalLedger.seq_no))).all()
    assert [r.action for r in rows] == ["BUILD", "APPROVE"]
    assert rows[0].seq_no == 1 and rows[0].prev_hash == "0" * 64
    assert rows[1].prev_hash == rows[0].row_hash          # 체인 링크
    assert (await ledger.verify(session))["ok"] is True


async def test_chain_records_reject_and_override(session):
    """반려(REJECT)·전권(OVERRIDE)도 원장에 기록."""
    await _flow(session, doc_id=2, reject=True)
    inst = await engine.build(session, doc_type="PO", doc_id=3, amount=500, created_by=11)
    await engine.act(session, doc_type="PO", doc_id=3, actor=13, approve=True,
                     override=True, note="긴급")
    actions = [r.action for r in (await session.scalars(
        select(ApprovalLedger).order_by(ApprovalLedger.seq_no))).all()]
    assert actions == ["BUILD", "REJECT", "BUILD", "OVERRIDE"]
    out = await ledger.verify(session)
    assert out["ok"] is True and out["entries"] == 4
    assert inst.status == "APPROVED"


async def test_chain_detects_tampering(session):
    """변조 탐지 — 저장 후 행 값을 바꾸면 verify가 최초 파손 지점 보고(sqlite로 UPDATE 시뮬레이션)."""
    await _flow(session, doc_id=4)
    await session.execute(update(ApprovalLedger).where(ApprovalLedger.seq_no == 2)
                          .values(actor=999))            # superuser 변조 시뮬레이션
    out = await ledger.verify(session)
    assert out["ok"] is False and out["first_broken_seq"] == 2
    assert out["reason"] == "행 내용 변조(서명 불일치)"


async def test_chain_detects_deletion(session):
    """행 삭제 탐지 — 중간 행 삭제 시 순서 교란으로 보고."""
    await _flow(session, doc_id=5)
    inst = await engine.build(session, doc_type="PO", doc_id=6, amount=100, created_by=11)
    assert inst is not None
    await session.execute(delete(ApprovalLedger).where(ApprovalLedger.seq_no == 2))
    out = await ledger.verify(session)
    assert out["ok"] is False and out["reason"] == "순서 교란/행 삭제"


async def test_hmac_forgery_detected(session):
    """HIGH-1 — 무키가 아닌 HMAC이라, 행을 바꾸고 이후 체인을 '재계산'해도 키 없이는 위조 불가.

    공격자가 서명키 없이 SHA-256(무키)으로 하류를 재작성한 상황을 시뮬레이션 → verify가 서명 불일치 탐지.
    """
    import hashlib

    await _flow(session, doc_id=9)                 # seq 1(BUILD), 2(APPROVE)
    rows = (await session.scalars(select(ApprovalLedger)
                                  .order_by(ApprovalLedger.seq_no))).all()
    # seq 2의 actor를 바꾸고, 키 없이 무키 SHA-256으로 row_hash 재계산(공격자 능력 가정)
    target = rows[1]
    forged = hashlib.sha256(f"{target.prev_hash}|forged".encode()).hexdigest()
    await session.execute(update(ApprovalLedger).where(ApprovalLedger.seq_no == 2)
                          .values(actor=999, row_hash=forged))
    out = await ledger.verify(session)
    assert out["ok"] is False                       # 키를 모르면 어떤 재작성도 서명 불일치
    assert out["first_broken_seq"] == 2


async def test_checkpoint_and_tail_truncation(session):
    """HIGH-2 — 앵커 봉인 후 최근 행을 삭제(꼬리 절단)하면 verify가 앵커로 탐지."""
    await _flow(session, doc_id=10)                 # seq 1,2
    await engine.build(session, doc_type="PO", doc_id=11, amount=50, created_by=11)  # seq 3
    cp = await ledger.checkpoint(session)
    assert cp["created"] is True and cp["entry_count"] == 3
    assert (await ledger.verify(session))["ok"] is True
    # 앵커 이후를 남기고 최근 행(seq 3)만 삭제 — 남은 1,2 체인은 유효하나 앵커 entry_count 미달
    await session.execute(delete(ApprovalLedger).where(ApprovalLedger.seq_no == 3))
    out = await ledger.verify(session)
    assert out["ok"] is False and out["reason"] == "꼬리 절단(앵커 이후 행 소실)"


async def test_anchor_tamper_detected(session):
    """앵커 자체 변조 — MAC 불일치로 탐지."""
    await _flow(session, doc_id=12)
    await ledger.checkpoint(session)
    await session.execute(update(ApprovalLedgerAnchor).values(entry_count=999))
    out = await ledger.verify(session)
    assert out["ok"] is False and out["reason"] == "앵커 변조"


async def test_checkpoint_skips_when_no_new_entries(session):
    """체크포인트 멱등 — 신규 엔트리 없으면 앵커 미생성."""
    await _flow(session, doc_id=13)
    assert (await ledger.checkpoint(session))["created"] is True
    assert (await ledger.checkpoint(session))["created"] is False
    assert _signing_key()                           # 서명키 로드 확인(폴백 파생)


async def test_entries_for_document(session):
    """문서별 원장 로그 — 해당 문서 항목만 시간순."""
    await _flow(session, doc_id=7)
    await _flow(session, doc_id=8)
    items = await ledger.entries_for(session, doc_type="PO", doc_id=7)
    assert [i["action"] for i in items] == ["BUILD", "APPROVE"]
    assert all(len(i["row_hash"]) == 64 for i in items)


# ── B안: sign 블록체인 시점봉인(자금결재 원장 앵커 외부 증명) ───────────────────────


async def test_verify_job_seals_new_anchor_to_sign(session, monkeypatch):
    """검증 잡 — 신규 앵커를 sign audit-events에 TSA 봉인 제출·영수증을 result에 기록."""
    from erp.app.recurring_handlers import approval_ledger_verify
    from erp.config import get_settings
    await _flow(session, doc_id=21)
    calls: dict = {}

    async def _fake_record(**kw):
        calls.update(kw)
        return {"entryHash": "e" * 64, "anchored": True,
                "anchor": {"headHash": "h" * 64, "tsGenTime": "2026-07-28T09:00:00Z"}}
    monkeypatch.setattr("erp.integrations.sign_client.record_audit_event", _fake_record)
    monkeypatch.setattr(get_settings(), "sign_api_key", "test-key", raising=False)
    out = await approval_ledger_verify(session, "2026-07-28", {})
    assert out["ok"] is True
    assert out["sign"] == {"entry_hash": "e" * 64, "anchored": True,
                           "ts_gen_time": "2026-07-28T09:00:00Z"}
    assert calls["anchor"] is True and calls["action"] == "LEDGER_ANCHOR"
    assert calls["data"]["head_hash"] and calls["data"]["seq_no"] >= 1   # 해시·시퀀스만(PHI 없음)
    # 재실행 — 신규 앵커 없음 → sign 제출 생략(중복 봉인 방지)
    out2 = await approval_ledger_verify(session, "2026-07-28", {})
    assert out2["ok"] is True and "sign" not in out2


async def test_sign_seal_failure_nonblocking(session, monkeypatch):
    """sign 불가(예외) — 원장 검증 자체는 유효(ok)·오류만 result에 기록(비차단)."""
    from erp.app.recurring_handlers import approval_ledger_verify
    from erp.config import get_settings

    async def _boom(**kw):
        raise RuntimeError("sign down")
    monkeypatch.setattr("erp.integrations.sign_client.record_audit_event", _boom)
    monkeypatch.setattr(get_settings(), "sign_api_key", "test-key", raising=False)
    await _flow(session, doc_id=22)
    out = await approval_ledger_verify(session, "2026-07-28", {})
    assert out["ok"] is True and out["sign"]["error"].startswith("sign down")


async def test_sign_seal_skipped_without_key(session, monkeypatch):
    """SIGN_API_KEY 미설정 — 봉인 생략 표기(개발/미연동 환경 안전)."""
    from erp.app.recurring_handlers import approval_ledger_verify
    from erp.config import get_settings
    monkeypatch.setattr(get_settings(), "sign_api_key", None, raising=False)
    await _flow(session, doc_id=23)
    out = await approval_ledger_verify(session, "2026-07-28", {})
    assert out["ok"] is True and out["sign"] == {"skipped": "SIGN_API_KEY 미설정"}


async def test_http_sign_log_endpoint(session, client, monkeypatch):
    """GET /approvals/ledger/sign-log — admin.audit read·sign 미설정 시 available=False·STAFF 403."""
    from tests._http import auth_as
    from erp.config import get_settings
    monkeypatch.setattr(get_settings(), "sign_api_key", None, raising=False)
    headers = await auth_as(session, user_id=31, role="ADMIN")
    r = await client.get("/api/v1/approvals/ledger/sign-log", headers=headers)
    assert r.status_code == 200 and r.json()["available"] is False
    staff = await auth_as(session, user_id=32, role="STAFF")
    assert (await client.get("/api/v1/approvals/ledger/sign-log",
                             headers=staff)).status_code == 403

    async def _fake_list(**kw):
        return {"data": [{"eventType": "LEDGER_ANCHOR", "occurredAt": "2026-07-28T09:00:00Z",
                          "entryHash": "e" * 64, "data": {"targetRef": "seq:5"}}]}

    async def _fake_verify(**kw):
        return {"valid": True, "chainValid": True, "anchored": True,
                "anchorTsValid": True, "anchorMatchesChain": True,
                "currentHead": "e" * 64, "anchoredAt": "2026-07-28T09:00:01Z"}
    monkeypatch.setattr("erp.integrations.sign_client.list_audit_events", _fake_list)
    monkeypatch.setattr("erp.integrations.sign_client.verify_audit_chain", _fake_verify)
    monkeypatch.setattr(get_settings(), "sign_api_key", "test-key", raising=False)
    r = await client.get("/api/v1/approvals/ledger/sign-log", headers=headers)
    body = r.json()
    assert r.status_code == 200 and body["available"] is True
    assert body["entries"][0]["data"]["targetRef"] == "seq:5" and body["verify"]["valid"] is True
    # sign ISO(UTC Z) → KST 벽시계 표기 정규화(v1.259.1)
    assert body["entries"][0]["occurredAt"] == "2026-07-28 18:00:00"
    assert body["verify"]["anchoredAt"] == "2026-07-28 18:00:01"


# ---- 외부 봉인 대조(적대검토 C-H1·C-H2·C-H3 회귀) ----

async def test_hash_at_matches_verify_head(session):
    """hash_at(마지막 seq) == verify head — 봉인값 대조의 기준이 성립하는가."""
    svc = ApprovalLedgerService()
    for i in range(3):
        await svc.append(session, doc_type="draft", doc_id=1, instance_id=1, step_no=i + 1,
                         action="APPROVE", actor=1, acted_at=datetime.now(timezone.utc))
    chk = await svc.verify(session)
    assert await svc.hash_at(session, chk["entries"]) == chk["head_hash"]


async def test_hash_at_detects_rewritten_past(session):
    """과거 구간 재작성 → 같은 seq의 해시가 달라진다(외부 봉인값과 불일치로 탐지)."""
    svc = ApprovalLedgerService()
    for i in range(3):
        await svc.append(session, doc_type="draft", doc_id=1, instance_id=1, step_no=i + 1,
                         action="APPROVE", actor=1, acted_at=datetime.now(timezone.utc))
    sealed_head = await svc.hash_at(session, 2)      # seq 2 시점이 외부에 봉인됐다고 가정
    row = await session.scalar(select(ApprovalLedger).where(ApprovalLedger.seq_no == 1))
    row.note = "재작성됨"                              # 키 보유자가 과거를 손댐
    await session.flush()
    assert await svc.hash_at(session, 2) != sealed_head


async def test_hash_at_none_when_sealed_seq_missing(session):
    """꼬리 절단 — 봉인된 지점이 아예 없으면 None(대조 불가 = 변조 신호)."""
    svc = ApprovalLedgerService()
    await svc.append(session, doc_type="draft", doc_id=1, instance_id=1, step_no=1,
                     action="APPROVE", actor=1, acted_at=datetime.now(timezone.utc))
    assert await svc.hash_at(session, 5) is None


async def test_checkpoint_returns_head_hash_of_anchor_row(session):
    """봉인 대상이 앵커 행의 값이어야 한다 — 바깥 verify 결과를 쓰면 경쟁 시 어긋난다(C-H3)."""
    svc = ApprovalLedgerService()
    await svc.append(session, doc_type="draft", doc_id=1, instance_id=1, step_no=1,
                     action="APPROVE", actor=1, acted_at=datetime.now(timezone.utc))
    anc = await svc.checkpoint(session)
    assert anc["created"] is True
    anchor = await session.scalar(select(ApprovalLedgerAnchor)
                                  .order_by(ApprovalLedgerAnchor.seq_no.desc()).limit(1))
    assert anc["head_hash"] == anchor.head_hash
