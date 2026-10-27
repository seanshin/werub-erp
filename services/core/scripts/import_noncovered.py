"""고시 전문 → 행위 비급여 목록(제1편 제3부) 추출·적재.

사용: python scripts/import_noncovered.py <pdftotext -layout 결과.txt>
- 섹션: "제3부 행위 비급여 목록" ~ "제2편" 사이
- 코드: EDI 형식 [A-Z]{2}\\d{3}[\\dA-Z]? (예: AZ001·BZ012·UZ035), '*' 접미 허용
- 명칭: 같은 줄 코드 뒤 텍스트(표 행) 또는 "명칭(코드)" 패턴
- fee_master upsert: code_type=NONCOVERED, unit_price=0(기관 자율가 — HIS 가격표 별도),
  valid_from=2026-01-01, (code, code_type, valid_from) 멱등
"""
import asyncio
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

CODE_RE = re.compile(r"\b([A-Z]{2}\d{3}[\dA-Z]?)\*?\b")
SECTION_START = "제3부 행위 비급여 목록"
SECTION_END = re.compile(r"제\s*2\s*편")
# 명칭 정제: 표 머리 분류기호·항목기호 제거
LEAD_STRIP = re.compile(r"^[\s*：:.\-]+|^[가-힣]\.\s*|^\d+\.\s*")


def parse(text: str) -> dict[str, str]:
    lines = text.splitlines()
    # 목차(점선 포함) 제외 — 본문 표제만
    start = next(i for i, ln in enumerate(lines)
                 if SECTION_START in ln and "·" not in ln)
    out: dict[str, str] = {}
    for ln in lines[start:]:
        if SECTION_END.search(ln) and "질병군" in ln:
            break
        for m in CODE_RE.finditer(ln):
            code = m.group(1)
            raw = ln[m.end():].lstrip("* ")
            if raw.startswith(")"):
                raw = raw[1:]
            before = ln[:m.start()].rstrip()
            if raw.lstrip().startswith(("：", ":")):
                name = ""                                   # 코드 뒤가 바로 설명 — 이름은 앞쪽
            else:
                name = raw.split("：")[0].split(":")[0].strip()
                name = re.sub(r"^[가-힣]\.\s*", "", name)
            if not name and before.endswith("("):           # "명칭(코드)：설명" 패턴
                base = re.sub(r"^.*?[：:]", "", before[:-1]).strip()
                name = re.sub(r"^[가-힣]\.\s*", "", base).strip()
            name = re.sub(r"\s{2,}", " ", name)[:200]
            if code not in out or (name and len(name) > len(out[code])):
                out[code] = name or out.get(code, "")
    return out


async def load(codes: dict[str, str]) -> None:
    from sqlalchemy import select

    from erp.db import session_factory
    from erp.regulatory.models import FeeMaster

    valid_from = date(2026, 1, 1)
    created = updated = 0
    async with session_factory()() as s:
        for code, name in sorted(codes.items()):
            row = (await s.execute(select(FeeMaster).where(
                FeeMaster.code == code, FeeMaster.code_type == "NONCOVERED",
                FeeMaster.valid_from == valid_from))).scalar_one_or_none()
            if row is None:
                s.add(FeeMaster(code=code, code_type="NONCOVERED",
                                name=name or code, unit_price=0,
                                valid_from=valid_from))
                created += 1
            elif name and row.name != name and len(name) > len(row.name or ""):
                row.name = name
                updated += 1
        await s.commit()
    print(f"NONCOVERED 적재: created={created} updated={updated} total={len(codes)}")


if __name__ == "__main__":
    text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
    codes = parse(text)
    print(f"추출 코드 {len(codes)}건 (샘플: {list(codes.items())[:3]})")
    asyncio.run(load(codes))
