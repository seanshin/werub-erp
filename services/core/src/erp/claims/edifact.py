"""EDIFACT 전문 토크나이저 — 심평원 전자문서 공통 (공고 제2021-55호 별표1 기반).

기본 구분자(UNA 미존재 시): 컴포넌트 `:`, 엘리먼트 `+`, 소수점 `.`, 릴리즈 `?`, 세그먼트 `'`
UNA 세그먼트가 있으면 해당 구분자를 사용. 릴리즈 문자는 다음 1글자를 리터럴 처리.
"""
from dataclasses import dataclass, field


@dataclass
class Segment:
    tag: str
    elements: list[list[str]] = field(default_factory=list)  # 엘리먼트 → 컴포넌트 목록

    def elem(self, i: int, c: int = 0, default: str = "") -> str:
        try:
            return self.elements[i][c]
        except IndexError:
            return default


@dataclass
class Delimiters:
    component: str = ":"
    element: str = "+"
    release: str = "?"
    segment: str = "'"


class EdifactParseError(Exception):
    pass


def parse(text: str) -> list[Segment]:
    """전문 → 세그먼트 목록. UNA 처리·릴리즈 문자 언이스케이프 포함."""
    text = text.strip().lstrip("﻿")
    if not text:
        raise EdifactParseError("빈 전문입니다")

    d = Delimiters()
    if text.startswith("UNA"):
        # UNA 직후 6글자: component, element, decimal, release, (reserved), segment
        if len(text) < 9:
            raise EdifactParseError("UNA 세그먼트가 불완전합니다")
        d = Delimiters(component=text[3], element=text[4], release=text[6], segment=text[8])
        text = text[9:]

    segments: list[Segment] = []
    elem_buf: list[str] = []
    comp_buf: list[str] = []
    cur = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == d.release:
            if i + 1 >= len(text):
                raise EdifactParseError("릴리즈 문자 뒤 입력 종료")
            cur += text[i + 1]
            i += 2
            continue
        if ch == d.component:
            comp_buf.append(cur)
            cur = ""
        elif ch == d.element:
            comp_buf.append(cur)
            cur = ""
            elem_buf.append(comp_buf)
            comp_buf = []
        elif ch == d.segment:
            comp_buf.append(cur)
            cur = ""
            elem_buf.append(comp_buf)
            comp_buf = []
            _flush_segment(segments, elem_buf)
            elem_buf = []
        elif ch in ("\r", "\n"):
            pass  # 세그먼트 사이 개행 허용 (가독 포맷)
        else:
            cur += ch
        i += 1

    if cur or comp_buf or elem_buf:  # 종결자 없는 잔여
        comp_buf.append(cur)
        elem_buf.append(comp_buf)
        _flush_segment(segments, elem_buf)
    if not segments:
        raise EdifactParseError("세그먼트가 없습니다")
    return segments


def _flush_segment(segments: list[Segment], elem_buf: list[list[str]]) -> None:
    tag_comp = elem_buf[0]
    tag = (tag_comp[0] if tag_comp else "").strip()
    if not tag:
        return
    segments.append(Segment(tag=tag, elements=elem_buf[1:]))
