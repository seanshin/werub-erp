"""0038 — 보험심사 워크리스트(PR-A): 케이스·적발·코멘트 + Tier1 룰 5종.

사전심사를 무상태 실행기에서 심사팀 워크플로로: 차트(chart_no×진료월) 케이스 영속,
적발 항목별 결정(인정/오탐), 검토 코멘트. 룰타입 CHECK를 Tier1 5종으로 확장하고
시드(코드 지정이 필요한 AGE_SEX_LIMIT·EXCLUSIVE_PAIR는 비활성 템플릿).

Revision ID: 0038
"""
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

_NEW_TYPES = ("'DUPLICATE','COUNT_LIMIT','UNKNOWN_CODE','AMOUNT_OUTLIER',"
              "'AGE_SEX_LIMIT','EXCLUSIVE_PAIR','PERIOD_LIMIT','SURCHARGE_CHECK','LOS_RATIO'")


def upgrade() -> None:
    # 0013은 인라인 CHECK(자동명 pre_review_rules_rule_type_check) — 양쪽 이름 모두 제거
    op.execute("ALTER TABLE claims.pre_review_rules "
               "DROP CONSTRAINT IF EXISTS ck_prerule_type")
    op.execute("ALTER TABLE claims.pre_review_rules "
               "DROP CONSTRAINT IF EXISTS pre_review_rules_rule_type_check")
    op.execute("ALTER TABLE claims.pre_review_rules ADD CONSTRAINT ck_prerule_type "
               f"CHECK (rule_type IN ({_NEW_TYPES}))")

    op.execute("""
        CREATE TABLE claims.pre_review_cases (
            id BIGSERIAL PRIMARY KEY,
            chart_no VARCHAR(20) NOT NULL,
            care_month VARCHAR(7) NOT NULL,
            dept_code VARCHAR(10),
            status VARCHAR(14) NOT NULL DEFAULT 'OPEN' CHECK (status IN
                ('OPEN','IN_REVIEW','CLEARED','FIX_REQUESTED','BLOCKED','RESOLVED')),
            max_severity VARCHAR(8),
            risk_amount NUMERIC(14,0) NOT NULL DEFAULT 0,
            claimed_amt NUMERIC(14,0) NOT NULL DEFAULT 0,
            finding_count INTEGER NOT NULL DEFAULT 0,
            assignee_id BIGINT,
            last_run_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_prereview_case UNIQUE (chart_no, care_month)
        )
    """)
    op.execute("CREATE INDEX ix_prc_status ON claims.pre_review_cases (status)")
    op.execute("""
        CREATE TABLE claims.pre_review_findings (
            id BIGSERIAL PRIMARY KEY,
            case_id BIGINT NOT NULL REFERENCES claims.pre_review_cases(id),
            rule_code VARCHAR(30) NOT NULL,
            rule_name VARCHAR(120),
            severity VARCHAR(8) NOT NULL,
            code VARCHAR(20),
            detail TEXT,
            amount NUMERIC(14,0) NOT NULL DEFAULT 0,
            decision VARCHAR(8) NOT NULL DEFAULT 'PENDING'
                CHECK (decision IN ('PENDING','ACCEPT','DISMISS')),
            decision_note TEXT,
            decided_by BIGINT,
            decided_at TIMESTAMPTZ,
            resolved BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_prf_case ON claims.pre_review_findings (case_id)")
    op.execute("""
        CREATE TABLE claims.pre_review_comments (
            id BIGSERIAL PRIMARY KEY,
            case_id BIGINT NOT NULL REFERENCES claims.pre_review_cases(id),
            author BIGINT NOT NULL,
            body TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_prcm_case ON claims.pre_review_comments (case_id)")

    # Tier1 룰 시드 — 코드 지정 필요한 2종은 비활성 템플릿(심사팀이 params 채워 활성화)
    op.execute("""
        INSERT INTO claims.pre_review_rules (code, name, rule_type, params, severity, active)
        VALUES
        ('PR-T1-PERIOD', '월 단위 산정횟수 상한', 'PERIOD_LIMIT',
         '{"max": 4, "window": "M"}', 'WARN', true),
        ('PR-T1-SURCHARGE', '야간·공휴 가산 적정성', 'SURCHARGE_CHECK',
         '{"surcharge_suffix": "010", "night_start": 20, "night_end": 8}', 'WARN', true),
        ('PR-T1-LOS', '일별 행위량 이상(내원일수 대비)', 'LOS_RATIO',
         '{"per_day_max": 30}', 'WARN', true),
        ('PR-T1-AGESEX', '연령·성별 적용 제한(템플릿)', 'AGE_SEX_LIMIT',
         '{"code": "", "min_age": null, "max_age": null, "sex": null}', 'BLOCK', false),
        ('PR-T1-EXCL', '동시산정 불가 조합(템플릿)', 'EXCLUSIVE_PAIR',
         '{"codes": []}', 'BLOCK', false)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE claims.pre_review_comments")
    op.execute("DROP TABLE claims.pre_review_findings")
    op.execute("DROP TABLE claims.pre_review_cases")
    op.execute("DELETE FROM claims.pre_review_rules WHERE code LIKE 'PR-T1-%'")
    op.execute("ALTER TABLE claims.pre_review_rules DROP CONSTRAINT IF EXISTS ck_prerule_type")
    op.execute("ALTER TABLE claims.pre_review_rules ADD CONSTRAINT ck_prerule_type CHECK "
               "(rule_type IN ('DUPLICATE','COUNT_LIMIT','UNKNOWN_CODE','AMOUNT_OUTLIER'))")
