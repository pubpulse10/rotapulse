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

    def _fake_send_email(to, subject, body):
        sent.append(body)
        return True

    monkeypatch.setattr("app.admin_config.send_email", _fake_send_email)

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

    def _fake_send_sms(to, body):
        sms_sent.append((to, body))
        return True

    monkeypatch.setattr("app.admin_config.send_sms", _fake_send_sms)
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: True)

    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue, mobile="07796123456", invite_method="sms", email="")
    access_id = _get_access_id(app, venue)

    client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/resend-invite")
    assert len(sms_sent) == 2  # create + resend, both via SMS
    assert sms_sent[1][0] == "07796123456"


def test_resend_invite_404s_once_already_accepted(app, client, venue, monkeypatch):
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: True)
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
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: True)
    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue)
    access_id = _get_access_id(app, venue)

    with client.session_transaction() as sess:
        sess.clear()

    resp = client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/resend-invite")
    assert resp.status_code in (302, 401, 403)


def test_staff_list_only_shows_resend_for_invited_status(app, client, venue, monkeypatch):
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: True)
    create_active_staff(app, venue["id"], name="Already Active")
    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue)

    resp = client.get(f"/v/{venue['slug']}/admin/staff")
    assert resp.data.count(b"Resend invite") == 1


def test_failed_delivery_is_recorded_and_flagged_on_staff_list(app, client, venue, monkeypatch):
    """The actual bug that prompted this feature: a delivery failure (e.g.
    the SMS E.164 issue) used to be invisible anywhere except server logs
    the admin can't see — this is the regression test for making it visible
    on the page instead."""
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: False)

    login_as_pub(client, venue["pub_id"])
    resp = _create_invite(client, venue)
    assert resp.status_code == 302
    access_id = _get_access_id(app, venue)

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT invite_delivery_status FROM app_access WHERE id = ?", (access_id,)
        ).fetchone()
    assert row["invite_delivery_status"] == "failed"

    list_resp = client.get(f"/v/{venue['slug']}/admin/staff")
    assert b"Delivery failed" in list_resp.data


def test_successful_delivery_shows_no_failed_flag(app, client, venue, monkeypatch):
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: True)

    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue)
    access_id = _get_access_id(app, venue)

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT invite_delivery_status FROM app_access WHERE id = ?", (access_id,)
        ).fetchone()
    assert row["invite_delivery_status"] == "sent"

    list_resp = client.get(f"/v/{venue['slug']}/admin/staff")
    assert b"Delivery failed" not in list_resp.data


def test_create_staff_flashes_failure_message_when_delivery_fails(app, client, venue, monkeypatch):
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: False)
    login_as_pub(client, venue["pub_id"])

    resp = client.post(
        f"/v/{venue['slug']}/admin/staff/create",
        data={"name": "New Starter", "email": "starter@example.com", "invite_method": "email", "permission_level": "staff"},
        follow_redirects=True,
    )
    assert b"couldn&#39;t be delivered" in resp.data or b"couldn't be delivered" in resp.data


def test_resend_clears_previous_failed_status_before_retrying(app, client, venue, monkeypatch):
    """A resend must not carry forward the old 'failed' flag if this attempt
    succeeds — confirmed by flipping the mock mid-test rather than assuming."""
    calls = {"n": 0}

    def flaky_send(*a, **k):
        calls["n"] += 1
        return calls["n"] > 1  # fails first time (create), succeeds after (resend)

    monkeypatch.setattr("app.admin_config.send_email", flaky_send)

    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue)
    access_id = _get_access_id(app, venue)

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute(
            "SELECT invite_delivery_status FROM app_access WHERE id = ?", (access_id,)
        ).fetchone()["invite_delivery_status"] == "failed"

    client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/resend-invite")

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute(
            "SELECT invite_delivery_status FROM app_access WHERE id = ?", (access_id,)
        ).fetchone()["invite_delivery_status"] == "sent"
