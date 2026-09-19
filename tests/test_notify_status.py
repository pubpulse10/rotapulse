"""
RotaPulse's send record and the checks behind /internal/mail-status and
/internal/notify-status (pricepulse docs/decisions.md D60).

send_sms and send_email swallow their failures so a page never 500s over one
text — which also means a dead Twilio token, an empty balance or a deleted
Brevo key stops every notification without anyone being told. These tests
lock in the record that makes that visible, the line between "our account is
broken" and "this one number is bad", and that nothing personal is kept.

No test here touches the network: Twilio's Client and smtplib.SMTP are replaced.
"""

import smtplib
import socket
from types import SimpleNamespace

import pytest
from twilio.base.exceptions import TwilioRestException

from app import config, notifications

SECRET = "test-internal-secret"
NUMBER = "07700900123"


def _rows():
    conn = notifications._log_connection()
    rows = [dict(r) for r in conn.execute(
        "SELECT channel, outcome, error FROM notify_attempts ORDER BY id")]
    conn.close()
    return rows


@pytest.fixture
def twilio_configured(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "test_token")
    monkeypatch.setattr(config, "TWILIO_FROM_NUMBER", "+447700900000")


@pytest.fixture
def mail_configured(monkeypatch):
    monkeypatch.setattr(config, "MAIL_SERVER", "smtp-relay.brevo.com")
    monkeypatch.setattr(config, "MAIL_PORT", 587)
    monkeypatch.setattr(config, "MAIL_USERNAME", "relay-login@example.com")
    monkeypatch.setattr(config, "MAIL_PASSWORD", "smtp-key")


def _twilio_send(monkeypatch, fail_with=None):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.messages = self

        def create(self, **kwargs):
            if fail_with is not None:
                raise fail_with
            return SimpleNamespace(sid="SM1")

    monkeypatch.setattr("twilio.rest.Client", FakeClient)


def _smtp(monkeypatch, fail_on=None, exc=None):
    """fail_on: 'login' or 'send' raises exc there."""
    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, **kwargs):
            pass

        def login(self, user, password):
            if fail_on == "login":
                raise exc

        def send_message(self, msg):
            if fail_on == "send":
                raise exc

    monkeypatch.setattr(notifications.smtplib, "SMTP", FakeSMTP)


# --------------------------------------------------------------------------- #
# The send record
# --------------------------------------------------------------------------- #

def test_a_sent_text_is_recorded(app, twilio_configured, monkeypatch):
    _twilio_send(monkeypatch)
    assert notifications.send_sms(NUMBER, "Your shift") is True
    assert _rows() == [{"channel": "sms", "outcome": "sent", "error": None}]


@pytest.mark.parametrize("exc, outcome", [
    (TwilioRestException(401, "uri", "Authenticate", code=20003), "service"),
    (TwilioRestException(400, "uri", "trial: unverified", code=21608), "service"),
    (TwilioRestException(503, "uri", "unavailable"), "transient"),
    (TwilioRestException(429, "uri", "too many", code=20429), "transient"),
    (TwilioRestException(400, "uri", "The 'To' number +447700900123 is not valid", code=21211), "recipient"),
])
def test_a_failed_text_is_recorded_with_why(app, twilio_configured, monkeypatch, exc, outcome):
    _twilio_send(monkeypatch, exc)
    assert notifications.send_sms(NUMBER, "Your shift") is False  # still swallowed
    [row] = _rows()
    assert row["outcome"] == outcome
    assert "7700900123" not in row["error"]  # Twilio's text quotes the number; never kept


def test_a_sent_email_is_recorded(app, mail_configured, monkeypatch):
    _smtp(monkeypatch)
    assert notifications.send_email("bob@example.com", "Rota", "Body") is True
    assert _rows() == [{"channel": "email", "outcome": "sent", "error": None}]


@pytest.mark.parametrize("fail_on, exc, outcome", [
    ("login", smtplib.SMTPAuthenticationError(535, b"Authentication failed"), "service"),
    ("send", smtplib.SMTPRecipientsRefused({"bob@example.com": (550, b"no such user")}), "recipient"),
    ("send", smtplib.SMTPResponseException(451, b"try later for bob@example.com"), "transient"),
    ("login", socket.timeout("timed out"), "transient"),
])
def test_a_failed_email_is_recorded_with_why(app, mail_configured, monkeypatch, fail_on, exc, outcome):
    _smtp(monkeypatch, fail_on, exc)
    assert notifications.send_email("bob@example.com", "Rota", "Body") is False
    [row] = _rows()
    assert row["outcome"] == outcome
    assert "bob@example.com" not in (row["error"] or "")


def test_nothing_is_recorded_when_not_configured(app, monkeypatch):
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", None)
    monkeypatch.setattr(config, "MAIL_SERVER", None)
    notifications.send_sms(NUMBER, "x")
    notifications.send_email("bob@example.com", "s", "b")
    assert _rows() == []


def test_a_broken_record_never_breaks_a_send(app, twilio_configured, monkeypatch):
    def broken():
        raise OSError("disk full")

    monkeypatch.setattr(notifications, "_log_connection", broken)
    _twilio_send(monkeypatch)
    assert notifications.send_sms(NUMBER, "Your shift") is True


def test_the_record_is_trimmed(app, monkeypatch):
    monkeypatch.setattr(notifications, "LOG_KEEP", 3)
    for _ in range(5):
        notifications._record("sms", "sent")
    assert len(_rows()) == 3


