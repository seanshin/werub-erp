"""건강보험요양급여비용 책자(pdftotext -layout) → 공시 CSV 분할.

사용: python scripts/split_suga_book.py <book.txt> <out.csv>
- 본문 표제(목차 점선 '·' 제외)의 '제N부/제N장' 경계로 분할
- 120K자 초과 장은 '제N절' 보조 분할 (weve 청크 부담 완화)
- import_notices.py CSV 형식(notice_type,title,body_text,effective_from) — 1회 실행용
"""
import csv
import re
import sys
from pathlib import Path

MAX_CHARS = 120_000
HEAD = re.compile(r"^\s{0,30}(제\s*\d+\s*[부장편])\s+(\S.*)$")
SUB = re.compile(r"^\s{0,30}(제\s*\d+\s*절)\s+(\S.*)$")


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()[:120]


def split(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    raw_marks: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        if "·" in ln:                       # 목차/페이지 점선 제외
            continue
        m = HEAD.match(ln)
        if m:
            raw_marks.append((i, f"{_clean(m.group(1))} {_clean(m.group(2))}"))
    # 페이지 러닝헤더(같은 표제가 페이지마다 반복) 제거 — 최초 1회만 경계로 인정
    freq: dict[str, int] = {}
    for _, t in raw_marks:
        freq[t] = freq.get(t, 0) + 1
    seen: set[str] = set()
    marks: list[tuple[int, str]] = []
    for i, t in raw_marks:
        if freq[t] > 3 and t in seen:       # 반복 표제는 첫 등장만
            continue
        seen.add(t)
        marks.append((i, t))
    out: list[tuple[str, str]] = []
    for idx, (start, title) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        body = "\n".join(lines[start:end]).strip()
        if len(body) < 500:                 # 표제 반복·빈 구획 스킵
            continue
        if len(body) <= MAX_CHARS:
            out.append((title, body))
            continue
        # 절 단위 보조 분할
        seg = lines[start:end]
        subs = [(j, f"{title} / {_clean(s.group(1))} {_clean(s.group(2))}")
                for j, ln2 in enumerate(seg) if "·" not in ln2
                for s in [SUB.match(ln2)] if s]
        if not subs:
            for k in range(0, len(body), MAX_CHARS):     # 최후: 길이 분할
                out.append((f"{title} ({k // MAX_CHARS + 1})", body[k:k + MAX_CHARS]))
            continue
        bounds = [0] + [j for j, _ in subs] + [len(seg)]
        titles = [title + " (서두)"] + [t for _, t in subs]
        for b in range(len(bounds) - 1):
            chunk = "\n".join(seg[bounds[b]:bounds[b + 1]]).strip()
            if len(chunk) >= 500:
                out.append((titles[b], chunk))
    # 최종 캡 — CSV 필드 한도(131,072자) 및 weve 청크 부담: 초과분 길이 분할
    capped: list[tuple[str, str]] = []
    for title, body in out:
        if len(body) <= MAX_CHARS:
            capped.append((title, body))
        else:
            for k in range(0, len(body), MAX_CHARS):
                capped.append((f"{title} ({k // MAX_CHARS + 1})", body[k:k + MAX_CHARS]))
    return capped


if __name__ == "__main__":
    text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
    parts = split(text)
    with open(sys.argv[2], "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["notice_type", "title", "body_text", "effective_from"])
        for title, body in parts:
            w.writerow(["TARIFF", f"건강보험요양급여비용(2026년판) — {title}",
                        body, "2026-01-01"])
    sizes = sorted(len(b) for _, b in parts)
    print(f"분할 {len(parts)}건 · 최소 {sizes[0]:,}자 · 중간 {sizes[len(sizes)//2]:,}자 "
          f"· 최대 {sizes[-1]:,}자")
