import hashlib
from datetime import datetime, timedelta, timezone

from app import db as db_module
from tests.conftest import login_as_pub


def _invite_and_get_token(app, venue, name="New Staff", permission_level="staff"):
    with app.app_context():
        conn = db_module.get_db()
        person_cur = conn.execute("INSERT INTO person (name, email) VALUES (?, ?)", (name, "new@example.com"))
        person_id = person_cur.lastrowid
        membership_cur = conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')",
            (person_id, venue["id"]),
        )
        membership_id = membership_cur.lastrowid
        app_id = db_module.get_app_id(conn, "rotapulse")
        raw_token = "test-invite-token-1234567890"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        conn.execute(
            """INSERT INTO app_access (venue_membership_id, app_id, permission_level, status,
               invite_token_hash, invite_expires_at, invited_at)
               VALUES (?, ?, ?, 'invited', ?, ?, datetime('now'))""",
            (membership_id, app_id, permission_level, token_hash, expires_at),
        )
        conn.commit()
    return raw_token, membership_id


def test_accept_invite_completes_profile_and_sets_pending_approval(app, client, venue):
    raw_token, membership_id = _invite_and_get_token(app, venue)

    resp = client.post(
        f"/v/{venue['slug']}/onboard/{raw_token}",
        data={
            "password": "a-secure-password",
            "confirm_password": "a-secure-password",
            "home_address": "1 Test Street",
            "consent": "on",
            "available_mon": "on",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        access = conn.execute(
            "SELECT * FROM app_access WHERE venue_membership_id = ?", (membership_id,)
        ).fetchone()
        assert access["status"] == "pending_approval"
        detail = conn.execute(
            "SELECT * FROM rota_staff_detail WHERE venue_membership_id = ?", (membership_id,)
        ).fetchone()
        assert detail["home_address"] == "1 Test Street"


def test_expired_invite_is_rejected(app, client, venue):
    with app.app_context():
        conn = db_module.get_db()
        person_cur = conn.execute("INSERT INTO person (name) VALUES ('Expired')")
        person_id = person_cur.lastrowid
        membership_cur = conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')",
            (person_id, venue["id"]),
        )
        membership_id = membership_cur.lastrowid
        app_id = db_module.get_app_id(conn, "rotapulse")
        raw_token = "expired-token"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expired_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        conn.execute(
            """INSERT INTO app_access (venue_membership_id, app_id, permission_level, status,
               invite_token_hash, invite_expires_at) VALUES (?, ?, 'staff', 'invited', ?, ?)""",
            (membership_id, app_id, token_hash, expired_at),
        )
        conn.commit()

    resp = client.get(f"/v/{venue['slug']}/onboard/{raw_token}", follow_redirects=True)
    assert b"invalid or has expired" in resp.data


def test_admin_can_approve_pending_staff(app, client, venue):
    raw_token, membership_id = _invite_and_get_token(app, venue)
    client.post(
        f"/v/{venue['slug']}/onboard/{raw_token}",
        data={"password": "a-secure-password", "confirm_password": "a-secure-password"},
    )

    login_as_pub(client, venue["pub_id"])
    with app.app_context():
        conn = db_module.get_db()
        access = conn.execute(
            "SELECT id FROM app_access WHERE venue_membership_id = ?", (membership_id,)
        ).fetchone()
        access_id = access["id"]

    resp = client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/approve", follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        access = conn.execute("SELECT status FROM app_access WHERE id = ?", (access_id,)).fetchone()
        assert access["status"] == "active"