def test_recording_is_not_blocked_by_a_request_holding_the_main_database(app, twilio_configured, monkeypatch):
    """An invite is committed around its SMS, so the main database can be
    mid-write when the text goes. The record must land anyway, at once."""
    import sqlite3
    import time

    from app import db as db_module

    holder = sqlite3.connect(db_module.DB_PATH)
    holder.execute("CREATE TABLE IF NOT EXISTS lock_probe (x)")
    holder.execute("INSERT INTO lock_probe VALUES (1)")  # write lock held, uncommitted
    try:
        _twilio_send(monkeypatch)
        started = time.monotonic()
        notifications.send_sms(NUMBER, "Your shift")
        assert time.monotonic() - started < 2
        assert _rows() == [{"channel": "sms", "outcome": "sent", "error": None}]
    finally:
        holder.rollback()
        holder.close()


def test_recent_attempts_ignores_bad_numbers_when_judging_the_service(app):
    notifications._record("sms", "service", "Twilio 20003 (HTTP 401)")
    notifications._record("sms", "recipient", "Twilio 21211 (HTTP 400)")
    notifications._record("email", "sent")
    recent = notifications.recent_attempts()
    assert recent["sms"]["last"]["outcome"] == "service"
    assert recent["sms"]["failed_7d"] == 1
    assert recent["sms"]["bad_recipient_7d"] == 1
    assert recent["email"]["last"]["outcome"] == "sent"
    assert recent["email"]["sent_7d"] == 1


# --------------------------------------------------------------------------- #
# Live checks
# --------------------------------------------------------------------------- #

def _twilio_account(monkeypatch, status="active", type_="Full",
                    owned=True, fail_with=None):
    class Account:
        def fetch(self):
            if fail_with is not None:
                raise fail_with
            return SimpleNamespace(status=status, type=type_)

        balance = SimpleNamespace(fetch=lambda: SimpleNamespace(balance="12.50", currency="GBP"))
        incoming_phone_numbers = SimpleNamespace(
            list=lambda phone_number, limit: [object()] if owned else [])

    class FakeClient:
        def __init__(self, sid, token, http_client=None):
            self.api = SimpleNamespace(v2010=SimpleNamespace(accounts=lambda sid: Account()))

    monkeypatch.setattr("twilio.rest.Client", FakeClient)


def test_the_twilio_check_reports_the_account(twilio_configured, monkeypatch):
    _twilio_account(monkeypatch)
    result = notifications.check_twilio()
    assert result["ok"] is True
    assert result["status"] == "active"
    assert result["type"] == "Full"
    assert result["balance"] == 12.50
    assert result["currency"] == "GBP"
    assert result["from_number_on_account"] is True


def test_the_twilio_check_reports_a_rejected_token(twilio_configured, monkeypatch):
    _twilio_account(monkeypatch, fail_with=TwilioRestException(401, "uri", "Authenticate", code=20003))
    result = notifications.check_twilio()
    assert result["ok"] is False
    assert result["transient"] is False
    assert result["error"] == "Twilio 20003 (HTTP 401)"


def test_the_twilio_check_notices_a_released_number(twilio_configured, monkeypatch):
    _twilio_account(monkeypatch, owned=False)
    assert notifications.check_twilio()["from_number_on_account"] is False


def test_the_twilio_check_skips_the_number_lookup_for_a_sender_name(twilio_configured, monkeypatch):
    monkeypatch.setattr(config, "TWILIO_FROM_NUMBER", "PubPulse")
    _twilio_account(monkeypatch)
    assert notifications.check_twilio()["from_number_on_account"] is None


def test_the_twilio_check_says_when_twilio_is_not_set_up(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", None)
    assert notifications.check_twilio()["configured"] is False


def test_the_mail_check_logs_in_without_sending(mail_configured, monkeypatch):
    _smtp(monkeypatch)
    assert notifications.check_login() == {"configured": True, "ok": True, "error": None, "transient": False}
    _smtp(monkeypatch, "login", smtplib.SMTPAuthenticationError(535, b"Authentication failed"))
    result = notifications.check_login()
    assert result["ok"] is False and result["transient"] is False


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", ["/internal/mail-status", "/internal/notify-status"])
def test_the_endpoints_need_the_internal_secret(client, monkeypatch, path):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", SECRET)
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_notify_status_reports_twilio_and_history(client, twilio_configured, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", SECRET)
    _twilio_account(monkeypatch)
    notifications._record("sms", "sent")
    body = client.get("/internal/notify-status",
                      headers={"Authorization": f"Bearer {SECRET}"}).get_json()
    assert body["twilio"]["balance"] == 12.50
    assert body["recent"]["sms"]["sent_7d"] == 1
    assert body["recent"]["email"]["last"] is None


def test_notify_status_can_skip_the_twilio_lookup(client, twilio_configured, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", SECRET)

    def must_not_be_called(*a, **k):
        raise AssertionError("probe=0 must not call Twilio")

    monkeypatch.setattr("twilio.rest.Client", must_not_be_called)
    body = client.get("/internal/notify-status?probe=0",
                      headers={"Authorization": f"Bearer {SECRET}"}).get_json()
    assert body["twilio"] is None


def test_mail_status_reports_the_check(client, mail_configured, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", SECRET)
    _smtp(monkeypatch)
    body = client.get("/internal/mail-status",
                      headers={"Authorization": f"Bearer {SECRET}"}).get_json()
    assert body == {"configured": True, "ok": True, "error": None, "transient": False}
