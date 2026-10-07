"""
Invites and open-shift alerts are our email and our texts, sent to recipients
a venue chose. They had no limit at all (found 2026-10-07), so a trial account
could send without end from our sender and our SMS number.
"""

import pytest

from tests.conftest import TEST_PUB_ID, login_as_pub


@pytest.fixture
def limited():
    from app.extensions import limiter

    limiter.enabled = True
    limiter.reset()
    yield
    limiter.reset()
    limiter.enabled = False


def test_staff_invites_are_limited_per_venue(app, client, venue, limited, monkeypatch):
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: None, raising=False)
    login_as_pub(client, TEST_PUB_ID)

    codes = [
        client.post(f"/v/{venue['slug']}/admin/staff/create", data={
            "name": f"Person {n}", "email": f"p{n}@example.com", "role_id": venue["role_id"],
            "invite_method": "email", "permission_level": "staff"}).status_code
        for n in range(32)
    ]

    assert 429 in codes and codes.index(429) == 30
