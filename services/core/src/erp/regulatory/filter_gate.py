"""FR-53 AI 발신 민감정보 필터 게이트 — design §5.3.

전처리: NFC + 공백/구분자/zero-width 제거 + 동형문자(전각 등) 정규화 후 매칭.
탐지: (a) 패턴 (b) 주민번호 체크섬 검증 (c) 실데이터 사전 대조(차트번호·사번 — 호출측 주입).
탐지 시 BLOCKED — 승인은 content_hash에 바인딩 (발신 직전 hash 불일치 시 재필터).
"""
import re
import unicodedata
from dataclasses import dataclass, field

# zero-width·BOM·결합 공백류
_INVISIBLE = re.compile(r"[​‌‍⁠﻿­]")
# 구분자류 (하이픈 변형·가운뎃점 포함 — 동형 대시/구분점 우회 무력화)
_SEPARATORS = re.compile(r"[\s\-‐‑‒–—―−.,/\\|_·‧・･]+")

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def _canonicalize(text: str) -> str:
    """매칭 전 정규화 — 우회 벡터(공백 삽입·전각·zero-width·특수 대시) 무력화."""
    text = unicodedata.normalize("NFKC", text)   # 전각→반각 포함
    text = text.translate(_FULLWIDTH_DIGITS)
    text = _INVISIBLE.sub("", text)
    return text


def _digit_stream(text: str) -> str:
    """구분자 제거 후 숫자 스트림 (주민번호류 탐지용)."""
    return _SEPARATORS.sub("", _canonicalize(text))


def rrn_checksum_valid(rrn13: str) -> bool:
    """주민등록번호 체크섬 (mod 11)."""
    if len(rrn13) != 13 or not rrn13.isdigit():
        return False
    weights = [2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5]
    total = sum(int(d) * w for d, w in zip(rrn13[:12], weights))
    return (11 - total % 11) % 10 == int(rrn13[12])


# 숫자 스트림에서 주민번호 후보: 생년월일(6) + 성별(1-4,5-8 외국인) + 6자리
_RRN_CANDIDATE = re.compile(r"\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[1-8]\d{6}")
# 휴대전화 (01x + 7~8자리)
_PHONE = re.compile(r"01[016789]\d{7,8}")
# 차트번호 패턴 (병원 표준 형식 — 운영 시 실제 형식으로 보정). \b — 단어 내부 오탐 방지
_CHART_NO = re.compile(r"(?i)\b(?:C|CHT|CHART)[-_]?\d{4,8}\b")


@dataclass
class FilterResult:
    blocked: bool
    findings: list[dict] = field(default_factory=list)


def scan(text: str, *, known_chart_nos: set[str] | None = None,
         known_employee_nos: set[str] | None = None) -> FilterResult:
    """본문 스캔 — 탐지 시 blocked=True + 탐지 상세 (저장은 호출측이 filter_detail에)."""
    findings: list[dict] = []
    canon = _canonicalize(text)
    stream = _digit_stream(text)

    # (a)+(b) 주민번호: 패턴 후보 → 체크섬 검증 통과분만 확정 (오탐 억제)
    for m in _RRN_CANDIDATE.finditer(stream):
        if rrn_checksum_valid(m.group()):
            findings.append({"type": "RRN", "masked": m.group()[:6] + "-*******"})

    for m in _PHONE.finditer(stream):
        findings.append({"type": "PHONE", "masked": m.group()[:3] + "****" + m.group()[-2:]})

    for m in _CHART_NO.finditer(canon):
        findings.append({"type": "CHART_NO", "masked": m.group()[:2] + "***"})

    # (c) 실데이터 사전 대조 — canon(정규화)과 stream(구분자 제거) 모두 대조.
    # canon만 보면 "1234-5678"처럼 구분자를 낀 차트번호가 우회(docstring이 약속한 구분자 제거 미적용).
    for chart in (known_chart_nos or set()):
        cc, cs = _canonicalize(chart), _digit_stream(chart)
        if cc and (cc in canon or (cs and cs in stream)):
            findings.append({"type": "CHART_NO_EXACT", "masked": chart[:2] + "***"})
    for emp in (known_employee_nos or set()):
        ec, es = _canonicalize(emp), _digit_stream(emp)
        if ec and (ec in canon or (es and es in stream)):
            findings.append({"type": "EMPLOYEE_NO", "masked": emp[:2] + "***"})

    return FilterResult(blocked=bool(findings), findings=findings)


async def pii_reference_sets(session) -> tuple[set[str], set[str]]:
    """비식별 대조용 — 활성 차트번호·사번 집합(scan known_ 인자). 발신 전 실데이터 누출 차단.

    공시 본문에 실 차트번호·사번이 섞여도(표준 합성패턴 밖) 대조 적발. 외부 발신 경로
    전용(저빈도)이라 전수 로드 허용.
    """
    from sqlalchemy import select

    from erp.hr.models import Employee
    from erp.patient.models import Patient
    # NULL은 SQL에서 배제 — 전송량 축소·인덱스 활용(전수 스캔 부담 완화)
    charts = set((await session.scalars(
        select(Patient.chart_no).where(Patient.chart_no.isnot(None)))).all())
    emps = set((await session.scalars(
        select(Employee.employee_no).where(Employee.employee_no.isnot(None)))).all())
    return charts, emps
