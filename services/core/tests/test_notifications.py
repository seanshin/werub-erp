"""알림센터 (FR-26) — 미읽음 카운트."""


async def test_unread_count(session):
    from erp.common.models import Notification, User
    from erp.common.router import CurrentUser, unread_notification_count
    u = User(email="n@x.example", password_hash="x", role="STAFF")
    session.add(u)
    await session.flush()
    session.add_all([
        Notification(user_id=u.id, type="STOCK", title="a"),
        Notification(user_id=u.id, type="STOCK", title="b"),
    ])
    await session.flush()
    cur = CurrentUser(user_id=u.id, role="STAFF", dept_id=None, license_flag=True)
    out = await unread_notification_count(user=cur, session=session)
    assert out["unread"] == 2
