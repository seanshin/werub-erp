"""MEDRLT 파서 — EDIFACT 토크나이저·구조 매핑·스테이징 파이프라인 (FR-38/39, G1).

⚠️ 픽스처는 공고 별표1 구조 + 잠정 한정자(medrlt_qualifiers) 기반 **합성** 전문 —
   실물 검증(운영 게이트)과 별개로 파서 코어·파이프라인의 정확성을 고정한다.
"""
from datetime import date

import pytest
from sqlalchemy import select

from erp.claims.edifact import EdifactParseError, parse
from erp.claims.ingest_service import NoticeIngestService
from erp.claims.medrlt_parser import MedrltParseError, parse_medrlt
from erp.claims.models import ClaimAdjustment, ClaimBatch, ClaimStatement, NoticeFileStaging


# ── EDIFACT 토크나이저 ──────────────────────────────────────
def test_tokenizer_segments_elements_components():
    segs = parse("UNH+REF001+MEDRLT:D:96A'BGM+390'")
    assert [s.tag for s in segs] == ["UNH", "BGM"]
    assert segs[0].elem(0, 0) == "REF001"
    assert segs[0].elem(1, 0) == "MEDRLT" and segs[0].elem(1, 1) == "D"


def test_tokenizer_release_char():
    """릴리즈 문자 `?` — 구분자 리터럴 처리."""
    segs = parse("FTX+ACB+++사유?'포함?+텍스트'")
    assert segs[0].elem(3, 0) == "사유'포함+텍스트"


def test_tokenizer_una_custom_delimiters():
    segs = parse("UNA;*,! |UNH*R1*MEDRLT|")
    assert segs[0].tag == "UNH" and segs[0].elem(1, 0) == "MEDRLT"


def test_tokenizer_empty_rejected():
    with pytest.raises(EdifactParseError):
        parse("   ")


# ── MEDRLT 구조 매핑 (합성 전문) ────────────────────────────
SYNTHETIC = (
    "UNH+MSG001+MEDRLT:D:96A'"
    "BGM+390+DOC001'"
    "DTM+137:20260601'"                 # 통보일자
    "NAD+HP+11234567'"                  # HEADER 요양기관
    "RFF+ACE:2026-001234'"              # SG1 접수번호 → body 진입
    "MOA+79:125430000'"                 # 접수 청구액
    "MOA+52:124896460'"                 # 접수 결정액
    "NAD+PAT'"                          # SG3 — 명세서 1
    "RFF+AFA:00027'"                    # 명일련
    "MOA+79:533540'"
    "MOA+52:0'"
    "MOA+129:533540'"                   # 명세서 조정 합계
    "ALC+A100'"                         # 조정코드
    "FTX+ACB+++요양급여 인정 기준 초과'"
    "NAD+PAT'"                          # SG3 — 명세서 2
    "RFF+AFA:00103'"
    "MOA+79:1204300'"
    "MOA+52:1094874'"
    "LIN+3'"                            # SG4 줄 3
    "ALC+B200'"
    "MOA+129:109426'"
    "FTX+ACB+++처치료 산정 착오'"
    "UNS+S'"
    "UNT+24+MSG001'"
)


def test_parse_medrlt_structure():
    n = parse_medrlt(SYNTHETIC)
    assert n.receipt_no == "2026-001234"
    assert n.notified_on == "20260601"
    assert n.claimed_amt == 125430000 and n.decided_amt == 124896460
    assert len(n.statements) == 2

    s1, s2 = n.statements
    assert s1.stmt_serial == "00027" and s1.claimed_amt == 533540
    assert s1.adjustments[0].adj_code == "A100"
    assert s1.adjustments[0].adj_reason == "요양급여 인정 기준 초과"

    assert s2.stmt_serial == "00103"
    adj = s2.adjustments[0]
    assert adj.line_no == "3" and adj.adj_code == "B200" and adj.adj_amt == 109426
    assert adj.adj_reason == "처치료 산정 착오"


def test_parse_unknown_qualifier_preserved_not_dropped():
    """미인식 한정자 — 버리지 않고 raw 보존 (실물 대조용)."""
    n = parse_medrlt(SYNTHETIC.replace("RFF+AFA:00027", "RFF+ZZZ:00027"))
    assert n.statements[0].stmt_serial is None
    assert n.statements[0].unknown_rff == {"ZZZ": "00027"}


def test_parse_non_medrlt_rejected():
    with pytest.raises(MedrltParseError):
        parse_medrlt("UNH+M1+MEDREQ:D'UNT+2+M1'")


