"""HIS 웹훅 수신 게이트웨이 (마스터플랜 Wave 골격) — 인증·본문HMAC·멱등·도메인 검증."""
import hashlib
import hmac
import time

import pytest
from sqlalchemy import func, select

from fastapi import BackgroundTasks

from erp.config import get_settings
from erp.errors import ForbiddenError
from erp.integrations.his_webhook_router import (
    WebhookBody, his_webhook, inbound_signature_reason,
)
from erp.integrations.models import HisInboundEvent


class _Req:
    """직접호출용 fake Request — rawBody 반환(HMAC 검증 입력)."""
    def __init__(self, raw: bytes = b""):
        self._raw = raw

    async def body(self) -> bytes:
        return self._raw


_REQ = _Req()


def _body(**kw):
    return WebhookBody(**kw)


def _sign(secret: str, ts: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()


async def _with_key(session, key, fn):
    s = get_settings()
    old = s.his_webhook_key
    s.his_webhook_key = key
    try:
        return await fn(s)
    finally:
        s.his_webhook_key = old


async def test_webhook_stores_and_idempotent(session):
    async def run(_s):
        out = await his_webhook("finance", _body(event_type="billing.changed", event_id="E1",
                                                 payload={"amount": 100}),
                                background=BackgroundTasks(), request=_REQ, x_integration_key="hwk_test", session=session)
        assert out["received"] and not out["duplicate"]
        dup = await his_webhook("finance", _body(event_type="billing.changed", event_id="E1"),
                                background=BackgroundTasks(), request=_REQ, x_integration_key="hwk_test", session=session)
        assert dup["duplicate"] is True
        cnt = await session.scalar(select(func.count()).select_from(HisInboundEvent))
        assert cnt == 1
        ev = (await session.execute(select(HisInboundEvent))).scalar_one()
        assert ev.domain == "finance" and ev.status == "NEW" and ev.payload == {"amount": 100}
    await _with_key(session, "hwk_test", run)


async def test_webhook_bad_key_rejected(session):
    async def run(_s):
        with pytest.raises(ForbiddenError, match="인증"):
            await his_webhook("finance", _body(event_type="x", event_id="E2"),
                              background=BackgroundTasks(), request=_REQ, x_integration_key="wrong", session=session)
    await _with_key(session, "hwk_test", run)


async def test_webhook_unknown_domain_rejected(session):
    async def run(_s):
        with pytest.raises(ForbiddenError, match="도메인"):
            await his_webhook("nope", _body(event_type="x", event_id="E3"),
                              background=BackgroundTasks(), request=_REQ, x_integration_key="hwk_test", session=session)
    await _with_key(session, "hwk_test", run)


async def test_webhook_disabled_when_unset(session):
    async def run(_s):
        with pytest.raises(ForbiddenError, match="미설정"):
            await his_webhook("finance", _body(event_type="x"),
                              background=BackgroundTasks(), request=_REQ, x_integration_key="any", session=session)
    await _with_key(session, None, run)


async def test_billing_line_requeue_on_resend(session):
    """progressive billing — 동일 ref billing.line_recorded 재수신 시 payload 갱신·재처리."""
    async def run(_s):
        ref = "bline:enc1:rx:p1"
        # 1차: 금액 0(발생)
        a = await his_webhook("finance", _body(
            event_type="billing.line_recorded", source_ref=ref,
            payload={"lineId": "p1", "chartNo": "C1", "code": "AA", "unitPrice": 0}),
            background=BackgroundTasks(), request=_REQ, x_integration_key="hwk_test", session=session)
        assert a.get("duplicate") is not True
        eid = a["id"]
        # consume 가정 — 상태를 APPLIED로
        ev = await session.get(HisInboundEvent, eid)
        ev.status = "APPLIED"
        await session.flush()
        # 2차: 정산 재발행(금액 동봉) — 동일 ref
        b = await his_webhook("finance", _body(
            event_type="billing.line_recorded", source_ref=ref,
            payload={"lineId": "p1", "chartNo": "C1", "code": "AA", "unitPrice": 3045}),
            background=BackgroundTasks(), request=_REQ, x_integration_key="hwk_test", session=session)
        assert b.get("requeued") is True and b["id"] == eid    # 재처리(거부 아님)
        ev2 = await session.get(HisInboundEvent, eid)
        assert ev2.status == "NEW"                              # 재소비 대기
        assert ev2.payload["unitPrice"] == 3045                # 갱신됨
        # 갱신성 외 이벤트는 기존 멱등 유지
        await his_webhook("finance", _body(event_type="billing.changed",
                          event_id="X1", payload={"v": 1}),
                          background=BackgroundTasks(), request=_REQ, x_integration_key="hwk_test", session=session)
        c2 = await his_webhook("finance", _body(event_type="billing.changed",
                               event_id="X1", payload={"v": 2}),
                               background=BackgroundTasks(), request=_REQ, x_integration_key="hwk_test", session=session)
        assert c2["duplicate"] is True                         # 갱신 안 됨
    await _with_key(session, "hwk_test", run)


async def test_updatable_resend_resets_retry_count(session):
    """DEAD(소진) 갱신성 이벤트 재발행 시 retry_count·next_retry 리셋 — 1회 재DEAD 방지(전체검토 integ)."""
    async def run(_s):
        ref = "bline:dead:rx:p1"
        a = await his_webhook("finance", _body(
            event_type="billing.line_recorded", source_ref=ref, payload={"lineId": "p1"}),
            background=BackgroundTasks(), request=_REQ, x_integration_key="hwk_test", session=session)
        ev = await session.get(HisInboundEvent, a["id"])
        ev.status, ev.retry_count = "DEAD", 5      # 재시도 소진 가정
        await session.flush()
        await his_webhook("finance", _body(
            event_type="billing.line_recorded", source_ref=ref,
            payload={"lineId": "p1", "unitPrice": 100}),
            background=BackgroundTasks(), request=_REQ, x_integration_key="hwk_test", session=session)
        ev2 = await session.get(HisInboundEvent, a["id"])
        assert ev2.status == "NEW" and ev2.retry_count == 0 and ev2.next_retry_at is None
    await _with_key(session, "hwk_test", run)


async def test_vendor_synced_requeue_on_resync(session):
    """딜러 재동기화 — 고정 ref vendor.synced 재수신 시 마스터 변경분 갱신·재처리(유실 방지)."""
    async def run(_s):
        ref = "voucher:dealer:D-RS"
        a = await his_webhook("finance", _body(
            event_type="vendor.synced", source_ref=ref,
            payload={"dealerId": "D-RS", "name": "딜러", "commissionRate": 10}),
            background=BackgroundTasks(), request=_REQ, x_integration_key="hwk_test", session=session)
        assert a.get("duplicate") is not True
        eid = a["id"]
        ev = await session.get(HisInboundEvent, eid)
        ev.status = "APPLIED"
        await session.flush()
        # 수수료율 변경 후 동일 ref 재동기화 → 거부 아닌 갱신·재처리
        b = await his_webhook("finance", _body(
            event_type="vendor.synced", source_ref=ref,
            payload={"dealerId": "D-RS", "name": "딜러", "commissionRate": 15}),
            background=BackgroundTasks(), request=_REQ, x_integration_key="hwk_test", session=session)
        assert b.get("requeued") is True and b["id"] == eid
        ev2 = await session.get(HisInboundEvent, eid)
        assert ev2.status == "NEW" and ev2.payload["commissionRate"] == 15
    await _with_key(session, "hwk_test", run)


# ── 인박스 본문 HMAC + timestamp (S-1/S-2 동형) ──────────────
async def _with_inbound(session, fn, *, key="hwk_test", secret=None, enforce=False):
    s = get_settings()
    saved = (s.his_webhook_key, s.inbound_webhook_secret, s.inbound_webhook_hmac_enforce)
    s.his_webhook_key, s.inbound_webhook_secret, s.inbound_webhook_hmac_enforce = key, secret, enforce
    try:
        return await fn(s)
    finally:
        (s.his_webhook_key, s.inbound_webhook_secret, s.inbound_webhook_hmac_enforce) = saved


def test_signature_reason_unit():
    secret, raw = "ws_k", b'{"event":"x","payload":{}}'
    ts = str(int(time.time()))
    sig = _sign(secret, ts, raw)
    common = dict(now_epoch=time.time(), max_skew=300)
    assert inbound_signature_reason(raw, sig, ts, secret, **common) is None          # 정상
    assert inbound_signature_reason(raw, sig, ts, None, **common) == "시크릿 미설정"
    assert inbound_signature_reason(raw, None, ts, secret, **common)                  # 서명 누락
    assert inbound_signature_reason(raw, sig, None, secret, **common)                 # ts 누락
    assert "신선도" in inbound_signature_reason(
        raw, _sign(secret, "0", raw), "0", secret, now_epoch=time.time(), max_skew=300)  # 오래됨
    assert "불일치" in inbound_signature_reason(raw, _sign(secret, ts, b"tampered"), ts, secret, **common)
    # ISO8601 ts(S-2 송신 형식)도 수용
    from datetime import datetime, timezone
    iso = datetime.now(timezone.utc).isoformat()
    assert inbound_signature_reason(raw, _sign(secret, iso, raw), iso, secret, **common) is None


async def test_inbound_enforce_accepts_valid(session):
    raw, ts, secret = b'{"event":"billing.changed","ref":"H1"}', str(int(time.time())), "ws_k"

    async def run(_s):
        out = await his_webhook(
            "finance", _body(event_type="billing.changed", event_id="H1", payload={"a": 1}),
            background=BackgroundTasks(), request=_Req(raw), x_integration_key="hwk_test",
            x_signature=_sign(secret, ts, raw), x_timestamp=ts, session=session)
        assert out["received"] and not out["duplicate"]
    await _with_inbound(session, run, secret=secret, enforce=True)


async def test_inbound_enforce_rejects_missing_and_bad(session):
    raw, ts, secret = b'{"event":"x","ref":"H2"}', str(int(time.time())), "ws_k"

    async def run(_s):
        with pytest.raises(ForbiddenError, match="본문서명"):     # 서명 누락
            await his_webhook("finance", _body(event_type="x", event_id="H2"),
                              background=BackgroundTasks(), request=_Req(raw),
                              x_integration_key="hwk_test",
                              x_signature=None, x_timestamp=None, session=session)
        with pytest.raises(ForbiddenError, match="본문서명"):     # 서명 불일치(다른 ts로 서명)
            await his_webhook("finance", _body(event_type="x", event_id="H2b"),
                              background=BackgroundTasks(), request=_Req(raw),
                              x_integration_key="hwk_test",
                              x_signature=_sign(secret, "999", raw), x_timestamp=ts, session=session)
    await _with_inbound(session, run, secret=secret, enforce=True)


async def test_inbound_enforce_fail_closed_without_secret(session):
    async def run(_s):
        with pytest.raises(ForbiddenError, match="본문서명"):     # ON인데 시크릿 미설정 → 차단
            await his_webhook("finance", _body(event_type="x", event_id="H3"),
                              background=BackgroundTasks(), request=_Req(b"{}"),
                              x_integration_key="hwk_test", session=session)
    await _with_inbound(session, run, secret=None, enforce=True)


async def test_inbound_off_accepts_signed_and_unsigned(session):
    """OFF=병행수락 — 무서명(레거시)·서명불일치 모두 수락(관측만, 거부 안 함)."""
    raw, secret = b'{"event":"billing.changed","ref":"H4"}', "ws_k"

    async def run(_s):
        un = await his_webhook(                                   # 무서명 수락
            "finance", _body(event_type="billing.changed", event_id="H4"),
            background=BackgroundTasks(), request=_Req(raw), x_integration_key="hwk_test",
            x_signature=None, x_timestamp=None, session=session)
        assert un["received"]
        bad = await his_webhook(                                  # 서명 불일치도 수락(OFF)
            "finance", _body(event_type="billing.changed", event_id="H5"),
            background=BackgroundTasks(), request=_Req(raw), x_integration_key="hwk_test",
            x_signature="sha256=deadbeef", x_timestamp=str(int(time.time())), session=session)
        assert bad["received"]
    await _with_inbound(session, run, secret=secret, enforce=False)


class _FakeRedis:
    """SET NX 의미만 — 인박스 서명 nonce 재생 테스트용."""
    def __init__(self):
        self._keys = set()

    async def set(self, key, val, nx=False, ex=None):
        if nx and key in self._keys:
            return None
        self._keys.add(key)
        return True


async def test_inbound_enforce_rejects_signature_replay(session, monkeypatch):
    """신선도 창 내 동일 서명 재전송 차단(재생 방지 nonce·전체검토 2026-07-03)."""
    import erp.common.redis_client as rc
    fake = _FakeRedis()
    monkeypatch.setattr(rc, "get_redis", lambda: fake)
    raw, ts, secret = b'{"event":"billing.line_recorded","ref":"RP1"}', str(int(time.time())), "ws_k"
    sig = _sign(secret, ts, raw)

    async def run(_s):
        # 1회차 — 통과
        out = await his_webhook(
            "finance", _body(event_type="billing.line_recorded", event_id="RP1", payload={"a": 1}),
            background=BackgroundTasks(), request=_Req(raw), x_integration_key="hwk_test",
            x_signature=sig, x_timestamp=ts, session=session)
        assert out["received"]
        # 2회차 — 동일 서명 재전송 → 재생 거부(멱등 계층 이전에 서명 nonce가 차단)
        with pytest.raises(ForbiddenError, match="재생"):
            await his_webhook(
                "finance", _body(event_type="billing.line_recorded", event_id="RP1", payload={"a": 2}),
                background=BackgroundTasks(), request=_Req(raw), x_integration_key="hwk_test",
                x_signature=sig, x_timestamp=ts, session=session)
    await _with_inbound(session, run, secret=secret, enforce=True)


async def test_inbound_nonce_skipped_when_redis_down(session, monkeypatch):
    """redis 불가 시 nonce 검사 생략(가용성) — 서명 유효하면 수락(ts 신선도가 상한)."""
    import erp.common.redis_client as rc

    def _boom():
        raise RuntimeError("redis down")
    monkeypatch.setattr(rc, "get_redis", _boom)
    raw, ts, secret = b'{"event":"x","ref":"RD1"}', str(int(time.time())), "ws_k"

    async def run(_s):
        out = await his_webhook(
            "finance", _body(event_type="x", event_id="RD1", payload={}),
            background=BackgroundTasks(), request=_Req(raw), x_integration_key="hwk_test",
            x_signature=_sign(secret, ts, raw), x_timestamp=ts, session=session)
        assert out["received"]
    await _with_inbound(session, run, secret=secret, enforce=True)


def test_signature_reason_accepts_bare_hex_and_prefixed():
    """서명 포맷 호환 — HIS 발신은 접두 없는 hex, S-2는 sha256= 접두. 둘 다 수용(활성화 전 선결)."""
    import hashlib as _h
    import hmac as _hm
    secret = "ws_k"
    raw, ts = b'{"event":"x","ref":"H1"}', str(int(time.time()))
    hex_sig = _hm.new(secret.encode(), ts.encode() + b"." + raw, _h.sha256).hexdigest()
    common = dict(now_epoch=time.time(), max_skew=300)
    # bare hex(HIS 발신 형식) 통과
    assert inbound_signature_reason(raw, hex_sig, ts, secret, **common) is None
    # sha256= 접두(S-2 형식) 통과
    assert inbound_signature_reason(raw, "sha256=" + hex_sig, ts, secret, **common) is None
    # 잘못된 서명은 여전히 거부
    assert "불일치" in inbound_signature_reason(raw, "deadbeef", ts, secret, **common)


async def test_requeue_writes_status_even_if_session_value_is_stale(session):
    """검토 F — 세션이 읽은 status(NEW)와 대입값이 같으면 ORM이 UPDATE에서 빼 APPLIED가 남던 결함."""
    from sqlalchemy import select, update

    async def run(_s):
        ref = "bline:stale:1"
        a = await his_webhook("finance", _body(
            event_type="billing.line_recorded", source_ref=ref,
            payload={"lineId": "s1", "chartNo": "C1", "code": "AA", "unitPrice": 0}),
            background=BackgroundTasks(), request=_REQ, x_integration_key="hwk_test", session=session)
        eid = a["id"]
        await session.get(HisInboundEvent, eid)                       # 세션은 NEW로 기억
        await session.execute(update(HisInboundEvent).where(HisInboundEvent.id == eid)
                              .values(status="APPLIED"))              # 소비자가 커밋한 상태(세션 모름)
        await his_webhook("finance", _body(
            event_type="billing.line_recorded", source_ref=ref,
            payload={"lineId": "s1", "chartNo": "C1", "code": "AA", "unitPrice": 500}),
            background=BackgroundTasks(), request=_REQ, x_integration_key="hwk_test", session=session)
        st = await session.scalar(select(HisInboundEvent.status).where(HisInboundEvent.id == eid)
                                  .execution_options(populate_existing=True))
        assert st == "NEW"
    await _with_key(session, "hwk_test", run)
