"""검진권 전기 규칙 관리자 CRUD — 수수료 방향·정산 계정 설정(다양한 처리 지원)."""
import pytest

from erp.common.router import CurrentUser
from erp.errors import ForbiddenError, ValidationError
from erp.finance.router import (
    PostingRuleBody, create_posting_rule, list_posting_rules, update_posting_rule,
)

ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
STAFF = CurrentUser(user_id=2, role="STAFF", dept_id=1, license_flag=True)


async def test_create_and_list_rule(session):
    out = await create_posting_rule(PostingRuleBody(
        line_type="commission", debit_account="5520", credit_account="2110",
        amount_source="commissionAmount", reverse_on_negative=True,
        description="수수료 적립"), user=ADMIN, session=session)
    assert out["line_type"] == "commission" and out["debit_account"] == "5520"
    listed = await list_posting_rules(user=ADMIN, session=session)
    assert any(r["id"] == out["id"] for r in listed["items"])


async def test_update_rule_changes_accounts(session):
    created = await create_posting_rule(PostingRuleBody(
        line_type="settlement", debit_account="2110", credit_account="1110",
        amount_source="netPayableKrw"), user=ADMIN, session=session)
    upd = await update_posting_rule(created["id"], PostingRuleBody(
        line_type="settlement", debit_account="1110", credit_account="1130",
        amount_source="netPayableKrw", description="AR 전환"), user=ADMIN, session=session)
    assert upd["debit_account"] == "1110" and upd["credit_account"] == "1130"


async def test_invalid_line_type_rejected(session):
    with pytest.raises(ValidationError):
        await create_posting_rule(PostingRuleBody(line_type="bogus"),
                                  user=ADMIN, session=session)


async def test_non_admin_cannot_write(session):
    with pytest.raises(ForbiddenError):
        await create_posting_rule(PostingRuleBody(line_type="commission"),
                                  user=STAFF, session=session)
