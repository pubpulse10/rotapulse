"""
Every outbound SMS must stay inside the GSM-7 alphabet.

Measured 2026-09-09: one em-dash in the approval message put the whole thing
into UCS-2, which drops an SMS from 153 characters per segment to 67 — that
one character was billing a 299-character message as 5 segments instead of 2.
Nothing warns you: the text arrives looking perfect and only the bill differs,
so it survived in production until someone counted. These tests count.

They cover the messages whose paths can be driven cheaply. The two that can't
(the weekly rota notify and the open-shift alert) are plain ASCII today and
built from dates and times, so they have far less room to drift.
"""

import pytest

from tests.conftest import create_active_staff, login_as_pub

# The GSM-7 default alphabet plus its extension table. Anything outside these
# forces the entire message to UCS-2 — it is not per-character.
GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENDED = set(r"^{}\[~]|€")


def assert_gsm7(body):
    outside = sorted({c for c in body if c not in GSM7 and c not in GSM7_EXTENDED})
    assert not outside, (
        "SMS body contains characters outside GSM-7, which doubles its cost: "
        + ", ".join(f"U+{ord(c):04X} ({c!r})" for c in outside)
    )


@pytest.fixture
def sms(monkeypatch):
    sent = []
    monkeypatch.setattr("app.admin_config.send_email", lambda *a, **k: True)
    monkeypatch.setattr(
        "app.admin_config.send_sms", lambda to, body: sent.append(body) or True
    )
    return sent


def _invite(client, venue):
    return client.post(
        f"/v/{venue['slug']}/admin/staff/create",
        data={
            "name": "Sam Barwell",
            "email": "",
            "mobile": "07796123456",
            "invite_method": "sms",
            "permission_level": "staff",
        },
        follow_redirects=True,
    )


def _access_id(app, venue):
    from app import db as db_module

    with app.app_context():
        return db_module.get_db().execute(
            """SELECT app_access.id FROM app_access
               JOIN venue_membership ON venue_membership.id = app_access.venue_membership_id
               WHERE venue_membership.venue_id = ? ORDER BY app_access.id DESC""",
            (venue["id"],),
        ).fetchone()["id"]


def test_the_invite_sms_is_gsm7(app, client, venue, sms):
    """The highest-volume one after the reminder: every starter gets it."""
    login_as_pub(client, venue["pub_id"])
    _invite(client, venue)

    assert len(sms) == 1
    assert_gsm7(sms[0])


def test_the_approval_sms_is_gsm7(app, client, venue, sms):
    from app import db as db_module

    login_as_pub(client, venue["pub_id"])
    _invite(client, venue)
    access_id = _access_id(app, venue)
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE app_access SET status = 'pending_approval' WHERE id = ?", (access_id,))
        conn.commit()
    sms.clear()

    client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/approve")

    assert len(sms) == 1
    assert_gsm7(sms[0])


def test_the_clock_in_reminder_sms_is_gsm7(app, venue, monkeypatch):
    """One per shift, per person — the most-sent text the app has, so the one
    where a stray character costs the most."""
    from datetime import date, datetime, timedelta

    from app import db as db_module
    from scripts.check_shift_notifications import remind_staff_to_clock_in

    sent = []
    monkeypatch.setattr(
        "app.notification_settings.send_sms", lambda to, body: sent.append(body) or True
    )

    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="Sam Barwell")
    today = date.today()
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET mobile = '07796123456' WHERE id = ?", (person_id,))
        conn.execute(
            """INSERT INTO shift (venue_id, person_id, shift_date, start_time, end_time, status)
               VALUES (?, ?, ?, '09:00', '17:00', 'scheduled')""",
            (venue["id"], person_id, today.isoformat()),
        )
        conn.commit()

        shift_start = datetime.combine(today, datetime.min.time()) + timedelta(hours=9)
        remind_staff_to_clock_in(db_module.get_db(), shift_start + timedelta(minutes=11))

    assert len(sent) == 1
    assert_gsm7(sent[0])


def test_the_password_reset_sms_is_gsm7(app, client, venue, monkeypatch):
    from app import rota_login

    sent = []
    monkeypatch.setattr(rota_login, "send_sms", lambda to, body: sent.append(body) or True)

    person_id, _membership_id, _email = create_active_staff(app, venue["id"], name="Sam Barwell")
    from app import db as db_module

    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE person SET email = NULL, mobile = '07796123456' WHERE id = ?", (person_id,))
        conn.commit()

    client.post(
        f"/v/{venue['slug']}/forgot-password",
        data={"identifier": "07796123456"},
        follow_redirects=True,
    )

    assert len(sent) == 1
    assert_gsm7(sent[0])
