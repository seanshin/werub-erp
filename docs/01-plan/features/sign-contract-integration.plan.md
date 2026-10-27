# 전자서명 계약서 연동 준비서 (ERP ← HIS ← sign)

> **성격**: 구현 계획이 아니라 **연동 준비서(readiness)**. 계약서·동의서의 핵심 기능(문서 생성·전자서명·
> 인증서·검증·증빙)은 **HIS가 sign 트러스트로 구축 중**이며, **ERP는 이를 사용하는 측**이다. 본 문서는
> ERP가 추후 연동할 때 필요한 준비 항목·역할 분담·HIS에 요청할 것을 정리한다. **본 사이클에 코드·마이그
> 변경 없음**(HIS 계약 API 확정 전 조급 구현 회피 — [[erp-dev-flow-principle]]).
>
> **작성일**: 2026-06-15 · **버전**: v0.1(준비) · **상태**: 대기(HIS 계약 기능 확정 의존)
> **연관**: sign `docs/05-handoff/his-integration-guide.md`(동의서 통합 가이드)·`sign-to-his-external-e2e-answers.md`·`openapi.json`, ERP hr.medical_contracts·scm.outsourcing_contracts

---

## 0. 결정 / 모델 (한 장)

```
ERP                          HIS (문서·UX·오케스트레이션)        sign (트러스트)
─ 계약 메타·금액·기간 보관    ─ 계약/동의서 PDF 생성·서명 UX       ─ 인증서 발급·서명·검증·감사
─ 서명결과 미러(읽기전용) ◀── ─ documentHash → sign 호출      ──▶ ─ webhook 완료 통지
─ HIS 문서 ref 링크           ─ serial·상태 미러 + ERP에 전달        (문서 미보관, 해시만)
```

- **핵심 기능은 HIS 소유**: 문서 생성·전자서명 플로우(enroll→sign-request→서명→webhook)·검증·증빙은
  HIS가 sign API로 구축(가이드 §3~§6). **ERP는 자체 서명/인증서 발급을 구현하지 않는다.**
- **ERP 역할 = 사용 + 미러**: 계약(의료진·위수탁)에 대해 (a) HIS에 서명 요청을 *위임/연계*하고,
  (b) 완료 결과(`signCertSerial`·상태·assuranceLevel)를 **읽기전용 미러**로 보관, (c) HIS 문서로 링크.
- 단일 SoT: 서명 트러스트=sign, 문서·UX=HIS, 계약 경영데이터=ERP.

---

## 1. ERP 계약 자산 (현재 — 미러 대상 후보)

| 계약 | 모델 | 위치 | 비고 |
|------|------|------|------|
| 의료진 개별 계약 | `MedicalStaffContract` (hr.medical_contracts) | `hr/contract_service.py` | 기본급·성과급·당직(P4) |
| 위수탁 계약 | `OutsourcingContract` (scm.outsourcing_contracts) | scm | 급식·세탁·폐기물·보안 등 월 위탁료 |

> 두 계약 모두 현재 **서명 관련 필드 없음**. 추후 연동 시 미러 컬럼 추가 대상(아래 §3).

---

## 2. 역할 분담 (확정 방향)

| 항목 | HIS | sign | ERP |
|------|:---:|:---:|:---:|
| 계약/동의서 문서(PDF) 생성·보관 | ✅ | — | (메타만) |
| documentHash(SHA-256) 계산 | ✅ | — | — |
| 인증서 발급·키 수탁 | — | ✅ | — |
| 서명 플로우·UX(환자/의료진) | ✅ | (트러스트) | — |
| 완료 webhook 수신 | ✅(1차) | (발화) | (HIS 경유 미러 수신) |
| 검증/증빙(verify·audit-cert) | ✅ 호출 | ✅ | (링크 표시) |
| 계약 경영데이터(금액·기간·정산) | — | — | ✅ |
| **서명결과 미러(serial·상태)** | ✅ | — | ✅(읽기전용) |

→ ERP는 **HIS의 계약 서명 API/이벤트를 사용**하고 결과만 미러. sign 직접 호출은 HIS가 담당(원칙).
  (단, 운영 합의 시 ERP가 sign을 직접 호출하는 옵션도 가이드상 가능 — 추후 결정 §6.)

---

## 3. ERP 측 추후 구현 항목 (준비 — 본 사이클 미구현)

> HIS 계약 API·미러 payload 확정 후 착수. 아래는 **무엇을 준비할지**의 명세일 뿐, 지금 코드화하지 않음.