def test_parse_missing_receipt_no_rejected():
    with pytest.raises(MedrltParseError):
        parse_medrlt("UNH+M1+MEDRLT'BGM+390'UNT+2+M1'")


def test_parse_empty_receipt_no_rejected():
    """심층 재검토 HIGH — 빈 접수번호(RFF+ACE:)는 거부(서로 다른 통보서를 ''로 병합하는 멱등키 붕괴 방지)."""
    empty_ace = SYNTHETIC.replace("RFF+ACE:2026-001234", "RFF+ACE:")
    with pytest.raises(MedrltParseError, match="접수번호"):
        parse_medrlt(empty_ace)


def test_parse_unparseable_amount_isolated_not_silent_zero():
    """심층 재검토 HIGH — 파싱 불가 금액은 침묵 0이 아니라 격리(재무 금액 소실 방지)."""
    bad_amount = SYNTHETIC.replace("MOA+79:533540", "MOA+79:533.540,00")   # 소수 콤마 등 표기 차이
    with pytest.raises(MedrltParseError, match="금액 파싱 실패"):
        parse_medrlt(bad_amount)
    # 빈 금액은 정상적으로 0 (격리 아님)
    empty_amount = SYNTHETIC.replace("MOA+52:0'", "MOA+52:'")
    n = parse_medrlt(empty_amount)
    assert n.statements[0].decided_amt == 0.0


# ── 스테이징 파이프라인 (멱등·격리) ─────────────────────────
async def _stage(session, tmp_path, content: bytes, name="n1"):
    svc = NoticeIngestService(storage_dir=str(tmp_path))
    (tmp_path / name).write_bytes(content)
    staging = NoticeFileStaging(stored_name=name, original_name=f"{name}.txt",
                                size_bytes=len(content), uploaded_by=1)
    session.add(staging)
    await session.flush()
    return svc, staging


async def test_ingest_creates_batch_statements_adjustments(session, tmp_path):
    svc, staging = await _stage(session, tmp_path, SYNTHETIC.encode("utf-8"))
    assert await svc.process_one(session, staging) == "OK"
    assert staging.status == "OK"

    batch = (await session.execute(select(ClaimBatch))).scalar_one()
    assert batch.receipt_no == "2026-001234"
    assert batch.notice_arrived_on == staging.received_at.date()  # 기산=실수신일(FR-40·#607)
    stmts = (await session.execute(select(ClaimStatement))).scalars().all()
    assert {s.stmt_serial for s in stmts} == {"00027", "00103"}
    adjs = (await session.execute(select(ClaimAdjustment))).scalars().all()
    assert all(a.map_status == "AUTO" for a in adjs)  # 자동화율 분자 (Plan §4.3)


async def test_ingest_arrived_on_uses_receipt_day_not_processing_day(session, tmp_path):
    """기한 기산일 = 실제 수신일(received_at) — G1으로 수일 뒤 파싱해도 처리일로 밀리지 않음(FR-40)."""
    from datetime import datetime, timedelta, timezone
    svc, staging = await _stage(session, tmp_path, SYNTHETIC.encode("utf-8"))
    backdated = datetime.now(timezone.utc) - timedelta(days=12)
    staging.received_at = backdated              # 12일 전 실제 수신, 오늘 파싱
    await session.flush()
    assert await svc.process_one(session, staging) == "OK"
    batch = (await session.execute(select(ClaimBatch))).scalar_one()
    assert batch.notice_arrived_on == backdated.date()    # 처리일(today)이 아닌 수신일
    assert batch.notice_arrived_on != date.today()


async def test_ingest_idempotent_same_receipt(session, tmp_path):
    svc, s1 = await _stage(session, tmp_path, SYNTHETIC.encode("utf-8"), "a")
    await svc.process_one(session, s1)
    _, s2 = await _stage(session, tmp_path, SYNTHETIC.encode("utf-8"), "b")
    assert await svc.process_one(session, s2) == "SKIPPED"  # 중복 반영 불가
    assert len((await session.execute(select(ClaimBatch))).scalars().all()) == 1


async def test_ingest_malformed_isolated_as_failed(session, tmp_path):
    """실패 행 격리 (E2E ⑥의 파싱 단계) — reprocess로 재시도 가능 상태."""
    svc, staging = await _stage(session, tmp_path, b"NOT-AN-EDIFACT-FILE")
    assert await svc.process_one(session, staging) == "FAILED"
    assert staging.status == "FAILED" and "error" in staging.error_detail
    assert (await session.execute(select(ClaimBatch))).scalar_one_or_none() is None


