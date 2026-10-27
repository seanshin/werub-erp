# 기관 규제 파라미터 관리 (간호등급 등) — 기획

> 목적: 산정 엔진은 완성됐으나 "기관 고정 파라미터·주기 실측치"를 관리자가 설정·저장할 곳이 없어
> 자동화·대시보드가 막힌 갭 해소. 간호등급을 플래그십으로, 유사 규제 파라미터를 담는 관리 기반 구축.
> 2026-07-15.

## 1. 문제

간호등급 산정 엔진(`regulatory/nursing_grade.py`·`nursing_monitor.py`)은 고시 원문대로 **완성**돼 있다
(종별 등급 구간·산정기간·반올림/절사·충원 시뮬레이션). 그러나 산식이 필요로 하는 **입력 정보**가
관리되는 곳이 없다:

| 필요 정보 | 현재 | 갭 |
|---|---|---|
| **종별**(hospital_type) | 매 API 호출 쿼리파라미터·기본 "HOSPITAL" | 기관 고정값인데 **저장 안 됨** → 대시보드 위젯이 종별을 모름(G2 미해소 표면) |
| **허가병상수** | 없음 | 의원급(병상 기준) 산정 불가 |
| **분기 재원환자수** | 매번 수동 파라미터·billing 추정 | **분기별 실측 저장처 없음** (HIS 0시 census는 M0a 대기) |
| (지연) 간호사 주 근로시간·산정제외 대상 | HR 스키마 부재 | 별도 HR 확장 — 본 기획 범위 밖 |

## 2. 설계 (기존 패턴 재사용)

**싱글턴 정책형**(OrgSettings 패턴) + **다중행 마스터형**(Holiday 패턴)을 규제 도메인에 적용.

### 2.1 기관 규제 프로필 — 싱글턴 `regulatory.institution_profile`
- `id=1` CHECK 싱글턴. 컬럼: `hospital_type`(TERTIARY|GENERAL|HOSPITAL|CLINIC_BED_BASIS·기본 HOSPITAL),
  `licensed_beds`(허가병상·의원급 산정용·nullable), `updated_by/at`.
- `InstitutionProfileService`: `get`·`effective`(미설정 시 안전 기본 HOSPITAL)·`upsert`(화이트리스트·검증).

### 2.2 분기 재원환자수 대장 — 다중행 `regulatory.nursing_census`
- 자연키 `period_start` UNIQUE(분기창 시작일). 컬럼: `period_start/period_end`·`patient_days`·`note`·`entered_by/at`.
- `NursingCensusService`: `upsert`(기간별)·`list`·`get_for(period_start)`·`delete`.

### 2.3 NursingMonitor 통합
- `monitor(hospital_type=None, patient_days=None)`: 파라미터 미지정 시
  **종별=프로필**, **재원일수 우선순위: 수동 파라미터 > 저장 census > HIS census > billing 추정**.
- 파라미터 지정 시 종전대로(하위호환).

### 2.4 라우터 (regulatory/router.py) — `regulatory.master` 스코프 재사용
- `GET/PUT /regulatory/institution-profile` (read=staff·write=ADMIN)
- `GET/POST/DELETE /regulatory/nursing-census` (read=staff·write=ADMIN)
- 기존 `/regulatory/nursing-grade/monitor` 기본 hospital_type을 프로필에서 해석.

### 2.5 프론트
- `/regulatory/nursing-grade` 화면에 **설정 패널**(종별·허가병상 저장 + 분기 census 입력·목록·삭제) 추가.
  기존 org/admin 컨벤션(useQuery/useMutation·dirty·toast·Card/Input/Button·useConfirm 삭제).
- **대시보드 간호등급 위젯**(DIR↑): 현재 등급·비율 표시 → G2 placeholder 문구 제거.

## 3. 범위 · 비범위

- **범위**: 위 2.1~2.5 (마이그 0198·모델·서비스·라우터·프론트·대시보드·테스트).
- **비범위(외부/후속)**: HIS 0시 census 연계(M0a 계약)·간호사 주 근로시간 FTE 필드·산정 제외 대상 HR 속성·
  실무 수기 대조 1회. 본 기획은 이들이 도착하기 전까지 **수동 설정으로 산정을 가능케** 한다.

## 4. 확장성

`institution_profile`은 향후 다른 기관 고정 규제 파라미터(종별가산 기준·병상 종류별 수 등)를 담는 자리로
확장된다. `nursing_census` 패턴은 다른 "분기 실측치" 대장으로 복제 가능.
