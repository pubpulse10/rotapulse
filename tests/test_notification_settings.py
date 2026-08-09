from app import db as db_module
from app.notification_settings import notify_admins
from tests.conftest import create_active_staff, login_as_pub, login_as_person


def _enable_notification(app, venue, notification_type, method, recipient_person_ids):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            """INSERT INTO notification_setting (venue_id, notification_type, enabled, method)
               VALUES (?, ?, 1, ?)""",
            (venue["id"], notification_type, method),
        )
        setting_id = conn.execute(
            "SELECT id FROM notification_setting WHERE venue_id = ? AND notification_type = ?",
            (venue["id"], notification_type),
        ).fetchone()["id"]
        for person_id in recipient_person_ids:
            conn.execute(
                "INSERT INTO notification_recipient (notification_setting_id, person_id) VALUES (?, ?)",
                (setting_id, person_id),
            )
        conn.commit()


# ---------- notify_admins() itself ----------


def test_notify_admins_is_a_noop_when_no_setting_exists(app, venue):
    with app.app_context():
        conn = db_module.get_db()
        # Must not raise, must not send anything - no setting row at all.
        notify_admins(conn, venue, "swap_request", "subject", "body")


def test_notify_admins_is_a_noop_when_disabled(app, venue, monkeypatch):
    sent = []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: sent.append(a) or True)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO notification_setting (venue_id, notification_type, enabled, method) VALUES (?, 'swap_request', 0, 'email')",
            (venue["id"],),
        )
        conn.commit()
        notify_admins(conn, venue, "swap_request", "subject", "body")
    assert sent == []


def test_notify_admins_sends_email_to_selected_recipients(app, venue, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.notification_settings.send_email",
        lambda to, subject, body: sent.append((to, subject, body)) or True,
    )
    _enable_notification(app, venue, "swap_request", "email", [venue["owner_person_id"]])

    with app.app_context():
        conn = db_module.get_db()
        notify_admins(conn, venue, "swap_request", "Swap needs approval", "details here")

    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == "owner@example.com"
    assert subject == "Swap needs approval"


def test_notify_admins_sends_sms_when_method_is_sms(app, venue, monkeypatch):
    sms_sent = []
    monkeypatch.setattr(
        "app.notification_settings.send_sms", lambda to, body: sms_sent.append((to, body)) or True
    )
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET mobile = '07700900111' WHERE id = ?", (venue["owner_person_id"],))
        conn.commit()
    _enable_notification(app, venue, "leave_request", "sms", [venue["owner_person_id"]])

    with app.app_context():
        conn = db_module.get_db()
        notify_admins(conn, venue, "leave_request", "subject", "body text")

    assert len(sms_sent) == 1
    assert sms_sent[0][0] == "07700900111"


def test_notify_admins_sends_both_when_method_is_both(app, venue, monkeypatch):
    emails, texts = [], []
    monkeypatch.setattr("app.notification_settings.send_email", lambda *a, **k: emails.append(a) or True)
    monkeypatch.setattr("app.notification_settings.send_sms", lambda *a, **k: texts.append(a) or True)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET mobile = '07700900111' WHERE id = ?", (venue["owner_person_id"],))
        conn.commit()
    _enable_notification(app, venue, "open_shift_claimed", "both", [venue["owner_person_id"]])

    with app.app_context():
        conn = db_module.get_db()
        notify_admins(conn, venue, "open_shift_claimed", "subject", "body")

    assert len(emails) == 1
    assert len(texts) == 1


# ---------- Settings page ----------


