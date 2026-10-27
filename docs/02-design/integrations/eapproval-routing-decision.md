# 전자결재 연동 경로 결정 (ADR) — ERP 직접 vs HIS 경유

> **결정 사항**: ERP 인사 결재를 그룹웨어 전자결재(SaaS)에 연동할 때, **ERP가 SaaS에 직접
> 연동**할 것인가, **이미 SaaS와 연동된 HIS를 경유(중계)**할 것인가.
> **상태**: ✅ **확정 — Option B (HIS 경유)** · **작성일**: 2026-06-09 (Hospital RUN 코드 분석으로 전제 확인)

---

## 0. Hospital RUN(HIS) 코드 분석 결과 — 전제 확인 ✅

ADR의 핵심 전제("HIS 기존 연동이 전자결재 포함?")를 HIS 코드(`/Users/user/GitHub_project/Hospital RUN`)
분석으로 확인. **결론: 전부 YES — Option B 확정.**

| 확인 항목 | 결과 | 근거(HIS 코드) |
|-----------|:---:|------|
| 그룹웨어 정체 | **WeRU.B Clinic** (`clinic.example.org`) — **ERP가 이미 직원동기화로 쓰는 그 시스템** | clinic-integration 모듈 |
| HIS 전자결재(W.Sign) 연동 | ✅ **구현됨(~85%)** | `clinic-api.service.ts` `requestApproval()`/`getApprovalStatus()`, `POST /api/clinic/his/wsign/request`·`GET .../status/:docId` |
| 결재 결과 콜백 | ✅ webhook `wsign.approved`/`wsign.rejected` → `/api/v1/webhooks/clinic` (HMAC+timestamp) | controller webhook |
| **결재선 자동 추천** | ✅ **HIS 조직도 기반** `GET /api/clinic/his/organization/approvers` | 조직도 7 API |
| 인사정보 교환 | ✅ **양방향**(직원 pull/push, 조직도, 역할매핑) — email/사번 키, `Staff.clinicUserId` | `syncStaff`/`pushStaff` |
| 기관코드·인증 | ✅ `CLINIC_HOSPITAL_CODE`(=**WRB-DEMO-M66M**, ERP와 동일)·`CLINIC_API_KEY`·`X-Hospital-Code`, `hospital-register` webhook 자동 세팅 | clinic-api.service `getHeaders()` |
| 결재 양식 | ✅ `leave`(휴가)·`medical-purchase`·`general`·`medical` 템플릿 보유 | W.Sign templates |

**시사점**:
1. 그룹웨어 = **Clinic**(ERP가 이미 `CLINIC_API_KEY`·기관코드 `WRB-DEMO-M66M`로 직원동기화 중).
2. HIS가 이미 **W.Sign 전자결재 + 결재선 추천(조직도) + 휴가 템플릿**을 보유 → **ERP가 재구현할 이유 없음**.
3. 결재선 권위가 **HIS 조직도**에 있음 → ERP→HIS 경유가 데이터 흐름상 자연스러움.
4. 기관코드가 동일 테넌트 → ERP 별도 발급 불필요(B의 핵심 이점 실증).

---

## 1. 배경

- 그룹웨어 전자결재는 **SaaS** → 연동에 **기관코드·API 인증정보·엔드포인트** 등 테넌트 설정이 필요.
- **현재 HIS ↔ 그룹웨어가 이미 연동**되어 있음(기관코드·인증·채널 확보된 상태).
- ERP는 HIS 생태계의 **백오피스**로, 이미 HIS에 **SSO·공유DB·동거**로 의존.

핵심 질문: SaaS와 **새 연동 계정/기관코드를 ERP가 또 발급**받아 직접 연동할 것인가, 아니면
**HIS의 기존 연동을 재사용**(HIS가 중계)할 것인가.

---

## 2. 옵션 비교

