"""MEDRLT(요양급여비용 심사결과통보서) 파서 — FR-38/39.

근거: 공고 제2021-55호 별표1 (docs/01-plan/references/hira-edi-spec/)
구조: HEADER → SG1(접수, ×150) → SG3(명세서/명일련, ×99999, NAD로 시작)
      → SG4(LIN 줄)/SG5(MOA 조정금액·FTX 사유) → SUMMARY(UNS·UNT)

⚠️ 한정자 코드는 medrlt_qualifiers.py 잠정값 — 실물 검증 게이트(G1) 유지.
   미인식 한정자는 버리지 않고 raw로 보존 (unknown_*) — 실물 대조 시 활용.
"""
import logging
from dataclasses import dataclass, field

from erp.claims import medrlt_qualifiers as Q
from erp.claims.edifact import EdifactParseError, Segment, parse

logger = logging.getLogger("erp.claims.medrlt")


@dataclass
class ParsedAdjustment:
    line_no: str | None = None
    adj_code: str | None = None
    adj_amt: float = 0.0
    adj_reason: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class ParsedStatement:
    stmt_serial: str | None = None
    chart_ref: str | None = None      # 수진자 식별 (실물 확정 대상)
    dept_code: str | None = None
    claimed_amt: float | None = None
    adjusted_amt: float | None = None
    decided_amt: float | None = None
    adjustments: list[ParsedAdjustment] = field(default_factory=list)
    unknown_rff: dict = field(default_factory=dict)
    unknown_moa: dict = field(default_factory=dict)


@dataclass
class ParsedNotice:
    message_ref: str | None = None    # UNH 참조 (심사차수)
    receipt_no: str | None = None     # 접수번호
    notified_on: str | None = None    # 통보일자 (YYYYMMDD)
    claimed_amt: float | None = None
    decided_amt: float | None = None
    statements: list[ParsedStatement] = field(default_factory=list)
    segment_count: int = 0
    unknown_segments: list[str] = field(default_factory=list)


class MedrltParseError(Exception):
    pass


def _amount(seg: Segment) -> tuple[str, float]:
    """MOA+qualifier:amount — 컴포넌트 (한정자, 금액).

    빈 금액은 0(정상). 값이 있으나 파싱 불가(소수 콤마 등 표기 차이)면 **침묵 0 대신 격리** —
    재무 금액을 0으로 조용히 적재하는 것보다 통보서를 FAILED로 격리해 사람이 검증하게 한다(G1).
    (UNA 소수 구분자는 edifact 토크나이저가 아직 미반영 — 실물 검증 시 확정 대상.)
    """
    qualifier = seg.elem(0, 0)
    raw = (seg.elem(0, 1) or seg.elem(1, 0) or "").strip()
    if not raw:
        return qualifier, 0.0
    try:
        return qualifier, float(raw)
    except ValueError as exc:
        raise MedrltParseError(
            f"금액 파싱 실패: {raw!r}(한정자 {qualifier}) — 소수/구분자 표기 확인 필요") from exc


def parse_medrlt_file(text: str) -> list[ParsedNotice]:
    """수신 파일 단위 파싱 — 복수 전문(UNH…UNT 반복) 지원.

    실무 사양(청구 SW 매뉴얼): 수신 파일은 청구 월이 달라도 한 파일에 누적됨.
    UNB/UNZ 인터체인지 봉투는 무시, UNH마다 독립 통보서로 분리.
    """
    try:
        segments = parse(text)
    except EdifactParseError as exc:
        raise MedrltParseError(f"EDIFACT 파싱 실패: {exc}") from exc

    groups: list[list[Segment]] = []
    current: list[Segment] | None = None
    for seg in segments:
        if seg.tag == "UNH":
            current = [seg]
            groups.append(current)
        elif current is not None:
            current.append(seg)
        # UNH 이전(UNB 등 봉투)은 무시
    if not groups:
        raise MedrltParseError("UNH 헤더가 없습니다 — MEDRLT 전문이 아님")
    return [_parse_message(g) for g in groups]


def parse_medrlt(text: str) -> ParsedNotice:
    """단일 전문 파싱 — 복수 전문 파일이면 첫 건만 (파일 단위는 parse_medrlt_file)."""
    return parse_medrlt_file(text)[0]


