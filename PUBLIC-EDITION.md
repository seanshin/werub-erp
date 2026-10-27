# ERP — 공개판 안내

**ERP — about this public edition**

> **EN** — This repository is the public edition of ERP v1.306.1. Development continues in a private repository; this edition is its final state with internal working records (reports and replies exchanged with other systems, key-rotation runbooks, server measurements) and operational details removed, licensed under MIT. Operational domains, server addresses and accounts were replaced with example values, and **every password, secret, token and key was emptied to `CHANGE_ME`** — you must set your own before running anything. The table below lists each emptied setting and where it appears.

이 저장소는 **ERP v1.306.1 의 공개판**입니다. 개발은 비공개 저장소에서 계속하고, 공개판은 그 최종 상태에서 다음을 빼거나 바꾼 것입니다(MIT).

| 무엇 | 어떻게 |
|---|---|
| 내부 작업 기록 | 다른 시스템과 주고받은 결함 보고 · 회신, 키 교체 런북, 서버 실측 등 189개 문서를 뺐습니다. 소스 주석이 빠진 문서를 가리키는 곳이 남아 있을 수 있습니다 |
| 운영 정보 | 운영 도메인 · 서버 주소 · 서버 계정을 예시 값(`example.org` · 문서용 IP 대역 · `deploy`)으로 바꿨습니다 |
| 🔴 비밀값 | 비밀번호 · 비밀 · 토큰 · 키 **8곳**을 `CHANGE_ME` 로 비웠습니다. **실행 전에 아래 값을 직접 정하십시오** |

## 직접 설정할 값

| 설정 이름 | 있는 곳 |
|---|---|
| `CLINIC_API_KEY` | `infra/.env.example`(비어 있음) |
| `EAPPROVAL_CALLBACK_SECRET` | `infra/.env.example`(비어 있음) |
| `ERP_BI_PASSWORD` | `infra/.env.bi.example`(비어 있음) |
| `FEE_API_SERVICE_KEY` | `infra/.env.example`(비어 있음) |
| `GF_ADMIN_PASSWORD` | `infra/.env.bi.example`(비어 있음) |
| `HIS_API_KEY` | `infra/.env.example`(비어 있음) |
| `HIS_FINANCE_WEBHOOK_SECRET` | `infra/.env.example`(비어 있음) |
| `HIS_INTEGRATION_KEY` | `infra/.env.example`(비어 있음) |
| `HIS_JWT_SECRET` | `infra/.env.example`(비어 있음) |
| `HIS_WEBHOOK_KEY` | `infra/.env.example`(비어 있음) |
| `INBOUND_WEBHOOK_SECRET` | `infra/.env.example`(비어 있음) |
| `METABASE_SSO_PASSWORD` | `infra/.env.example`(비어 있음) |
| `SIGN_API_KEY` | `infra/.env.example`(비어 있음) |
| `SIGN_WEBHOOK_SECRET` | `infra/.env.example`(비어 있음) |
| `WEVE_AI_API_KEY` | `infra/.env.example`(비어 있음) |
| 문서 · 예시 속 비밀번호 | `.github/workflows/ci.yml` · `README.md` · `infra/docker-compose.yml` — 접속 문자열 · 예시 명령의 비밀번호를 `CHANGE_ME` 로 비웠습니다. 실제 값으로 바꿔 쓰십시오 |

- 같은 이름이 여러 곳에 있으면 **모두 같은 값**이어야 하는지(서로 맞물리는 연동 비밀) 확인하십시오.
- 값은 길고 무작위로 만드십시오(예: `openssl rand -base64 32`). 저장소에 다시 커밋하지 마십시오.

## 켜기 전에 기관이 정할 보안 설정

아래 기능은 공개판에서 **설정하지 않으면 안전한 쪽으로 멈추거나 꺼져 있습니다.** 기관의 운영 절차에 맞춰 직접 정하십시오.

| 설정 | 무엇을 정하나 |
|---|---|
| `HIS SSO 정책(관리 화면 · HIS_JWT_AUDIENCE)` | 공개판은 **정하지 않으면 안전한 쪽**으로 동작합니다 — URL · 본문으로 HIS 메인 토큰을 넘기는 방식은 막고, `erp-sso` aud 와 1회용 jti 를 요구합니다. 옛 HIS 와 붙여 URL 토큰 방식이 꼭 필요할 때만 관리 화면(연동 설정)에서 명시적으로 허용하십시오. |
| `HIS_JWT_SECRET · HIS_JWT_SECRET_FILE` | HIS 의 서명키(AUTH_SECRET)와 같은 값입니다. 이 값을 가진 쪽은 HIS 직원 토큰을 만들 수 있으므로 파일(권한 600)로 두고 접근을 좁히십시오. 비어 있으면 SSO 가 꺼집니다. |
| `SSO_ALLOWED_EMAIL_DOMAINS` | 비어 있으면 HIS 토큰의 모든 이메일 도메인을 계정으로 만듭니다. 기관 도메인만 적으십시오. |
| `TRUSTED_HOSTS` | 비어 있으면 리버스 프록시가 보낸 Host 헤더를 그대로 믿습니다. 운영에서는 ERP 를 여는 호스트 이름을 적으십시오. |
| `WEBAUTHN_RP_ID · WEBAUTHN_ORIGIN` | 패스키 로그인의 기준 도메인입니다. 공개판 기본값은 예시 도메인이라 그대로는 패스키가 성립하지 않습니다. ERP 를 여는 도메인으로 정하십시오. |
| `HIS_ALLOWED_HOSTS · CLINIC_ALLOWED_HOSTS · AI_ALLOWED_HOSTS` | 바깥으로 부를 수 있는 호스트 목록(쉼표 구분)입니다. 주소(`*_API_BASE`)와 **따로** 적어야 호출이 나갑니다 — 주소만 잘못 바뀌어도 급여 · 청구 자료가 다른 곳으로 가지 않게 하려는 장치입니다. |
| `LIS_BILLING_SECRET · EAPPROVAL_CALLBACK_SECRET` | LIS 검사 청구 · HIS 결재 결과 콜백의 서명 검증 비밀입니다. 보내는 쪽과 같은 값이어야 합니다. 비어 있으면 해당 수신을 거부합니다(LIS 청구는 `LIS_BILLING_SECRET` 이 비면 `INBOUND_WEBHOOK_SECRET` 으로 검증하므로, 두 연동에 같은 비밀을 쓰지 않으려면 따로 정하십시오). |

- 코드 안의 개발용 기본값은 운영에서 반드시 환경 변수로 덮어쓰십시오.
- 이 생태계 전체의 소개 · 구축 순서: [werub-his-ecosystem](https://github.com/seanshin/werub-his-ecosystem)
