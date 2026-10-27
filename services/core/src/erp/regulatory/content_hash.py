"""content_hash 정규화 7항 — AI 연동 스펙 v1.0 확정 (weve-ai-integration-reply.md B-4).

1. 인코딩: UTF-8 (BOM 제거)
2. 유니코드 정규화: NFC
3. 개행: CRLF/CR → LF 통일
4. 각 행 끝 trailing whitespace 제거
5. 문서 끝: 마지막 개행 1개로 정규화
6. 그 외 공백·연속 공백은 보존 (표 서식 보호)
7. 해시 대상: 정규화된 body_text 바이트열 전체 (SHA-256)
"""
import hashlib
import unicodedata

_BOM = "﻿"


def normalize_body(text: str) -> str:
    if text.startswith(_BOM):                      # 1. BOM 제거
        text = text[len(_BOM):]
    text = unicodedata.normalize("NFC", text)      # 2. NFC
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # 3. 개행 LF
    lines = [line.rstrip(" \t") for line in text.split("\n")]  # 4. 행끝 공백 제거
    text = "\n".join(lines)
    text = text.rstrip("\n") + "\n"                # 5. 문서 끝 개행 1개
    return text                                    # 6. 그 외 공백 보존


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_body(text).encode("utf-8")).hexdigest()  # 7
