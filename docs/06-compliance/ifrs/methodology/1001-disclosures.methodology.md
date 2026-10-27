# 측정방법론서 — 재무제표 주석 (K-IFRS 1001)

**상태**: Draft · **작성일**: 2026-06-22 · **구현**: `services/core/src/erp/finance/disclosure_service.py`

## 1. 자동 정량주석 산식
- 계정 기간 증감 `_movement(code, start, end)` = (기초전 d/cr, 기중 d/cr).
- **유형자산 증감(1016)**: 취득원가(1710·1720) 기초=Σ(d−cr 전), 취득=Σ기중d, 처분=Σ기중cr;
  감가누계(1810·1825) 기초=Σ(cr−d 전), 상각=기중cr, 처분=기중d; 장부가=취득원가−감가누계.
- **충당부채 증감(1037)**: 2120·1190·1159 각 기초=cr−d(전), 전입=기중cr, 감소=기중d, 기말=기초+전입−감소.

## 2. 엔드포인트
- `GET /finance/disclosures?year=` (자동+저장), `PUT /finance/disclosures?year=`(정성주석 upsert).

## 3. 예시 (테스트 근거)
- 장비취득 1천만·감가 100만 → 취득원가 기말 1천만·감가누계 100만·장부가 900만.
- 퇴직충당 전입 300만 → 기말 300만.
- 검증: `tests/test_disclosures.py::test_ppe_and_provision_movement`, `::test_save_qualitative_note`.

## 4. 증빙 (재현)
1. `GET /finance/disclosures?year=YYYY` → ppe_movement·provision_movement payload 확인.
2. 정성: `PUT /finance/disclosures?year=YYYY` {note_key, title, payload}.
- 마이그: `0091_disclosure_notes.py`.