def test_notification_settings_page_lists_all_five_types(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.get(f"/v/{venue['slug']}/admin/notifications")
    assert resp.status_code == 200
    for label_fragment in ("Missed clock-in", "Missed clock-out", "Swap request", "Leave request", "Open shift claimed"):
        assert label_fragment.encode() in resp.data


def test_notification_settings_requires_app_admin_not_rota_admin(app, client, venue):
    rota_admin_id, _membership_id, _email = create_active_staff(
        app, venue["id"], name="RotaAdminOnly", permission_level="rota_admin"
    )
    login_as_person(client, rota_admin_id)
    resp = client.get(f"/v/{venue['slug']}/admin/notifications")
    assert resp.status_code == 302  # bounced to the shared PubPulse login, same as roles/settings


def test_saving_notification_settings_persists_enabled_method_and_recipients(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/admin/notifications",
        data={
            "enabled_swap_request": "on",
            "method_swap_request": "sms",
            "recipients_swap_request": str(venue["owner_person_id"]),
            "method_missed_clock_in": "email",
            "method_missed_clock_out": "email",
            "method_leave_request": "email",
            "method_open_shift_claimed": "email",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Notification settings saved." in resp.data

    with app.app_context():
        conn = db_module.get_db()
        setting = conn.execute(
            "SELECT * FROM notification_setting WHERE venue_id = ? AND notification_type = 'swap_request'",
            (venue["id"],),
        ).fetchone()
        assert setting["enabled"] == 1
        assert setting["method"] == "sms"
        recipients = conn.execute(
            "SELECT person_id FROM notification_recipient WHERE notification_setting_id = ?", (setting["id"],)
        ).fetchall()
        assert {r["person_id"] for r in recipients} == {venue["owner_person_id"]}


def test_saving_ignores_a_recipient_id_that_is_not_actually_eligible(app, client, venue):
    """A person_id posted for a recipients_* field that ISN'T currently an
    active app_admin/rota_admin at this venue (e.g. tampered form, or the
    person left between page load and save) must not be silently accepted."""
    outsider_person_id, _membership_id, _email = create_active_staff(
        app, venue["id"], name="Outsider", permission_level="staff"  # staff, not admin-tier
    )
    login_as_pub(client, venue["pub_id"])
    client.post(
        f"/v/{venue['slug']}/admin/notifications",
        data={
            "enabled_leave_request": "on",
            "method_leave_request": "email",
            "recipients_leave_request": str(outsider_person_id),
        },
    )

    with app.app_context():
        conn = db_module.get_db()
        setting = conn.execute(
            "SELECT * FROM notification_setting WHERE venue_id = ? AND notification_type = 'leave_request'",
            (venue["id"],),
        ).fetchone()
        recipients = conn.execute(
            "SELECT person_id FROM notification_recipient WHERE notification_setting_id = ?", (setting["id"],)
        ).fetchall()
    assert recipients == []  # the staff-level person was rejected as a recipient


# ---------- Event-triggered integrations ----------


def test_leave_request_notifies_enabled_recipients(app, client, venue, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.staff_portal.notify_admins",
        lambda db, venue, kind, subject, body: sent.append((kind, subject, body)),
    )
    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="Leave Requester")
    login_as_person(client, person_id)

    resp = client.post(
        f"/v/{venue['slug']}/staff/leave",
        data={"start_date": "2026-09-01", "end_date": "2026-09-05"},
    )
    assert resp.status_code == 302
    assert len(sent) == 1
    assert sent[0][0] == "leave_request"
    assert "Leave Requester" in sent[0][2]


def test_open_shift_claim_notifies_enabled_recipients(app, client, venue, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.staff_portal.notify_admins",
        lambda db, venue, kind, subject, body: sent.append((kind, subject, body)),
    )
    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="Shift Claimer")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO shift (venue_id, shift_date, start_time, end_time, status) VALUES (?, '2026-09-10', '09:00', '17:00', 'open')",
            (venue["id"],),
        )
        conn.commit()
        shift_id = conn.execute("SELECT id FROM shift WHERE status = 'open'").fetchone()["id"]

    login_as_person(client, person_id)
    resp = client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim")
    assert resp.status_code == 302
    assert len(sent) == 1
    assert sent[0][0] == "open_shift_claimed"


def test_open_shift_claim_does_not_notify_if_someone_else_already_claimed_it(app, client, venue, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.staff_portal.notify_admins",
        lambda db, venue, kind, subject, body: sent.append(kind),
    )
    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="Too Slow")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO shift (venue_id, shift_date, start_time, end_time, status) VALUES (?, '2026-09-11', '09:00', '17:00', 'scheduled')",
            (venue["id"],),
        )  # already scheduled, not open - simulates someone beat them to it
        conn.commit()
        shift_id = conn.execute("SELECT id FROM shift WHERE shift_date = '2026-09-11'").fetchone()["id"]

    login_as_person(client, person_id)
    client.post(f"/v/{venue['slug']}/staff/open-shifts/{shift_id}/claim")
    assert sent == []


def test_swap_accept_notifies_enabled_recipients(app, client, venue, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.staff_portal.notify_admins",
        lambda db, venue, kind, subject, body: sent.append((kind, subject, body)),
    )
    requester_id, _m1, _e1 = create_active_staff(app, venue["id"], name="Requester")
    accepter_id, _m2, _e2 = create_active_staff(app, venue["id"], name="Accepter")
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status) VALUES (?, ?, '2026-09-12', '09:00', '17:00', 'scheduled')",
            (venue["id"], requester_id),
        )
        conn.commit()
        shift_id = conn.execute("SELECT id FROM shift WHERE shift_date = '2026-09-12'").fetchone()["id"]
        conn.execute(
            "INSERT INTO shift_swap_request (shift_id, from_person_id, to_person_id, status) VALUES (?, ?, ?, 'pending_peer')",
            (shift_id, requester_id, accepter_id),
        )
        conn.commit()
        swap_id = conn.execute("SELECT id FROM shift_swap_request WHERE shift_id = ?", (shift_id,)).fetchone()["id"]

    login_as_person(client, accepter_id)
    resp = client.post(f"/v/{venue['slug']}/staff/swap/{swap_id}/accept")
    assert resp.status_code == 302
    assert len(sent) == 1
    assert sent[0][0] == "swap_request"
    assert "Accepter" in sent[0][2]
