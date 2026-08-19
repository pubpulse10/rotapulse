"""Local password login + forgot/reset-password (app/rota_login.py). No
prior coverage existed for this blueprint at all before the SMS-fallback
fix below — a real report, 2026-08-19: a staff member invited by SMS with
no email ever captured had no self-service password-reset path, and the
flow failed completely silently (same "we've sent a reset link" message
whether anything was actually sent or not)."""

from werkzeug.security import generate_password_hash

from app import db as db_module
from tests.conftest import TEST_STAFF_PASSWORD, create_active_staff


def _sms_only_staff(app, venue_id, name="Sms Only Person", mobile="+447700900111"):
    """A staff member with a mobile but deliberately no email — the exact
    shape that exposed the bug."""
    with app.app_context():
        conn = db_module.get_db()
        person_id = conn.execute(
            "INSERT INTO person (name, email, mobile, password_hash) VALUES (?, NULL, ?, ?)",
            (name, mobile, generate_password_hash(TEST_STAFF_PASSWORD)),
        ).lastrowid
        membership_id = conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')",
            (person_id, venue_id),
        ).lastrowid
        app_id = db_module.get_app_id(conn, "rotapulse")
        conn.execute(
            """INSERT INTO app_access (venue_membership_id, app_id, permission_level, status, accepted_at, approved_at)
               VALUES (?, ?, 'staff', 'active', datetime('now'), datetime('now'))""",
            (membership_id, app_id),
        )
        conn.commit()
    return person_id


def test_forgot_password_emails_a_reset_link_when_person_has_email(app, client, venue, monkeypatch):
    sent = []
    monkeypatch.setattr("app.rota_login.send_email", lambda to, subject, body: sent.append((to, subject, body)) or True)
    monkeypatch.setattr("app.rota_login.send_sms", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not SMS")))
    _person_id, _m, email = create_active_staff(app, venue["id"], name="Has Email")

    resp = client.post(f"/v/{venue['slug']}/forgot-password", data={"identifier": email}, follow_redirects=True)
    assert resp.status_code == 200
    assert len(sent) == 1
    assert sent[0][0] == email
    assert "/reset-password/" in sent[0][2]

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT COUNT(*) AS n FROM rota_password_reset_token").fetchone()["n"] == 1


def test_forgot_password_falls_back_to_sms_when_no_email(app, client, venue, monkeypatch):
    """The actual fix: a person with no email but a mobile on file now gets
    the reset link by text instead of the request silently doing nothing."""
    emailed = []
    texted = []
    monkeypatch.setattr("app.rota_login.send_email", lambda *a, **k: emailed.append(a) or True)
    monkeypatch.setattr("app.rota_login.send_sms", lambda to, body: texted.append((to, body)) or True)
    _sms_only_staff(app, venue["id"], name="Kelly Test", mobile="+447875571419")

    resp = client.post(
        f"/v/{venue['slug']}/forgot-password", data={"identifier": "+447875571419"}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert emailed == []
    assert len(texted) == 1
    assert texted[0][0] == "+447875571419"
    assert "/reset-password/" in texted[0][1]

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT COUNT(*) AS n FROM rota_password_reset_token").fetchone()["n"] == 1


def test_forgot_password_prefers_email_over_sms_when_both_exist(app, client, venue, monkeypatch):
    emailed = []
    texted = []
    monkeypatch.setattr("app.rota_login.send_email", lambda *a, **k: emailed.append(a) or True)
    monkeypatch.setattr("app.rota_login.send_sms", lambda *a, **k: texted.append(a) or True)
    with app.app_context():
        conn = db_module.get_db()
        person_id = conn.execute(
            "INSERT INTO person (name, email, mobile, password_hash) VALUES (?, ?, ?, ?)",
            ("Both Contacts", "both@example.com", "+447700900222", generate_password_hash(TEST_STAFF_PASSWORD)),
        ).lastrowid
        conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')",
            (person_id, venue["id"]),
        )
        conn.commit()

    resp = client.post(f"/v/{venue['slug']}/forgot-password", data={"identifier": "both@example.com"})
    assert resp.status_code == 302
    assert len(emailed) == 1
    assert texted == []


def test_forgot_password_sends_nothing_for_an_unknown_identifier(app, client, venue, monkeypatch):
    """No account-enumeration leak: same flash either way, and genuinely
    nothing gets sent for an identifier that doesn't match anyone."""
    emailed = []
    texted = []
    monkeypatch.setattr("app.rota_login.send_email", lambda *a, **k: emailed.append(a) or True)
    monkeypatch.setattr("app.rota_login.send_sms", lambda *a, **k: texted.append(a) or True)

    resp = client.post(
        f"/v/{venue['slug']}/forgot-password", data={"identifier": "nobody@example.com"}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert b"sent a reset link" in resp.data
    assert emailed == []
    assert texted == []


def test_reset_password_via_sms_delivered_token_actually_works(app, client, venue, monkeypatch):
    """End-to-end: the token texted out in the SMS fallback is a genuine,
    usable reset link, not just a message that looks right."""
    texted = []
    monkeypatch.setattr("app.rota_login.send_email", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr("app.rota_login.send_sms", lambda to, body: texted.append(body) or True)
    person_id = _sms_only_staff(app, venue["id"], name="Reset Via Sms", mobile="+447700900333")

    client.post(f"/v/{venue['slug']}/forgot-password", data={"identifier": "+447700900333"})
    reset_path = texted[0].split("RotaPulse password reset: ")[1].split(" (")[0]
    reset_path = reset_path[reset_path.index(f"/v/{venue['slug']}/reset-password/"):]

    resp = client.post(
        reset_path,
        data={"password": "a-new-strong-password-1", "confirm_password": "a-new-strong-password-1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        person = conn.execute("SELECT password_hash FROM person WHERE id = ?", (person_id,)).fetchone()
        assert person["password_hash"] != generate_password_hash(TEST_STAFF_PASSWORD)  # actually changed
