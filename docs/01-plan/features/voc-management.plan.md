# 환자 민원·CS(VOC) 관리 — 기획 v1 (내부운영 갭 감사 Tier3 ①)

> 2026-07-23. Tier3 신규 도메인 첫 착수(사용자 선택). 의료기관 인증평가(민원·고충처리 체계)와
> 직결되는 환자 접점 도메인 — 접수→분류→배정→처리→회신→종결 생애주기 + 통계.

## 범위 (v1)

- **접수**: 원내 접수(창구/전화/서면/홈페이지 전달/기타). 환자 연결은 선택(`patient_id` — 익명·보호자
  접수 허용). 비연결 접수의 회신 연락처는 AES-GCM 암호화 저장(환자 연결 시 별도 저장 안 함 — 최소수집).
- **분류**: 유형(불만/고충/제안/칭찬/상담) · 카테고리(진료/원무행정/시설환경/직원응대/비용/개인정보/기타)
  · 심각도(LOW/MEDIUM/HIGH — HIGH=경영진 보고 대상).
- **처리 흐름**: `RECEIVED → IN_PROGRESS(담당 배정) → ANSWERED(회신 기록) → CLOSED`, 종결 후
  `재오픈(reopen_count)` 가능. 모든 전이는 `voc_actions` 이력 + 감사로그.
- **처리 기한**: 접수일 + 14일(민원 처리에 관한 법률 준용 내부 SLA) → `due_on`. 기한 경과 미종결은
  목록·통계에서 식별(OVERDUE). ※ 컴플라이언스 만기 대시보드 연동은 후속.
- **환자안전 연계**: 안전사고 성격이면 기존 `regulatory.patient_safety_incidents` id를 참조 필드로
  연결(도메인 경계상 하드 FK/import 없이 번호 참조).
- **통계**: 기간별 건수·유형/카테고리/부서별 분포·평균 처리일수·기한초과 건수 — 인증평가 제출 자료 소스.

## 비범위 (후속)

환자 포털/외부 접수 채널(공개 폼) · SMS/알림톡 회신 발송 · 만기 대시보드 소스 등록 · VOC→개선과제
추적(CAPA) · 첨부파일(범용 Attachment 연결은 필요 시 entity_type 추가로 소규모).

## 데이터 모델 (schema `patient` · 마이그 0223)

- `voc_cases`: case_no(민원-YYYY-NNNN·`lock_doc_no` 직렬화) · channel · vtype · category · severity ·
  patient_id? · contact_enc?(AES-GCM) · title · body · department_id?(귀속부서) · assignee_user_id? ·
  status · received_on · due_on · answered_at? · answer_body? · closed_at? · reopen_count ·
  safety_incident_id? · created_by.
- `voc_actions`: case_id FK · action(ASSIGN/COMMENT/ANSWER/CLOSE/REOPEN) · actor · note · at.

## RBAC (`patient.voc`)

- read: DIRECTOR/ADMIN ALL · MANAGER DEPT(귀속부서) · STAFF SELF(본인 접수분) — 민원 본문=민감.
- write(접수·의견): WORKSTATION 제외 전 역할(접수는 전 직원 창구 업무).
- admin(배정·회신·종결·재오픈): DIRECTOR/ADMIN ALL · MANAGER DEPT.
- 상세 조회는 `record_access`(접속기록) — 환자 PII 접점.

## EP (`/patients/voc*`)

POST /voc(접수) · GET /voc(status/category/vtype/dept/overdue 필터) · GET /voc/{id}(이력 포함) ·
POST /voc/{id}/assign · POST /voc/{id}/comment · POST /voc/{id}/answer · POST /voc/{id}/close ·
POST /voc/{id}/reopen · GET /voc/stats(기간별).

## FE (후속 PR)

`/patients/voc` — 접수 폼(환자 검색 연결 선택)·목록(상태/기한 배지)·상세(이력 타임라인·처리 액션)·
통계 카드. 메뉴 `원무 > 민원(VOC)` `_ALL`. v1.203 FE 체크리스트(isPending·confirm·QueryErrorBanner·
dirty) 준수.