### Option A — ERP ⇄ 전자결재 SaaS (직접)
```
ERP ──상신/콜백──▶ 전자결재 SaaS
```
| 장점 | 단점 |
|------|------|
| HIS 무관하게 독립 동작 | **SaaS 테넌트 설정 이중화**(ERP용 기관코드·키·갱신 별도 발급·관리) |
| SaaS와 직접 계약·전체 제어 | ERP가 SaaS 관리화면(기관코드 세팅) **자체 구축 필요** |
| | 동일 기관에 **연동 클라이언트 2개**(HIS·ERP) → 중복 유지보수·벤더 혼선 |
| | ERP 외부 통신면 증가(SaaS 직접 호출) |

### Option B — ERP ⇄ HIS ⇄ 전자결재 SaaS (HIS 경유/중계) ★권장★
```
ERP ──상신/콜백──▶ HIS(중계) ──기존 연동──▶ 전자결재 SaaS
```
| 장점 | 단점 |
|------|------|
| **SaaS 자격증명·기관코드 일원화**(HIS 한 곳) — ERP는 재발급 불필요 | HIS가 **전자결재 중계 인터페이스 제공** 필요(개발) |
| 단일 연동점 = 벤더 계약·갱신·모니터링 일원화 | ERP 결재가 HIS 가용성에 의존 |
| ERP↔HIS는 **이미 신뢰 채널**(SSO·동거) → 증분 연동 | HIS 기존 연동이 전자결재까지 **포함하는지 확인 필요** |
| ERP 외부 통신면 최소(내부 HIS만 호출) | |
| 아키텍처 일관성: 환자·수납처럼 **ERP→HIS** 패턴 동일 | |

---

## 3. 권장안

### ✅ **Option B (HIS 경유) 확정** (전제 §0에서 확인 완료)

**근거**
1. **SaaS 테넌트 일원화**: 기관코드·API키·갱신·벤더계약을 HIS 한 곳에서 관리. ERP가 동일 SaaS에
   별도 연동 계정을 또 발급·세팅하는 **이중화·중복 유지보수를 제거**.
2. **아키텍처 일관성**: ERP는 이미 HIS의 백오피스(SSO·공유DB·환자/수납 데이터 ERP→HIS 수신).
   전자결재도 같은 **ERP→HIS** 경로로 통일하면 연동 토폴로지가 단순.
3. **관리화면 부담 감소**: SaaS 연결정보(기관코드 등)는 HIS가 보유 → **ERP는 SaaS 관리화면 불필요**,
   "어느 HIS 중계 엔드포인트를 쓸지" 정도의 **경량 설정**만.
4. **보안 면적 최소화**: ERP는 외부 SaaS를 직접 호출하지 않고 내부 HIS만 호출(SSRF·자격증명 노출 축소).

**전제 확인 완료(§0)**: HIS가 이미 W.Sign 전자결재 + 결재선 추천 + 휴가 템플릿 + 양방향 인사연동 +
기관코드(ERP와 동일 `WRB-DEMO-M66M`)를 보유. → **"HIS에게 요청하는 것이 더 바람직"하다는 판단이 옳음.
B 확정.** §6(예외분기)은 해당 없음.

---

## 4. Option B 채택 시 — **HIS에 요청할 내용** (중계 인터페이스)

HIS는 이미 Clinic W.Sign을 호출(`requestApproval`/`getApprovalStatus` + `wsign.*` 콜백 수신)하므로,
ERP는 **HIS가 보유한 그 기능을 ERP-facing 릴레이로 얇게 노출**해 달라고 요청하면 됨(신규 SaaS 연동 불요):

| | 인터페이스 | 요청 대상 | 비고 |
|--|-----------|----------|------|
| ① | **결재 상신 중계** `POST {HIS}/api/erp/eapproval/submit` | HIS | HIS가 기관코드·인증 부착 후 SaaS 상신 |
| ② | **결과 콜백 중계** `POST {ERP}/integrations/eapproval/callback` | HIS→ERP | SaaS→HIS→ERP 전달(HMAC) |
| ③ | **상태 조회 중계** `GET {HIS}/api/erp/eapproval/{id}` | HIS | 폴백 |
| ④ | **마스터 제공** 결재양식·사용자(사번/email) 매핑 | HIS | HIS가 SaaS에서 받아 ERP에 제공 |

