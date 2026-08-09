from app import db as db_module
from tests.conftest import create_active_staff, login_as_pub


def _create_invite(client, venue, **overrides):
    data = {
        "name": "New Starter",
        "email": "starter@example.com",
        "invite_method": "email",
        "permission_level": "staff",
    }
    data.update(overrides)
    return client.post(f"/v/{venue['slug']}/admin/staff/create", data=data)


def _get_access_id(app, venue, name="New Starter"):
    with app.app_context():
        conn = db_module.get_db()
        return conn.execute(
            """SELECT app_access.id FROM app_access
               JOIN venue_membership ON venue_membership.id = app_access.venue_membership_id
               JOIN person ON person.id = venue_membership.person_id
               WHERE person.name = ? AND venue_membership.venue_id = ?""",
            (name, venue["id"]),
        ).fetchone()["id"]


def test_resend_invite_generates_a_fresh_token(app, client, venue, monkeypatch):
    sent = []
    monkeypatch.setattr("app.admin_config.send_email", lambda to, subject, body: sent.append(body))

    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue)
    access_id = _get_access_id(app, venue)

    with app.app_context():
        conn = db_module.get_db()
        original_hash = conn.execute(
            "SELECT invite_token_hash FROM app_access WHERE id = ?", (access_id,)
        ).fetchone()["invite_token_hash"]

    resp = client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/resend-invite", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Invite resent to New Starter." in resp.data
    assert len(sent) == 2  # one from create, one from resend

    with app.app_context():
        conn = db_module.get_db()
        new_hash = conn.execute(
            "SELECT invite_token_hash FROM app_access WHERE id = ?", (access_id,)
        ).fetchone()["invite_token_hash"]
    assert new_hash != original_hash


def test_resend_invite_uses_original_invite_method(app, client, venue, monkeypatch):
    sms_sent = []
    monkeypatch.setattr("app.admin_config.send_sms", lambda to, body: sms_sent.append((to, body)))
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: None)

    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue, mobile="07796123456", invite_method="sms", email="")
    access_id = _get_access_id(app, venue)

    client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/resend-invite")
    assert len(sms_sent) == 2  # create + resend, both via SMS
    assert sms_sent[1][0] == "07796123456"


def test_resend_invite_404s_once_already_accepted(app, client, venue, monkeypatch):
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: None)
    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue)
    access_id = _get_access_id(app, venue)

    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE app_access SET status = 'pending_approval' WHERE id = ?", (access_id,))
        conn.commit()

    resp = client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/resend-invite")
    assert resp.status_code == 404


def test_resend_invite_requires_admin_permission(app, client, venue, monkeypatch):
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: None)
    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue)
    access_id = _get_access_id(app, venue)

    with client.session_transaction() as sess:
        sess.clear()

    resp = client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/resend-invite")
    assert resp.status_code in (302, 401, 403)


def test_staff_list_only_shows_resend_for_invited_status(app, client, venue, monkeypatch):
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: None)
    create_active_staff(app, venue["id"], name="Already Active")
    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue)

    resp = client.get(f"/v/{venue['slug']}/admin/staff")
    assert resp.data.count(b"Resend invite") == 1
