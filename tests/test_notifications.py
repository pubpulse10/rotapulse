"""
Regression coverage for a real production 500: inviting a staff member by
SMS crashed admin_config.create_staff with an unhandled
twilio.base.exceptions.TwilioRestException ("Invalid 'To' Phone Number")
because a plain UK domestic-format number ("07796...") was passed straight
to Twilio, which requires E.164 ("+447796..."). The invite row had already
been committed by that point, so the admin saw a 500 for a record that
actually existed underneath it.
"""

import pytest

from app.notifications import _to_e164_uk, send_email, send_sms
from tests.conftest import login_as_pub


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("07796123456", "+447796123456"),
        ("07796 123 456", "+447796123456"),
        ("(07796) 123-456", "+447796123456"),
        ("+447796123456", "+447796123456"),
        ("447796123456", "+447796123456"),
        ("00447796123456", "+447796123456"),
    ],
)
def test_to_e164_uk_normalises_common_formats(raw, expected):
    assert _to_e164_uk(raw) == expected


def test_send_sms_normalises_number_before_calling_twilio(monkeypatch, app):
    monkeypatch.setattr("app.config.TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setattr("app.config.TWILIO_AUTH_TOKEN", "test_token")
    monkeypatch.setattr("app.config.TWILIO_FROM_NUMBER", "+15551234567")

    captured = {}

    class FakeMessages:
        def create(self, to, from_, body):
            captured["to"] = to
            captured["from_"] = from_
            captured["body"] = body

    class FakeTwilioClient:
        def __init__(self, *a, **k):
            self.messages = FakeMessages()

    monkeypatch.setattr("twilio.rest.Client", FakeTwilioClient)

    with app.app_context():
        send_sms("07796123456", "hello")

    assert captured["to"] == "+447796123456"


def test_send_sms_swallows_twilio_failure_instead_of_raising(monkeypatch, app):
    monkeypatch.setattr("app.config.TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setattr("app.config.TWILIO_AUTH_TOKEN", "test_token")
    monkeypatch.setattr("app.config.TWILIO_FROM_NUMBER", "+15551234567")

    class FakeMessages:
        def create(self, to, from_, body):
            raise RuntimeError("simulated TwilioRestException: Invalid 'To' Phone Number")

    class FakeTwilioClient:
        def __init__(self, *a, **k):
            self.messages = FakeMessages()

    monkeypatch.setattr("twilio.rest.Client", FakeTwilioClient)

    with app.app_context():
        send_sms("not-a-real-number", "hello")  # must not raise


def test_send_email_swallows_smtp_failure_instead_of_raising(monkeypatch, app):
    monkeypatch.setattr("app.config.MAIL_SERVER", "smtp.example.com")
    monkeypatch.setattr("app.config.MAIL_PORT", 587)

    import smtplib

    class FakeSMTP:
        def __init__(self, *a, **k):
            raise smtplib.SMTPConnectError(421, "simulated connection failure")

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    with app.app_context():
        send_email("someone@example.com", "subject", "body")  # must not raise


def test_invite_by_sms_with_domestic_uk_number_does_not_500(app, client, venue, monkeypatch):
    """The exact real-world scenario that crashed in production: inviting a
    staff member via SMS with a plain UK mobile number, but Twilio rejects
    it (simulated here as any exception, matching the swallow-failures fix
    above rather than depending on twilio's real exception type)."""
    monkeypatch.setattr("app.config.TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setattr("app.config.TWILIO_AUTH_TOKEN", "test_token")
    monkeypatch.setattr("app.config.TWILIO_FROM_NUMBER", "+15551234567")

    class FakeMessages:
        def create(self, to, from_, body):
            raise RuntimeError("simulated TwilioRestException: Invalid 'To' Phone Number")

    class FakeTwilioClient:
        def __init__(self, *a, **k):
            self.messages = FakeMessages()

    monkeypatch.setattr("twilio.rest.Client", FakeTwilioClient)

    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/admin/staff/create",
        data={
            "name": "New Starter",
            "mobile": "07796123456",
            "invite_method": "sms",
            "permission_level": "staff",
        },
    )
    assert resp.status_code == 302  # redirect back to staff list, not a 500

    from app import db as db_module

    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute("SELECT * FROM person WHERE name = 'New Starter'").fetchone()
    assert row is not None  # the invite was still actually created
