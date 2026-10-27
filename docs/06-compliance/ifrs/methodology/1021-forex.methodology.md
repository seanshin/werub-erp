# 측정방법론서 — 외화환산 (K-IFRS 1021)

**상태**: Draft · **작성일**: 2026-06-22 · **구현**: `services/core/src/erp/finance/forex_service.py`

## 1. 산식
```
환산 KRW = 외화잔액 × 기말환율
환산차 = 환산 KRW − GL 장부가(외화예금 계정, 차변잔액)
delta>0 → 차) 외화예금 GL / 대) 외화환산이익 4650
delta<0 → 차) 외화환산손실 5670 / 대) 외화예금 GL
```
- 대상: corporate_accounts(통화≠KRW)의 gl_account_code. 멱등(계좌·기간). 구현 `ForexTranslationService`.

## 2. 엔드포인트
- `GET /finance/forex/preview`·`POST /finance/forex/post`(account_id·fc_balance·rate·yyyymm, finance.periods).

## 3. 예시 (테스트 근거)
- USD 10,000 장부 1,200만(@1,200) → 기말 @1,300 → 환산 1,300만 → 외화환산이익 100만.
- 검증: `tests/test_forex_translation.py::test_fx_gain`, `::test_krw_account_rejected`.

## 4. 한계
- 환율 입력(자동 피드 후속). 실현 외환손익(4630/5660)과 구분(미실현 환산).
