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


def test_resend_invite_can_switch_from_email_to_sms(app, client, venue, monkeypatch):
    """A person invited by email who never got it should be resendable by
    SMS instead, not stuck permanently on their original choice."""
    emails_sent, sms_sent = [], []
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: emails_sent.append(a) or True)
    monkeypatch.setattr("app.admin_config.send_sms", lambda *a, **k: sms_sent.append(a) or True)

    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue, mobile="07796123456")  # invite_method email, has both contacts
    access_id = _get_access_id(app, venue)
    assert len(emails_sent) == 1

    resp = client.post(
        f"/v/{venue['slug']}/admin/staff/{access_id}/resend-invite",
        data={"invite_method": "sms"}, follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Invite resent to New Starter." in resp.data
    assert len(sms_sent) == 1
    assert len(emails_sent) == 1  # not re-sent by email too

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute(
            "SELECT invite_method FROM app_access WHERE id = ?", (access_id,)
        ).fetchone()["invite_method"] == "sms"


def test_resend_invite_switch_to_sms_requires_a_mobile_on_file(app, client, venue, monkeypatch):
    sms_sent = []
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: True)
    monkeypatch.setattr("app.admin_config.send_sms", lambda *a, **k: sms_sent.append(a) or True)

    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue)  # no mobile on file
    access_id = _get_access_id(app, venue)

    with app.app_context():
        conn = db_module.get_db()
        original_hash = conn.execute(
            "SELECT invite_token_hash FROM app_access WHERE id = ?", (access_id,)
        ).fetchone()["invite_token_hash"]

    resp = client.post(
        f"/v/{venue['slug']}/admin/staff/{access_id}/resend-invite",
        data={"invite_method": "sms"}, follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"no mobile number on file" in resp.data
    assert sms_sent == []

    with app.app_context():
        conn = db_module.get_db()
        # Rejected before rotating the token, so the still-valid existing link isn't burned.
        assert conn.execute(
            "SELECT invite_token_hash FROM app_access WHERE id = ?", (access_id,)
        ).fetchone()["invite_token_hash"] == original_hash


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


def _put_into_pending_approval(app, access_id):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE app_access SET status = 'pending_approval' WHERE id = ?", (access_id,))
        conn.commit()


def test_approving_sends_a_welcome_message_with_what_they_can_do(app, client, venue, monkeypatch):
    sent = []
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.admin_config.send_sms", lambda to, body: sent.append((to, body)) or True
    )

    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue, mobile="07796123456", invite_method="sms", email="")
    access_id = _get_access_id(app, venue)
    _put_into_pending_approval(app, access_id)
    sent.clear()  # drop the original invite send, only care about the approval one

    resp = client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/approve", follow_redirects=True)
    assert resp.status_code == 200

    assert len(sent) == 1
    to, body = sent[0]
    assert to == "07796123456"
    assert "welcome" in body.lower() or "approved" in body.lower()
    # actually tells them what they can now do, not just "you're approved"
    assert "clock in" in body.lower()
    assert "shift" in body.lower()


def test_approving_uses_email_when_that_was_the_invite_method(app, client, venue, monkeypatch):
    sent = []

    def fake_send_email(to, subject, body):
        sent.append((to, subject, body))
        return True

    monkeypatch.setattr("app.admin_config.send_email", fake_send_email)

    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue)  # default invite_method is email
    access_id = _get_access_id(app, venue)
    _put_into_pending_approval(app, access_id)
    sent.clear()

    client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/approve")

    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == "starter@example.com"
    assert "approved" in subject.lower()


def test_approving_twice_does_not_resend_the_welcome_message(app, client, venue, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.admin_config.send_email", lambda *a, **k: sent.append(1) or True
    )

    login_as_pub(client, venue["pub_id"])
    _create_invite(client, venue)
    access_id = _get_access_id(app, venue)
    _put_into_pending_approval(app, access_id)
    sent.clear()

    first = client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/approve")
    assert first.status_code == 302
    second = client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/approve")
    assert second.status_code == 404

    assert len(sent) == 1