async def test_ingest_missing_serials_no_uq_violation(session, tmp_path):
    """심층 재검토 HIGH — 일련번호 없는 명세가 2건이어도 uq_stmt 충돌 없이 적재(배치잡 오염 방지).

    과거 stmt_serial='?' 폴백이면 무번호 다건이 (batch_id,'?') 중복 → IntegrityError가
    process_one의 MedrltParseError 격리를 뚫고 배치잡 전체를 오염시켰다."""
    no_serials = (SYNTHETIC.replace("RFF+AFA:00027'", "").replace("RFF+AFA:00103'", ""))
    svc, staging = await _stage(session, tmp_path, no_serials.encode("utf-8"))
    assert await svc.process_one(session, staging) == "OK"   # IntegrityError 없이 성공
    stmts = (await session.execute(select(ClaimStatement))).scalars().all()
    assert len(stmts) == 2
    assert len({s.stmt_serial for s in stmts}) == 2          # 폴백 일련번호 유일(#1·#2)


async def test_ingest_cp949_decoded(session, tmp_path):
    svc, staging = await _stage(session, tmp_path, SYNTHETIC.encode("cp949"))
    assert await svc.process_one(session, staging) == "OK"


# ── G1 운영 게이트 (FR-38 medrlt_live_enabled) ──────────────
async def test_process_pending_gated_by_default(session, tmp_path):
    """G1 게이트 — 기본(medrlt_live_enabled=False)이면 통보서 반영 차단(잠정 한정자 무게이트 유입 방지).

    staging은 NEW로 보존 — 실물 2건 검증 후 플래그를 켜면 다음 회차에 반영."""
    svc, staging = await _stage(session, tmp_path, SYNTHETIC.encode("utf-8"))
    out = await svc.process_pending(session)
    assert out.get("gated") is True and out["ok"] == 0
    assert staging.status == "NEW"          # 소비 안 됨 — 큐 보존
    assert (await session.execute(select(ClaimBatch))).scalar_one_or_none() is None


async def test_process_pending_runs_when_enabled(session, tmp_path, monkeypatch):
    """게이트 ON(실물 검증 통과)이면 정상 반영 — 권위 테이블 생성."""
    from erp.config import get_settings
    monkeypatch.setattr(get_settings(), "medrlt_live_enabled", True, raising=False)
    svc, staging = await _stage(session, tmp_path, SYNTHETIC.encode("utf-8"))
    out = await svc.process_pending(session)
    assert out["ok"] == 1 and not out.get("gated")
    assert staging.status == "OK"
    assert (await session.execute(select(ClaimBatch))).scalar_one() is not None


# ── 조정사유 분류 (FR-39 — adj_codes) ──────────────────────
def test_adj_categorize():
    from erp.claims.adj_codes import RECOVERABLE, categorize
    assert categorize("A100") == ("A", "금액 산정착오")
    assert categorize("F12") == ("F", "구입 증빙자료 미제출")
    assert categorize(None) == ("?", "미분류")
    assert categorize("Z99") == ("?", "미분류")
    assert "F" in RECOVERABLE and "B" not in RECOVERABLE


async def test_stats_by_reason(session, tmp_path):
    from erp.claims.service import ClaimStatsService
    svc, staging = await _stage(session, tmp_path, SYNTHETIC.encode("utf-8"), "r1")
    await svc.process_one(session, staging)
    rows = await ClaimStatsService().by_dimension(session, by="reason")
    dims = {r["dim"] for r in rows}
    assert any(d.startswith("A — ") for d in dims)  # A100
    assert any(d.startswith("B — ") for d in dims)  # B200


async def test_ingest_multi_message_file(session, tmp_path):
    """복수 전문 누적 파일 (청구 SW 실무 사양) — UNB 봉투 + 2건 → 배치 2개."""
    second = SYNTHETIC.replace("2026-001234", "2026-005678").replace("MSG001", "MSG002")
    multi = "UNB+UNOB:1+SENDER+RECEIVER+260601:1200+IC001'" + SYNTHETIC + second + "UNZ+2+IC001'"
    svc, staging = await _stage(session, tmp_path, multi.encode("utf-8"), "multi")
    assert await svc.process_one(session, staging) == "OK"
    batches = (await session.execute(select(ClaimBatch))).scalars().all()
    assert {b.receipt_no for b in batches} == {"2026-001234", "2026-005678"}
    assert staging.error_detail["messages"] == 2 and staging.error_detail["created"] == 2