def _parse_message(segments: list[Segment]) -> ParsedNotice:
    notice = ParsedNotice(segment_count=len(segments))

    current_stmt: ParsedStatement | None = None
    current_adj: ParsedAdjustment | None = None
    in_body = False

    for seg in segments:
        tag = seg.tag
        if tag == "UNH":
            notice.message_ref = seg.elem(0, 0)
            msg_type = seg.elem(1, 0)
            if msg_type and msg_type != "MEDRLT":
                raise MedrltParseError(f"지원하지 않는 전문 유형: {msg_type}")
        elif tag == "BGM":
            in_body = False
        elif tag == "DTM":
            qualifier, value = seg.elem(0, 0), seg.elem(0, 1)
            if Q.DTM.get(qualifier) == "notified_on" and not notice.notified_on:
                notice.notified_on = value
        elif tag == "NAD" and in_body:
            # SG3 시작 — 새 명세서
            current_stmt = ParsedStatement()
            current_adj = None
            notice.statements.append(current_stmt)
        elif tag == "RFF":
            qualifier, value = seg.elem(0, 0), seg.elem(0, 1)
            meaning = Q.RFF.get(qualifier)
            if meaning == "receipt_no" and not notice.receipt_no and (value or "").strip():
                notice.receipt_no = value.strip()
                in_body = True  # SG1 진입 후 본문 (빈 접수번호는 무시 — 뒤의 유효값이 채우도록)
            elif current_stmt is not None:
                if meaning == "stmt_serial":
                    current_stmt.stmt_serial = value
                elif meaning == "line_no":
                    current_adj = ParsedAdjustment(line_no=value)
                    current_stmt.adjustments.append(current_adj)
                elif meaning == "adj_basis" and current_adj is not None:
                    current_adj.raw["basis"] = value
                else:
                    current_stmt.unknown_rff.setdefault(qualifier, value)
        elif tag == "LIN":
            if current_stmt is not None:
                current_adj = ParsedAdjustment(line_no=seg.elem(0, 0) or None)
                current_stmt.adjustments.append(current_adj)
        elif tag == "MOA":
            qualifier, amount = _amount(seg)
            meaning = Q.MOA.get(qualifier)
            if current_adj is not None and meaning == "adjusted_amt":
                current_adj.adj_amt = amount
            elif current_stmt is not None:
                if meaning == "claimed_amt":
                    current_stmt.claimed_amt = amount
                elif meaning == "decided_amt":
                    current_stmt.decided_amt = amount
                elif meaning == "adjusted_amt":
                    current_stmt.adjusted_amt = amount
                else:
                    current_stmt.unknown_moa.setdefault(qualifier, amount)
            else:
                if meaning == "claimed_amt":
                    notice.claimed_amt = amount
                elif meaning == "decided_amt":
                    notice.decided_amt = amount
        elif tag == "ALC":
            # 조정 구분 — 조정코드 (SG3/SG6)
            code = seg.elem(0, 0) or seg.elem(1, 0)
            if current_adj is not None and current_adj.adj_code is None:
                current_adj.adj_code = code
            elif current_stmt is not None:
                current_adj = ParsedAdjustment(adj_code=code)
                current_stmt.adjustments.append(current_adj)
        elif tag == "FTX":
            qualifier = seg.elem(0, 0)
            text_val = seg.elem(3, 0) or seg.elem(1, 0)
            if Q.FTX.get(qualifier) == "adj_reason" and current_adj is not None:
                current_adj.adj_reason = text_val
        elif tag in ("UNS", "UNT", "AUT", "AGR", "CCI", "CTA", "COM",
                     "QTY", "PRI", "IND", "IMD", "UNB", "UNZ"):
            pass  # 구조 인지하되 현 단계 미사용 — 실물 검증 시 확장
        else:
            notice.unknown_segments.append(tag)

    if not (notice.receipt_no or "").strip():   # None·빈문자열 모두 거부 — 빈 접수번호가 서로 다른 통보서를 병합(멱등키 붕괴)하는 것 방지
        raise MedrltParseError("접수번호(RFF)가 없습니다 — 한정자 테이블 보정 필요 가능")
    return notice
