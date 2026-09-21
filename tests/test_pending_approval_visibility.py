"""
Telling people about the approval step (real report, 2026-09-21, The Queens
Head: "the account seems to get created but then cannot log in").

Staff finish their invite, land in 'pending_approval', and until an admin
approves them the right password used to bounce them back to the login form
with nothing said at all — identical to a wrong password. Meanwhile nothing
told the admin anyone was waiting.
"""

from app import db as db_module
from app.notifications import send_email as real_send_email
from tests.conftest import TEST_STAFF_PASSWORD, create_active_staff, login_as_pub


def _set_access_status(app, person_id, status):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            """UPDATE app_access SET status = ?
               WHERE venue_membership_id = (SELECT id FROM venue_membership WHERE person_id = ?)""",
            (status, person_id),
        )
        conn.commit()


def _log_in(client, venue, email, password=TEST_STAFF_PASSWORD):
    return client.post(
        f"/v/{venue['slug']}/login",
        data={"identifier": email, "password": password},
        follow_redirects=True,
    )


# --- what the staff member is told -----------------------------------------

def test_waiting_for_approval_is_explained_not_silently_bounced(app, client, venue):
    person_id, _m, email = create_active_staff(app, venue["id"], name="Waiting Wendy")
    _set_access_status(app, person_id, "pending_approval")

    resp = _log_in(client, venue, email)
    assert resp.status_code == 200
    assert b"needs approving" in resp.data
    assert b"View pending approvals" in resp.data
    # The old symptom: the generic wrong-password message, or nothing at all.
    assert b"Invalid email/mobile or password" not in resp.data
    with client.session_transaction() as session:
        assert "rotapulse_person_id" not in session


def test_an_approved_staff_member_still_logs_in(app, client, venue):
    _person_id, _m, email = create_active_staff(app, venue["id"], name="Active Alice")

    resp = _log_in(client, venue, email)
    assert resp.status_code == 200
    assert b"needs approving" not in resp.data
    with client.session_transaction() as session:
        assert "rotapulse_person_id" in session


def test_revoked_access_gets_its_own_message_not_the_approval_one(app, client, venue):
    person_id, _m, email = create_active_staff(app, venue["id"], name="Gone Gary")
    _set_access_status(app, person_id, "revoked")

    resp = _log_in(client, venue, email)
    # Apostrophes render escaped, so match on the plain part of the sentence.
    assert b"speak to your manager" in resp.data
    assert b"needs approving" not in resp.data
    with client.session_transaction() as session:
        assert "rotapulse_person_id" not in session


def test_a_wrong_password_still_says_nothing_useful(app, client, venue):
    _person_id, _m, email = create_active_staff(app, venue["id"], name="Typo Tom")

    resp = _log_in(client, venue, email, password="not the password")
    assert b"Invalid email/mobile or password" in resp.data
    # Must not leak account state to someone who can't prove who they are.
    assert b"needs approving" not in resp.data


# --- what the admin is told -------------------------------------------------

def test_finishing_an_invite_emails_the_venues_admins(app, client, venue, monkeypatch):
    sent = []
    monkeypatch.setattr("app.onboarding.send_email", lambda to, subject, body: sent.append((to, subject, body)) or True)

    owner_id, _m, owner_email = create_active_staff(
        app, venue["id"], name="Adam Owner", permission_level="rota_admin", email="adam@example.com"
    )
    invitee_id, membership_id, _email = create_active_staff(app, venue["id"], name="New Nina")
    # Put the invitee back to a fresh, unaccepted invite.
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            """UPDATE app_access SET status = 'invited', invite_token_hash = ?,
               invite_expires_at = '2099-01-01T00:00:00+00:00'
               WHERE venue_membership_id = ?""",
            ("d0d4d0dcb2e4c94c0c2d9e0f1e9a3e6b1f2c3d4e5a6b7c8d9e0f1a2b3c4d5e6f", membership_id),
        )
        conn.execute("UPDATE person SET password_hash = NULL WHERE id = ?", (invitee_id,))
        conn.commit()

    import hashlib

    raw = "letmein-token"
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE app_access SET invite_token_hash = ? WHERE venue_membership_id = ?",
            (hashlib.sha256(raw.encode()).hexdigest(), membership_id),
        )
        conn.commit()

    resp = client.post(
        f"/v/{venue['slug']}/onboard/{raw}",
        data={"password": "a-long-password", "confirm_password": "a-long-password"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    assert [s for s in sent if s[0] == owner_email], "the venue's admin was never told"
    subject = [s for s in sent if s[0] == owner_email][0][1]
    body = [s for s in sent if s[0] == owner_email][0][2]
    assert "New Nina" in subject
    assert "/admin/staff/pending" in body


def test_the_staff_nav_shows_how_many_are_waiting(app, client, venue):
    person_id, _m, _email = create_active_staff(app, venue["id"], name="Waiting Wendy")
    _set_access_status(app, person_id, "pending_approval")
    login_as_pub(client, venue["pub_id"])

    body = client.get(f"/v/{venue['slug']}/rota/").data
    assert b"waiting to be approved" in body

    staff_page = client.get(f"/v/{venue['slug']}/admin/staff").data
    assert b"waiting" in staff_page
    assert b"can't log in until you approve them" in staff_page


def test_no_badge_when_nobody_is_waiting(app, client, venue):
    create_active_staff(app, venue["id"], name="Active Alice")
    login_as_pub(client, venue["pub_id"])

    body = client.get(f"/v/{venue['slug']}/rota/").data
    assert b"waiting to be approved" not in body


def test_send_email_signature_matches_what_onboarding_calls(app):
    # Tripwire: the admin alert is sent directly, not through
    # notification_settings.notify_admins (which is silent until a venue
    # configures it). If send_email's shape changes, fail here rather than
    # silently stopping the one message that makes approval discoverable.
    import inspect

    assert list(inspect.signature(real_send_email).parameters)[:3] == ["to", "subject", "body"]
