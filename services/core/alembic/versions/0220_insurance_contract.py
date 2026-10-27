"""0220 — 보험계약 전용화(내부운영 갭 감사 Tier2·소규모): contract_type INSURANCE + 증권 필드.

audit 발견: 배상책임/화재/의료기기 보험이 OutsourcingContract OTHER로 흡수·증권번호/보장한도/납입주기
전용 필드 없음 → 보험 만기관리 얕음. INSURANCE 타입·카테고리 + policy_no/coverage_limit/premium_cycle.

Revision ID: 0220
"""
import sqlalchemy as sa
from alembic import op

revision = "0220"
down_revision = "0219"
branch_labels = None
depends_on = None

_CAT_OLD = "'MEAL','LAUNDRY','WASTE','SECURITY','CLEANING','FACILITY','IT','OTHER'"
_CAT_NEW = "'MEAL','LAUNDRY','WASTE','SECURITY','CLEANING','FACILITY','IT','INSURANCE','OTHER'"
_TYPE_OLD = "'OUTSOURCING','PURCHASE','SERVICE','LEASE','OTHER'"
_TYPE_NEW = "'OUTSOURCING','PURCHASE','SERVICE','LEASE','INSURANCE','OTHER'"


def upgrade() -> None:
    # ck_oc_type: 0142가 명명. ck_oc_category: 0044 인라인 무명(PG 자동명) — 양쪽 IF EXISTS 드롭.
    op.execute("ALTER TABLE scm.outsourcing_contracts DROP CONSTRAINT IF EXISTS ck_oc_type")
    op.create_check_constraint("ck_oc_type", "outsourcing_contracts",
                               f"contract_type IN ({_TYPE_NEW})", schema="scm")
    op.execute("ALTER TABLE scm.outsourcing_contracts "
               "DROP CONSTRAINT IF EXISTS outsourcing_contracts_category_check")
    op.execute("ALTER TABLE scm.outsourcing_contracts DROP CONSTRAINT IF EXISTS ck_oc_category")
    op.create_check_constraint("ck_oc_category", "outsourcing_contracts",
                               f"category IN ({_CAT_NEW})", schema="scm")
    op.add_column("outsourcing_contracts", sa.Column("policy_no", sa.String(60), nullable=True),
                  schema="scm")
    op.add_column("outsourcing_contracts",
                  sa.Column("coverage_limit", sa.Numeric(16, 0), nullable=True), schema="scm")
    op.add_column("outsourcing_contracts",
                  sa.Column("premium_cycle", sa.String(10), nullable=True), schema="scm")


def downgrade() -> None:
    op.execute("UPDATE scm.outsourcing_contracts SET contract_type='OTHER' "
               "WHERE contract_type='INSURANCE'")
    op.execute("UPDATE scm.outsourcing_contracts SET category='OTHER' WHERE category='INSURANCE'")
    op.drop_column("outsourcing_contracts", "premium_cycle", schema="scm")
    op.drop_column("outsourcing_contracts", "coverage_limit", schema="scm")
    op.drop_column("outsourcing_contracts", "policy_no", schema="scm")
    op.execute("ALTER TABLE scm.outsourcing_contracts DROP CONSTRAINT IF EXISTS ck_oc_type")
    op.create_check_constraint("ck_oc_type", "outsourcing_contracts",
                               f"contract_type IN ({_TYPE_OLD})", schema="scm")
    op.execute("ALTER TABLE scm.outsourcing_contracts DROP CONSTRAINT IF EXISTS ck_oc_category")
    op.create_check_constraint("ck_oc_category", "outsourcing_contracts",
                               f"category IN ({_CAT_OLD})", schema="scm")