1. **미러 필드** (계약 모델 2종에 추가 — 마이그 1건, 추후):
   `sign_cert_serial`(읽기전용), `sign_status`(UNSIGNED/PENDING/SIGNED/WITHDRAWN/EXPIRED),
   `signed_at`, `sign_assurance_level`(SOFTWARE/HSM_ANCHORED), `external_doc_id`(=issueNo),
   `his_document_ref`(HIS 문서 링크). **null일 때만 채움**(멱등 미러 — 가이드 §5).
2. **수신 경로**: HIS→ERP 미러 통지. 기존 인박스(`his_webhook_router`·`his_inbound_events`) 재사용 후보
   — 신규 도메인/이벤트(예: `contract.signed`) 추가. HMAC·eventId 멱등은 기존 패턴 그대로.
3. **config 플레이스홀더**(대기 모드): HIS 계약 API base/key (미설정 시 대기 — fee_api_key 패턴).
   ※ sign 직접 호출을 안 하면 `sign.*` 시크릿은 ERP에 불요(HIS 보유).
4. **계약서 documentHash**: ERP가 계약 PDF를 생성하는 경우에만 SHA-256 산출. 문서 생성이 HIS면 불요.
5. **UI**: 계약 화면에 서명상태 배지·검증/증빙(audit-certificate) 링크(HIS/sign URL) 표시.

---

## 4. HIS에 요청할 것 (연동 계약 — 확정 필요)

- [ ] **계약 서명 API**: ERP 계약(의료진·위수탁)에 대한 서명 요청을 HIS가 받는 엔드포인트/이벤트 규약
      (또는 ERP가 sign을 직접 호출할지 역할 확정).
- [ ] **미러 payload**: 완료 통지 시 ERP가 받을 필드(`externalDocId`·`signCertSerial`·`status`·
      `assuranceLevel`·`auditCertUri`) + **식별자 매핑**(ERP contract id ↔ externalSignerRef/externalDocId).
- [ ] **문서 소유**: 계약서 PDF 생성·보관 주체(HIS 권장) 및 ERP 링크 방식.
- [ ] **인증/시크릿**: 수신 webhook HMAC secret, (직접호출 시) sign apiKey 안전채널 수령.
- [ ] **SSO(의료진 서명)**: 의료진 계약 서명 시 staff-token(aud=sign) 발급 주체.

---

## 5. 미래 흐름 (참고 — 동의서 가이드 §3 차용)

```
① 계약 생성(ERP) → HIS에 서명요청 위임(또는 ERP가 sign 직접: POST /v1/sign-requests, documentHash·issueNo)
② 의료진/거래처 서명(HIS UX / portalUrl)
③ sign → HIS webhook(request.completed, HMAC) → HIS 미러
④ HIS → ERP 미러 통지(contract.signed) → ERP: sign_cert_serial·status=SIGNED 기록(멱등)
⑤ 검증/증빙: verify·audit-certificate 링크 표시
```

---

## 6. 전제 / 게이트 (외부 의존)

| 게이트 | 의존 | 상태 |
|--------|------|------|
| HIS 계약·동의서 기능 | HIS 팀 구축 중 | ⏳ |
| 역할(ERP가 sign 직접호출 vs HIS 경유) | HIS·ERP 합의 | ⏳ |
| 미러 payload·식별자 매핑 | HIS API 확정 | ⏳ |
| 시크릿(webhook/apiKey) | 안전채널 수령 | ⏳ |

> 전부 미충족 → 본 준비서는 **명세·체크리스트만**. 충족 시 §3 구현 착수(마이그+미러+수신경로+UI).

---

## 7. 구현 체크리스트 (추후 — 게이트 해소 후)

- [ ] 계약 모델 2종 미러 필드 마이그 + 읽기전용 노출
- [ ] HIS→ERP 미러 수신(인박스 신규 이벤트 `contract.signed`, HMAC·멱등)
- [ ] config(대기 모드) + 미구성 시 무중단 차단
- [ ] (필요 시) 계약 PDF documentHash 산출
- [ ] 계약 UI 서명상태·검증/증빙 링크
- [ ] 테스트(미러 멱등·식별자 매핑·미구성 대기)

---

## 관련 문서
- sign 동의서 통합 가이드: `seanshin/sign:docs/05-handoff/his-integration-guide.md`
- 계약 상세/기계가독: `sign-to-his-external-e2e-answers.md`·`openapi.json`
- ERP↔HIS 생태계: [[erp-his-ecosystem]] · 운영 배포: [[erp-prod-deploy]]
