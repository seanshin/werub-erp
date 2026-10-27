"""content_hash 상호 검증 벡터 생성 (AI 연동 스펙 v1.0 — C그룹 합의 ③).

AI팀 회신이 명시한 3케이스: ① 일반 본문 ② 표 서식 포함 ③ CRLF+BOM 포함.
산출 JSON을 양측(ERP·LLMManager)이 각자 구현으로 검증 — 해시 일치 확인.

실행: .venv/bin/python scripts/gen_hash_vectors.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from erp.regulatory.content_hash import content_hash  # noqa: E402

VECTORS = [
    {
        "name": "v1-plain",
        "description": "일반 본문 — 한글·개행·문서끝 개행 정규화",
        "body_text": "보건복지부 고시 제2026-001호\n\n요양급여비용 수가 개정 안내.\n시행일: 2026년 7월 1일\n",
    },
    {
        "name": "v2-table",
        "description": "표 서식(파이프·탭) + 행끝 공백 제거 검증 (행끝 공백 2칸 포함)",
        "body_text": "| 코드 | 명칭 | 단가 |  \n| AA100 | 진찰료 | 15,000 |\t\n합계\t1건\n",
    },
    {
        "name": "v3-crlf-bom",
        "description": "BOM + CRLF + NFD 한글 — 전체 정규화 경로 검증",
        "body_text": "﻿고시 개정\r\n\r\n무한글 NFD 조합\r\n끝.\r\n\r\n\r\n",
    },
]


def main() -> None:
    out = {
        "spec": "ERP-AI content_hash 정규화 v1.0 (BOM제거→NFC→LF→행끝공백제거→끝개행1→SHA-256)",
        "generated_by": "hospital-erp services/core erp.regulatory.content_hash",
        "vectors": [
            {**v, "expected_hash": content_hash(v["body_text"])} for v in VECTORS
        ],
    }
    targets = [
        Path(__file__).parent.parent.parent.parent
        / "docs/01-plan/references/content-hash-vectors.json",
        Path("/Users/user/LLMManager/docs/01-plan/references/erp-content-hash-vectors.json"),
    ]
    for t in targets:
        if t.parent.exists():
            t.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
            print(f"✓ {t}")
        else:
            print(f"skip (디렉터리 없음): {t}")


if __name__ == "__main__":
    main()
