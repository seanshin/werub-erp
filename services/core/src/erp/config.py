"""애플리케이션 설정 — Plan §7.2 환경 변수."""
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings

_KNOWN_ENVS = ("dev", "test", "prod")


class Settings(BaseSettings):
    app_name: str = "hospital-erp-core"
    env: str = "dev"  # dev | test | prod — 그 밖의 값은 기동 거부(_check_env)

    database_url: str = "postgresql+asyncpg://erp_app:erp_dev@localhost:5432/erp"
    redis_url: str = "redis://localhost:6379/0"

    # 인증 (design §6.1)
    jwt_secret: str = "dev-only-change-me-needs-32-bytes-minimum!"  # prod: 파일 주입 (§6.4)
    jwt_secret_file: str | None = None              # 권한 600 파일 — 설정 시 env보다 우선
    access_token_minutes: int = 30
    refresh_token_days: int = 7
    refresh_grace_seconds: int = 10                 # 동시 회전 오탐 방지 grace (§6.1)
    login_max_failures: int = 5
    login_lockout_minutes: int = 15

    # 컬럼 암호화 (design §6.4) — prod: 파일 주입
    data_enc_key: str = "0" * 64                    # hex(32B) AES-256 v1 — dev placeholder
    data_enc_key_file: str | None = None            # 권한파일 우선(env 평문 회피·P2-5). 설정 시 env보다 우선
    data_enc_key_v2: str | None = None              # hex(32B) v2 — 키 회전(P2-6) 시 신 버전 키
    data_enc_key_v2_file: str | None = None         # v2 키 파일 주입(P2-5)
    data_enc_key_version: int = 1                   # 신규 암호화에 쓸 현재 키 버전(회전 시 2로 승격)
    phone_bidx_key: str = "1" * 64                  # HMAC blind index 키 (DATA_ENC_KEY와 분리)
    phone_bidx_key_file: str | None = None          # bidx 키 파일 주입(P2-5)
    # 결재 불변 원장 MAC 서명 키(v1.143) — 무키 해시체인은 쓰기권한자 재작성 가능(적대검토 HIGH-1).
    # HMAC 키를 앱만 보유 → DB 직접쓰기(superuser)는 유효 MAC 생성 불가 → verify가 위조 탐지.
    # 미설정 시 jwt_secret에서 도메인 분리 파생(별도 주입 없이도 무키보다 강함·키격리는 전용키 권장).
    approval_ledger_key: str | None = None          # APPROVAL_LEDGER_KEY(전용 서명키)
    approval_ledger_key_file: str | None = None     # 파일 주입(P2-5)
    # ESS 급여조회 대상토큰(#4·적대검토) — 단일 정적키로 전 직원 열거 가능하던 것 축소. HIS가 조회
    # 대상 직원별 단명 서명 토큰(sub=employeeNumber) 발급·제시 → ERP가 조회 대상과 대조.
    ess_target_token_secret: str | None = None      # ESS_TARGET_TOKEN_SECRET(HIS 공유·JWT HS256)
    ess_target_token_secret_file: str | None = None
    ess_target_token_enforce: bool = False          # OFF=관측(있으면 검증·없으면 정적키)·ON=필수
    # S-3 대상 토큰(ERP→HIS 발신) — HIS PII EP(staff/schedule·patients/:mrn·approvers) 호출 시
    # X-Target-Token 동봉(erp.targetTokenSecret 공유·md5 앞12=638c276b3ee2). 호출부 생기면 필수.
    his_target_token_secret: str | None = None
    his_target_token_secret_file: str | None = None
    # 공유 PG(HIS·OpenPACS 동거) 슬롯 점유 경보 임계(%) — 이웃감시 잡·진단 EP 공유 (P2-4)
    pg_neighbor_warn_pct: int = 80

    web_origin: str = "http://localhost:3000"

    # 그룹웨어(Clinic) 근태 연동 (M0a=REST) — 키 미설정 시 수집 비활성 (대기 모드)
    clinic_api_base: str = "https://clinic.example.org"
    clinic_api_key: str | None = None
    hospital_code: str = "WRB-DEMO-M66M"
    # 공식 문서(증명서·계약서) 발급기관 표기명 — 미설정 시 "본 의료기관" 폴백(플레이스홀더 출력 방지).
    # 운영 활성화: .env ORG_NAME="<기관 정식명칭>"
    org_name: str = ""

    # HIS(Hospital RUN) 환자·수납 연동 (M0a=REST) — 키 미설정 시 수집 비활성
    his_api_base: str = "https://his.example.org"
    # 아웃바운드 허용 호스트 추가(CSV·integrations/egress.py) — base URL과 **따로** 적는다(INV-NET-01:
    # base 오설정만으로 페이로드가 새지 않게). 다른 기관 설치 시 base와 함께 설정.
    his_allowed_hosts: str = ""
    clinic_allowed_hosts: str = ""
    ai_allowed_hosts: str = ""
    his_api_key: str | None = None

    # HIS SSO — HIS가 발급한 JWT를 검증할 공유 서명키(HIS AUTH_SECRET). 미설정 시 SSO 비활성.
    his_jwt_secret: str | None = None
    his_jwt_secret_file: str | None = None         # 권한 600 파일 우선 (env 평문 회피)
    # SSO 허용 이메일 도메인(쉼표구분) — 설정 시 해당 도메인만 JIT 허용 (보안 C-1). 미설정=전체 허용.
    sso_allowed_email_domains: str = ""
    # HIS SSO 토큰 audience — 설정 시 aud 강제 검증(보안 C-2, 토큰 confusion 차단). HIS가 aud 발급 시 설정.
    his_jwt_audience: str | None = None

    # 리버스 프록시 신뢰 호스트(쉼표구분) — 리다이렉트/쿠키 베이스 구성 시 Host 화이트리스트(보안 H-1).
    # 미설정=현행(헤더 신뢰). 운영에선 반드시 설정(예: his.example.org).
    trusted_hosts: str = ""

    # 공공 수가 마스터 API (FR-34) — data.go.kr serviceKey 방식, 키 미설정 시 동기화 비활성
    fee_api_base: str = "https://apis.data.go.kr"
    fee_api_service_key: str | None = None

    # 이메일 알림 (FR-27) — SMTP 미설정 시 발송 비활성(no-op+로그). 격리망: 내부 릴레이 전제.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "erp-noreply@hospital.local"

    # 백업/DR (design §6.5) — backup_dir 미설정 시 백업 비활성. AES-256-GCM 암호화.
    backup_dir: str | None = None
    backup_enc_key: str | None = None      # hex(32B). 미설정 시 data_enc_key 재사용
    backup_keep_daily: int = 7             # 일 세대 보존(§6.5: 일7·월12·연5 — 1차는 일)
    # 오프사이트 백업(재해복구 P2-1) — 미설정 시 로컬만(현행). 설정 시 매 백업 후 암호화본을 원격 push.
    # 예: BACKUP_OFFSITE_DEST="backup@10.0.0.9:/data/erp-offsite/". SSH키는 컨테이너에 마운트(런북).
    backup_offsite_dest: str | None = None       # rsync dest(user@host:/path/)·미설정=로컬만
    backup_offsite_ssh_port: int = 22
    backup_offsite_ssh_key: str | None = None    # SSH 개인키 경로(마운트)·미지정 시 기본 키

    # 증빙 문서 첨부·보관 — attachment_dir 미설정 시 첨부 비활성(대기). sha256 키 저장(중복제거).
    attachment_dir: str | None = None
    attachment_max_bytes: int = 20 * 1024 * 1024   # 파일당 20MB 상한

    # PIN + WebAuthn(패스키) 2FA 로그인 — HIS SSO와 별개. RP는 ERP 도메인.
    webauthn_rp_id: str = "his.example.org"            # 패스키 RP ID(eTLD+1 또는 호스트)
    webauthn_rp_name: str = "병원 ERP"
    webauthn_origin: str = "https://his.example.org"   # 기대 origin(브라우저 검증)
    pin_2fa_enabled: bool = True                      # PIN+패스키 로그인 경로 활성

    # 전자결재(HIS W.Sign 릴레이) — HIS 경유. 미설정 시 상신 비활성.
    his_eapproval_base: str = "https://his.example.org"
    his_integration_key: str | None = None        # ERP→HIS 요청 X-Integration-Key
    # PII 조회 전용 키(S-1, HIS 계약 his-erp-integration-auth-hardening) — HIS PII EP
    # (billing/lines·hr/staff) 호출 시 사용. 미설정 시 webhook_key 폴백(무중단, HIS도 레거시 허용).
    his_pii_read_key: str | None = None
    eapproval_callback_secret: str | None = None   # HIS→ERP 콜백 X-Signature(HMAC) 검증
    eapproval_callback_max_skew: int = 300         # 콜백 재생 방지 — 서명된 ts 신선도 창(초)
    eapproval_submit_path: str = "/api/v1/erp/eapproval/submit"
    eapproval_status_path: str = "/api/v1/erp/eapproval"
    eapproval_callback_url: str = ""              # 미설정 시 web_origin 기반 자동
    # 상신은 HIS 엔드포인트 경로 확정 후 활성(콜백 수신은 시크릿만으로 즉시 동작)
    eapproval_submit_enabled: bool = False
    # 검진권 정산 지급 회신(ERP→HIS settlement.paid, DB-4) — 키·HIS EP 준비 후 활성
    settlement_callback_path: str = "/api/v1/voucher/settlements/erp-callback"
    dealer_settlement_callback_enabled: bool = False
    # S-2 재무 콜백 본문 서명(HIS 계약) — 설정 시 ERP→HIS 콜백에 X-Signature(HMAC rawBody)+
    # X-Timestamp 동봉. 미설정 시 미서명(permissive — HIS enforce 전 무영향).
    his_finance_webhook_secret: str | None = None
    # e-sign 트랙 A(외부 거래처 계약, ERP↔sign 직접). sign-core가 공유망 docker_default 합류 →
    # ERP가 서비스 DNS로 호출(shared PG/redis 방식 동일·공개 노출 0). 컨테이너 내부 포트 4000(호스트 4100 아님).
    sign_internal_base: str = "http://sign-core:4000/v1"   # sign 내부 API 베이스(docker_default DNS)
    sign_portal_base: str = "https://sign.example.org"      # 거래처 포털 deep-link 공개 도메인
    sign_api_key: str | None = None        # x-api-key(ERP_API_KEY) — 미설정 시 트랙A 비활성
    sign_webhook_secret: str | None = None  # ERP_WEBHOOK_SECRET — sign→ERP 웹훅 HMAC 검증
    # sign이 완료 통지를 보낼 ERP 콜백. sign-core는 별도 docker 브리지 컨테이너라 127.0.0.1
    # 루프백으론 호스트 ERP에 못 닿음(조인트 E2E에서 확인) → 기존 nginx 공개 split 재사용
    # (HMAC 보호·신규노출 없음). 0.0.0.0 바인드(공인 raw 포트 노출)보다 안전. prod는 .env 우선.
    sign_callback_url: str = "https://his.example.org/erp/api/v1/integrations/sign/webhook"

    # e-sign #6 ERP 의료계약 발의(origination, ERP→HIS) — 모델(a) ERP 주도(HIS PR#106 확정).
    # 전용 키(키 격리 — webhook 키 역방향 금지). 미설정 시 발의 비활성(수신·미러는 무관하게 동작).
    his_sign_origination_key: str | None = None
    his_sign_origination_path: str = "/api/v1/sign-integration/erp/request-sign"
    org_legal_name: str = "위루비병원"   # 계약 snapshot 병원 당사자명

    # HIS 아웃바운드 웹훅 수신(마스터플랜 Wave) — 미설정 시 게이트웨이 비활성.
    his_webhook_key: str | None = None            # HIS→ERP X-Integration-Key 검증
    # /api/metrics — 프록시(Cloudflare·Caddy)를 거친 요청은 이 토큰(Bearer)이 있어야 응답. 직접(내부망) 요청은 무인증.
    metrics_token: str | None = None
    # 인박스 본문 HMAC+timestamp(S-1/S-2 동형) — 정적 키 외 본문 무결성·재생방지.
    inbound_webhook_secret: str | None = None     # 본문서명 전용(정적키와 분리) INBOUND_WEBHOOK_SECRET
    inbound_webhook_hmac_enforce: bool = False    # OFF=병행수락(서명 있으면 관측로그)·ON=서명 필수
    inbound_webhook_max_skew: int = 300           # 본문서명 ts 신선도 창(초)

    # 셀프서비스 BI(Metabase) 자동 로그인 브리지 — ERP 인증자 → Metabase 공용계정 세션 발급·쿠키 주입.
    # OSS는 헤더 SSO 미지원 → 세션 브리지. 미설정 시 브리지 비활성(Metabase 자체 로그인 폴백).
    metabase_internal_base: str = "http://erp-metabase:3000"   # docker_default 내부 서비스 DNS
    metabase_sso_user: str | None = None          # 공용 계정 이메일(METABASE_SSO_USER)
    metabase_sso_password: str | None = None      # 공용 계정 비번(METABASE_SSO_PASSWORD, 안전채널)

    # example.org AI 사전심사 — 공시 RAG 검색(미설정 시 ERP 로컬 검색 fallback).
    weve_ai_api_base: str = "https://example.org/ollama"   # RAG 루트(/api/rag/* · /api/notice)
    weve_ai_api_key: str | None = None            # Bearer sk-* (AI 색인 발신과 공유)

    # 사전심사 자동화 P1 — 무적발(리스크0) 케이스 자동 이상없음(CLEARED). 킬스위치(가역·감사).
    prereview_auto_clear: bool = True
    # P2 — 확정 오탐 패턴(룰×코드, 인간 전건 오탐) 자동 DISMISS. 환수 리스크라 기본 OFF(옵트인).
    prereview_auto_dismiss: bool = False
    prereview_auto_dismiss_min_sample: int = 10   # 자동 오탐 인정 최소 인간결정 표본(전건 DISMISS)
    # FR-38 G1 운영 게이트 — 통보서(MEDRLT) 한정자 잠정값. 실물 2건 검증 통과 전엔
    # 권위 테이블(claim_batches/statements/adjustments) 반영 금지. 기본 차단(옵트인).
    medrlt_live_enabled: bool = False
    # 환자 본인부담 발생주의(accrual) 전환 — ON이면 진료시점 DR 환자미수금(1131)/CR 외래수익,
    # 수납시점 DR 현금·카드/CR 환자미수금. OFF(기본)=현행 현금주의(수납시점 수익). 회계사 사인오프 후 ON.
    patient_accrual_enabled: bool = False
    # 발생주의 활성화 기준월(YYYY-MM) — 이 월 이전 진료분은 accrue 제외(현금주의로 이미 인식됨).
    # cutover 시 open_cutover가 이전 미수납분을 1회 개통전기(DR1131/CR4110). 미설정 시 경계 없음(비권장).
    patient_accrual_cutover_month: str | None = None
    # 입원 중간정산 **수납**의 GL 전기(HIS billing.interim_paid 소비·0224).
    # 실행 코드: inbound_consumer._interim_paid — ON이면 `DR 1110 현금 / CR 1131 환자미수금` 1건만.
    # ⚠ **발생(수익) 인식 스위치가 아니다.** 발생은 patient 도메인의 별도 엔진
    #   (ReceivableService.accrue → DR1131/CR4110)이 담당하며 게이트도 patient_accrual_enabled로
    #   다르다. 이 둘을 혼동해 "이것만 켜면 입원수익이 인식된다"고 읽던 오해가 있었다(HIS B-1).
    #   이 게이트만 켜면 1131이 대변(선수금)으로만 쌓이고 상계할 차변이 오지 않는다.
    # OFF(기본)=원장 적재만. ON 선결=발생주의 전환 결정(결정 대기함 patient-accrual-cutover).
    his_interim_gl_enabled: bool = False
    # 급여 본인부담금 절사 단위(건보 시행령 §19 — 10원 미만 버림). 1이면 절사 없음(정액제 기관 등).
    patient_copay_round_unit: int = 10
    # Phase0 가명 토큰 키 (patientRef=HMAC-SHA256(chartNo, key) hex 앞16). HIS 발급·서버 파일 이동
    # → .env 0600 또는 PSEUDONYM_KEY_FILE. md5 지문 상호검증 후 원본 파일 삭제. 미설정 시 역해소 불가.
    pseudonym_key: str | None = None
    pseudonym_key_file: str | None = None
    # ERP측 컷오버 플립 — HIS pseudonymizeChartNo=true와 합의 일시 동시 전환. ON이면 inbound_consumer가
    # 수신 chartNo 필드를 patientRef로 간주·대사표 역해소(미등록=보류 큐). OFF(기본)=원 chartNo 그대로.
    his_pseudonym_enabled: bool = False
    # LIS 검사 청구 인바운드 HMAC 시크릿(LIS ERP_BILLING_SECRET와 대칭·HIS S0 절차 발급). 미설정 시
    # inbound_webhook_secret 폴백. 서명=HMAC-SHA256(<ts>.<body>)·X-Integration-Signature/Ts.
    lis_billing_secret: str | None = None

    @field_validator("env")
    @classmethod
    def _check_env(cls, v: str) -> str:
        """알 수 없는 ENV는 기동 거부.

        운영 판정이 `env == "prod"` 정확 일치라, `ENV=production`처럼 흔한 오기만으로 프로덕션
        fail-closed 가드(플레이스홀더 시크릿 차단)와 세션 쿠키 `Secure`가 **조용히 꺼졌다**.
        오기를 운영으로 추정하지도 않는다 — 대소문자·공백만 정규화하고 나머지는 멈춘다.
        """
        norm = (v or "").strip().lower()
        if norm not in _KNOWN_ENVS:
            raise ValueError(f"알 수 없는 ENV={v!r} — {'|'.join(_KNOWN_ENVS)} 중 하나로 설정하세요"
                             " (운영은 정확히 'prod'·오기 시 운영 안전가드가 꺼지므로 기동 거부)")
        return norm

    def load_secret_files(self) -> None:
        """기동 시 권한 파일에서 시크릿 로드 (env 평문·docker inspect 노출 회피 — design §6.4·P2-5).

        `*_FILE`가 설정되고 파일이 존재하면 env 값을 덮어쓴다(파일 우선). 미설정/부재 시 env 폴백
        (무회귀). ⚠ compose native secrets(/run/secrets)는 소스 파일 권한을 보존하므로, 비루트
        컨테이너(uid 10001)가 읽으려면 소스 파일이 0444여야 함(런북 secrets-provisioning 참조)."""
        def _load(file_attr: str, target_attr: str) -> None:
            path = getattr(self, file_attr, None)
            if path and Path(path).exists():
                setattr(self, target_attr, Path(path).read_text().strip())

        _load("jwt_secret_file", "jwt_secret")
        _load("his_jwt_secret_file", "his_jwt_secret")
        _load("data_enc_key_file", "data_enc_key")
        _load("data_enc_key_v2_file", "data_enc_key_v2")
        _load("phone_bidx_key_file", "phone_bidx_key")
        _load("approval_ledger_key_file", "approval_ledger_key")
        _load("ess_target_token_secret_file", "ess_target_token_secret")
        _load("his_target_token_secret_file", "his_target_token_secret")
        _load("pseudonym_key_file", "pseudonym_key")
        # 프로덕션 fail-closed 가드 — 플레이스홀더 시크릿으로 기동 금지(공개 상수로 ADMIN 토큰 위조·
        # 0-키 PII 평문 복호 방지·전체검토 발견). 파일/env 주입이 없으면 조용히 dev값으로 기동하던 갭.
        if self.env == "prod":
            bad = []
            if self.jwt_secret == "dev-only-change-me-needs-32-bytes-minimum!":
                bad.append("JWT_SECRET(_FILE)")
            if self.data_enc_key == "0" * 64:
                bad.append("DATA_ENC_KEY")
            # PHONE_BIDX_KEY 공백 시 blind index가 공개 상수("1"*64)로 계산돼, 공유 PG read 접근자가
            # 알려진 키로 전 환자 전화번호 bidx를 사전계산·열거 가능(AES 복호 없이·적대검토 MED-HIGH).
            # data_enc_key와 동일 등급으로 fail-closed.
            if self.phone_bidx_key == "1" * 64:
                bad.append("PHONE_BIDX_KEY")
            if bad:
                raise RuntimeError(
                    f"프로덕션 시크릿 미설정: {', '.join(bad)} — 기본 플레이스홀더로 기동 불가"
                    " (파일/env 주입 필요·design §6.4)")
            # 키 회전 정합 — 현재 버전 키가 실제로 설정돼 있어야 신규 암호화 가능(P2-6)
            if self.data_enc_key_version == 2 and not self.data_enc_key_v2:
                raise RuntimeError(
                    "DATA_ENC_KEY_VERSION=2 이나 DATA_ENC_KEY_V2 미설정 — 회전 키 주입 필요")
            if self.data_enc_key_version not in (1, 2):
                raise RuntimeError(f"미지원 DATA_ENC_KEY_VERSION: {self.data_enc_key_version}")


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.load_secret_files()
    return s
