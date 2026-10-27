# 전자서명 계약 단계 흐름 (e-sign #6) — 직원·의료진 / 외부 거래처

> 최종 2026-06-28. 의료진/직원 계약 = **Track 1(HIS 경유)**, 외부 거래처 = **Track A(sign 직접)**.
> 상태 머신은 동일: `null(미발송) → SENT(서명대기) → COMPLETED(완료)`.

## Track 1 — 직원·의료진 계약 (HIS 경유)

```
[0] 계약 등록 ──► [1] 발의 ──► [2] 서명 ──► [3] 완료·미러 ──► [4] 조회·증빙
   (ERP)         (ERP→HIS)    (의료진/    (sign→HIS→ERP)     (ERP 웹UI)
                              병원장)        자동
   sign_status:  null ───────► SENT ──────────────────────► COMPLETED
```

| 단계 | 행위자 | 동작 | ERP 상태/필드 |
| --- | --- | --- | --- |
| 0. 계약 등록 | HR/ADMIN | `/erp/hr/medical-contracts` 등록(기존 ACTIVE 자동 종료·승계) | `status=ACTIVE`, `sign_status=null` |
| 1. 발의 | HR/ADMIN | 웹UI "전자서명 개시"(채널) → ERP snapshot → HIS `request-sign` → HIS 발급·PDF 렌더·documentHash·sign 제출 | `sign_status=SENT`, `sign_envelope_id=<reqId>`, `sign_doc_ref=<issueNo>`, portal_url 반환 |
| 2. 서명 | 의료진(+병원장) | 채널별 서명 · 다자면 `signOrder` 순차 게이팅 | (대기) |
| 3. 완료·미러 | 시스템(자동) | 전 봉투 완료 → `sign.completed` → ERP 인박스(`/integrations/his/sign/webhook`) → `erpContractId` 매핑 | `sign_status=COMPLETED`, `signed_at`, `sign_cert_serial`, `sign_assurance` + 완료본 회수(Attachment)→`sign_doc_attachment_id` |
| 4. 조회·증빙 | HR/ADMIN | 계약이력 "전자서명" 열(배지) + "서류" 열(완료본 PDF) | 읽기 |

### 2단계 — 서명 채널 (의료진 유형별)
| 채널 | 대상 | 방식 |
| --- | --- | --- |
| PORTAL | 신규 입사·HIS 미등록 포함 | 이메일/문자 deep-link → 포털 서명, 신원 임시 인증서 자동 발급(SSO 불요) |
| STAFF | 기존 등록 직원 | HIS SSO 로그인 서명(staff-token `certSerial`) |
| DIRECT | 시스템·키 제출 | API 키 제출 |

### 상태 머신
```
null(미발송) ──발의──► SENT(서명 대기) ──전봉투 서명완료──► COMPLETED(완료)
                         └─ 재발의(미완료 시 새 봉투로 갱신)
```

### 역할 분담 (SoT)
- **ERP** = 계약 경영데이터(금액·기간·정산) SoT + 발의 트리거 + 결과 미러·완료본 보관
- **HIS** = 문서·PDF 렌더·오케스트레이션(snapshot→PDF)
- **sign** = 트러스트(인증서·서명·감사증적)

## Track A — 외부 거래처 (sign 직접)
동일 상태 머신. 경로만 다름:
```
[0] 위탁계약 등록 ──► [1] 발의(ERP→sign 직접) ──► [2] 거래처 포털 서명 ──► [3] 완료·미러
   (ERP, PDF 보관)    enroll→requests→portal-tokens   (deep-link)         (sign→ERP webhook)
```
- ERP가 **계약 PDF 직접 보관**(SHA-256만 sign 송신) → ERP→`sign-core:4000`(docker_default) → 거래처
  포털 서명 → sign→ERP webhook(`/integrations/sign/webhook`, HMAC) → 미러 COMPLETED.
- HIS 미경유. 개시 EP `POST /integrations/sign/outsourcing-contracts/{id}/request`(PDF 업로드).

## 네트워킹 (확정)
- sign→ERP(완료 통지): 공개 nginx split `https://his.example.org/erp/api/v1/integrations/sign/webhook`.
- ERP→sign(개시): `http://sign-core:4000/v1`(공유망 docker_default DNS).
- ERP→HIS(발의): `https://his.example.org/api/v1/sign-integration/erp/request-sign`(공개 https).

## 완료 (LIVE)
1. **다자 봉투(병원장 공동/순차) 노출** — ✅ LIVE. 개시 EP가 JSON body(`channel`·`signers`·`force`)로
   전환, signers[] HIS 패스스루 + 웹UI 공동서명자 입력(PR#477·#479). 봉투 채널 STAFF→DIRECT 매핑(PR#482).
2. **STAFF 채널 certSerial** — ✅ 해소. HIS `prepareSigners`(werubyHIS PR#117)가 STAFF 서명자
   `externalSignerRef(erp:staff:<employee_no>)`→Staff 역해소→ensureStaffCert로 certSerial 내부 주입.
   ERP는 certSerial 미공급(employee_no=HIS employeeNumber로 역해소).
3. **재발의 멱등** — ✅ LIVE. SENT+envelope 존재 시 `force` 없으면 기존 봉투 반환(orphan 방지, PR#477).
4. **PORTAL deep-link 발송** — ✅ ERP 발송 확정(sign D3). 다자 봉투 응답 `signers[].portalUrl`을
   서명자별 `portal_links`로 노출(웹 복사 UI, PR#482). HIS의 per-signer relay는 별도 요청 진행 중.

## 향후 보완 후보 (go-live 보류)
5. **완료(COMPLETED) 종단 합동 검증·P7 테스트데이터 정리·P8 키 회전·assurance HSM_ANCHORED cutover**
   — sign과 go-live 시 합동.

---
> 관련: `erp-notice-esign6-e2e-final-2026-06-28.md`(E2E 결과),
> `erp-reply-his-esign6-origination-2026-06-28.md`(발의 모델).