추가 요청:
- HIS가 보유한 **기관코드·테넌트 설정 재사용**(ERP 재발급 불필요) 확인.
- ERP↔HIS 인증은 **기존 SSO/내부 채널 자격증명 재사용**(별도 SaaS 키 불요).
- HIS 측 결재 결과 → ERP 콜백 **재시도·서명** 정책.

---

## 5. 관리화면(Admin) 설계 — 두 경우 공통(범위만 다름)

SaaS·연동 설정을 운영자가 세팅하는 ERP 관리화면. **Option B면 경량, A면 전체.**

| 설정 항목 | Option B (HIS 경유) | Option A (직접) |
|-----------|---------------------|-----------------|
| 연동 모드 | `VIA_HIS` 선택 | `DIRECT` 선택 |
| 기관코드 | (HIS 보유 — 표시만/불요) | **ERP 입력·저장** |
| SaaS 엔드포인트·API키 | 불요 | **입력**(암호화 저장) |
| HIS 중계 엔드포인트 | **입력** | 불요 |
| 콜백 시크릿(HMAC) | 입력 | 입력 |
| 결재양식↔문서종류 매핑 | 매핑표 편집 | 매핑표 편집 |
| 사용자 매핑 키 | email/사번 선택 | email/사번 선택 |
| 연동 on/off·문서종류별 활성 | 토글 | 토글 |
| 연동 상태·최근 상신/실패 | 모니터 | 모니터 |

> 화면 위치: `재무/시스템` 영역의 **관리 > 연동 설정(전자결재)**. 권한 ADMIN. 비밀값은 600 .env가
> 아니라 **암호화 컬럼 저장 + 마스킹 표시**(기존 DATA_ENC_KEY 재사용).

---

## 6. 만약 HIS가 전자결재 미연동이라면 (예외 분기)

HIS 기존 연동이 전자결재를 **포함하지 않는** 경우:
- **여전히 B 우선** 권장 — 단일 연동점·테넌트 일원화 가치가 큼. HIS가 중계 레이어를 신규 구축.
- HIS 개발여력 부족·일정 촉박 시 **A(직접) 임시 채택** 가능하나, 기관코드·키를 ERP가 별도 발급받아야
  하고 관리화면 풀구현 필요 → **중복비용** 발생. 추후 B로 이관 권장.

---

## 7. 결론 · 다음 단계

1. **결정**: **Option B(HIS 경유) 확정**. ERP는 Clinic W.Sign에 직접 붙지 않고 **HIS가 기보유한 W.Sign
   연동을 ERP-facing 릴레이로 재사용**. (코드 분석으로 HIS 전자결재·결재선·인사연동·기관코드 보유 확인.)
2. **HIS 팀에 요청**(§4): ERP-facing 릴레이 4종(상신·결과콜백·상태조회·조직도/결재선·사용자매핑). HIS의
   `requestApproval`/`getApprovalStatus`/`wsign.*` 콜백을 ERP에 노출 + 결재양식에 `leave` 등 매핑.
3. **관리화면**: **경량**(HIS 릴레이 엔드포인트·문서종류↔W.Sign 템플릿 매핑·on/off·모니터). 기관코드/
   SaaS키는 HIS·ERP 공통(`WRB-DEMO-M66M`) — ERP 별도 발급 불요.
4. **ERP 구현**: 콜백 수신 엔드포인트 + 상신 어댑터(기존 Clinic/HIS ACL 규약 재사용) + `hr.leave_requests`에
   `approval_id`·`approval_status`·`view_url` 컬럼.
5. **PoC**: `HR_LEAVE`(휴가) → W.Sign `leave` 템플릿으로 상신→결재→콜백 E2E.
6. 본 ADR + 인터페이스 요청서를 **HIS 팀에 전달**(SaaS가 아니라 HIS 대상).
