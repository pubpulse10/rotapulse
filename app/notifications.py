"""
The one shared "send email" / "send SMS" pipe (spec §2.4) — invites,
shift-swap notifications, open-shift alerts, and the weekly digest all call
these two functions with different templates, rather than each feature
wiring up its own client.

send_email mirrors the sibling apps' app/email.py exactly (stdlib smtplib,
Brevo SMTP relay, logs instead of sending when unconfigured).

send_sms is new to the family (Twilio, spec §2.4) — same dev-fallback
philosophy: unset Twilio credentials means the message is logged, not sent,
so every SMS-triggering flow (invites, open-shift notify, digest) stays
fully testable without a real Twilio account.
"""

import re
import smtplib
import socket
import sqlite3
import sys
from email.message import EmailMessage

from app import config, db

# Every real send attempt is recorded (pricepulse D60). Both functions below
# swallow their failures by design — a text that can't go must never 500 the
# page that triggered it — which also means a dead Twilio token or Brevo key
# stops every notification without anyone being told. The record is what the
# Hub's health board reads to say so.
#
# Its own SQLite file, rotapulse.notify.db beside rotapulse.db, not a table in
# it: sends happen inside requests that may hold the main database's write
# lock (an invite is committed around its SMS), and a write there would wait
# out the lock and be lost. Same persistent disk, so it survives deploys; not backed up, on
# purpose — it is a monitoring log. It never holds a recipient, a message or
# an error text that might quote one: only the channel, the outcome and a code.
_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_attempts (
    id INTEGER PRIMARY KEY,
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    channel TEXT NOT NULL,
    outcome TEXT NOT NULL,
    error TEXT
)
"""
# outcome: 'sent'; or why it failed —
#   'service'   lasting, and ours to fix: rejected credentials, a suspended or
#               trial account, a From number or sender that isn't allowed
#   'transient' an outage, timeout or rate limit: worth retrying, nothing to fix
#   'recipient' this one number or address is bad: the landlord's data, not a
#               fault in the service, so it never turns the board amber
LOG_KEEP = 2000

# Twilio error codes that are about the number being sent to, not about our
# account. https://www.twilio.com/docs/api/errors
_TWILIO_RECIPIENT_CODES = {21211, 21214, 21217, 21610, 21612, 21614}

# How long the health checks wait. Short, because the Hub's board is waiting.
CHECK_TIMEOUT = 8


def send_email(to: str, subject: str, body: str) -> bool:
    """Returns True if the message was sent (or dev-fallback-logged), False
    if a real send was attempted and failed — callers that need to show the
    admin whether a notification actually got there (e.g. invite delivery
    status) can act on this instead of just trusting it silently worked."""
    if not config.MAIL_SERVER:
        print(f"[email:dev-fallback] To: {to} | Subject: {subject}\n{body}", flush=True, file=sys.stderr)
        return True

    msg = EmailMessage()
    msg["From"] = config.MAIL_FROM or config.MAIL_USERNAME
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(config.MAIL_SERVER, config.MAIL_PORT) as smtp:
            smtp.starttls()
            if config.MAIL_USERNAME:
                smtp.login(config.MAIL_USERNAME, config.MAIL_PASSWORD)
            smtp.send_message(msg)
        _record("email", "sent")
        return True
    except Exception as exc:
        _record("email", *_classify_email_failure(exc))
        # A delivery failure (bad address, SMTP outage, etc.) must never crash
        # the caller's request — the same "log instead of send" fallback
        # philosophy as the unconfigured-credentials case above, just for the
        # send-attempt-failed case instead of the never-attempted case.
        print(f"[email:failed] To: {to} | Subject: {subject}\n{body}", flush=True, file=sys.stderr)
        return False


def _to_e164_uk(raw: str) -> str:
    """Best-effort normalisation of a UK mobile number to E.164 (+447...),
    which Twilio requires — confirmed live in production: a plain domestic
    '07...' number is flatly rejected ("Invalid 'To' Phone Number"). Almost
    nobody types the +44 prefix themselves, so without this every UK number
    entered the ordinary way fails every time, not just malformed ones."""
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("+"):
        return digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):
        return "+44" + digits[1:]
    if digits.startswith("44"):
        return "+" + digits
    return raw  # unrecognised shape — let Twilio's own validation reject it


def send_sms(to: str, body: str) -> bool:
    """Returns True if the message was sent (or dev-fallback-logged), False
    if a real send was attempted and failed — see send_email's docstring."""
    if not (config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and config.TWILIO_FROM_NUMBER):
        print(f"[sms:dev-fallback] To: {to}\n{body}", flush=True, file=sys.stderr)
        return True

    from twilio.rest import Client

    client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    try:
        client.messages.create(to=_to_e164_uk(to), from_=config.TWILIO_FROM_NUMBER, body=body)
        _record("sms", "sent")
        return True
    except Exception as exc:
        _record("sms", *_classify_twilio_failure(exc))
        # A delivery failure (bad number even after normalising, Twilio
        # outage, etc.) must never crash the caller's request — confirmed
        # live: an unhandled TwilioRestException here 500'd
        # admin_config.create_staff AFTER the invite row was already
        # committed, leaving an admin looking at an error page for a record
        # that actually existed underneath it.
        print(f"[sms:failed] To: {to}\n{body}", flush=True, file=sys.stderr)
        return False


# --------------------------------------------------------------------------- #
# The send record and the health checks the Hub reads (pricepulse D60)
# --------------------------------------------------------------------------- #

def _log_connection():
    # Named after the main database, so it sits beside it (data/rotapulse.notify.db)
    # and a test pointing DB_PATH at a temp file gets its own log too.
    conn = sqlite3.connect(db.DB_PATH.with_suffix(".notify.db"), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute(_LOG_SCHEMA)
    return conn


def _record(channel, outcome, error=None):
    """Never raises: a monitoring write must not be what breaks a send."""
    try:
        db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = _log_connection()
        try:
            conn.execute("INSERT INTO notify_attempts (channel, outcome, error) VALUES (?, ?, ?)",
                         (channel, outcome, error))
            conn.execute("DELETE FROM notify_attempts WHERE id <= "
                         "(SELECT MAX(id) FROM notify_attempts) - ?", (LOG_KEEP,))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[notify-log:failed] {type(exc).__name__}", flush=True, file=sys.stderr)


def _classify_twilio_failure(exc):
    """(outcome, error) for a failed send. Only Twilio's code and status are
    kept — its message text quotes the number."""
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    if isinstance(status, int):
        label = f"Twilio {code} (HTTP {status})" if code else f"Twilio HTTP {status}"
        if code in _TWILIO_RECIPIENT_CODES:
            return "recipient", label
        if status == 429 or status >= 500:
            return "transient", label
        return "service", label
    if isinstance(exc, OSError) or "Connection" in type(exc).__name__ or "Timeout" in type(exc).__name__:
        return "transient", type(exc).__name__
    return "service", type(exc).__name__


def _classify_email_failure(exc):
    """(outcome, error) for a failed send. Codes only — a relay's reply text
    can quote the address."""
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "recipient", "recipient refused"
    if isinstance(exc, smtplib.SMTPResponseException):
        outcome = "transient" if 400 <= exc.smtp_code < 500 else "service"
        return outcome, f"SMTP {exc.smtp_code} ({type(exc).__name__})"
    _message, transient = describe_smtp_failure(exc)
    return ("transient" if transient else "service"), type(exc).__name__


def describe_smtp_failure(exc):
    """(message, transient) for an exception from the mail relay. The message
    never carries credentials or addresses — used by the live login check,
    which sends to nobody."""
    if isinstance(exc, smtplib.SMTPResponseException):
        text = exc.smtp_error
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        return f"SMTP {exc.smtp_code}: {str(text)[:120]}", 400 <= exc.smtp_code < 500
    if isinstance(exc, socket.gaierror):
        return f"mail server {config.MAIL_SERVER!r} not found", False
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "the mail server hung up", True
    if isinstance(exc, smtplib.SMTPException):
        return f"{type(exc).__name__}: {str(exc)[:120]}", False
    if isinstance(exc, OSError):
        return f"couldn't reach the mail server ({type(exc).__name__})", True
    return f"{type(exc).__name__}: {str(exc)[:120]}", False


def check_login() -> dict:
    """Log in to the mail relay and quit, sending nothing, to prove the
    credentials still work. Returns {"configured", "ok", "error",
    "transient"} — the same shape as the sibling apps' app/email.py."""
    if not config.MAIL_SERVER:
        return {"configured": False, "ok": None, "error": None, "transient": False}
    try:
        with smtplib.SMTP(config.MAIL_SERVER, config.MAIL_PORT, timeout=CHECK_TIMEOUT) as smtp:
            smtp.starttls()
            if config.MAIL_USERNAME:
                smtp.login(config.MAIL_USERNAME, config.MAIL_PASSWORD)
        return {"configured": True, "ok": True, "error": None, "transient": False}
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        error, transient = describe_smtp_failure(exc)
        return {"configured": True, "ok": False, "error": error, "transient": transient}


def check_twilio() -> dict:
    """Ask Twilio about the account without sending anything: is the token
    accepted, is the account active (and not still a trial, which only
    reaches verified numbers), what is the balance, and is the From number
    still on the account. All free reads.

    Returns the facts; the Hub decides what counts as broken. ok=False means
    Twilio couldn't be asked at all, with error and transient as for mail.
    """
    blank = {"status": None, "type": None, "balance": None, "currency": None,
             "from_number_on_account": None}
    if not (config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and config.TWILIO_FROM_NUMBER):
        return {"configured": False, "ok": None, "error": None, "transient": False, **blank}
    try:
        from twilio.http.http_client import TwilioHttpClient
        from twilio.rest import Client

        client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN,
                        http_client=TwilioHttpClient(timeout=CHECK_TIMEOUT))
        account = client.api.v2010.accounts(config.TWILIO_ACCOUNT_SID)
        info = account.fetch()
        balance = account.balance.fetch()
        from_owned = None
        # An alphanumeric sender ("PubPulse") isn't a number to look up.
        if config.TWILIO_FROM_NUMBER.startswith("+"):
            from_owned = bool(account.incoming_phone_numbers.list(
                phone_number=config.TWILIO_FROM_NUMBER, limit=1))
        return {"configured": True, "ok": True, "error": None, "transient": False,
                "status": info.status, "type": info.type,
                "balance": float(balance.balance), "currency": balance.currency,
                "from_number_on_account": from_owned}
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        outcome, error = _classify_twilio_failure(exc)
        return {"configured": True, "ok": False, "error": error,
                "transient": outcome == "transient", **blank}


def recent_attempts() -> dict:
    """Per channel: the latest attempt that wasn't a bad recipient, and the
    last week's counts. For the Hub's "texts and emails are getting out"."""
    conn = _log_connection()
    try:
        out = {}
        for channel in ("sms", "email"):
            last = conn.execute(
                "SELECT sent_at, outcome, error FROM notify_attempts "
                "WHERE channel = ? AND outcome != 'recipient' ORDER BY id DESC LIMIT 1",
                (channel,)).fetchone()
            week = conn.execute(
                "SELECT COALESCE(SUM(outcome = 'sent'), 0) AS sent, "
                "COALESCE(SUM(outcome IN ('service', 'transient')), 0) AS failed, "
                "COALESCE(SUM(outcome = 'recipient'), 0) AS recipient "
                "FROM notify_attempts WHERE channel = ? AND sent_at >= datetime('now', '-7 days')",
                (channel,)).fetchone()
            out[channel] = {
                "last": dict(last) if last else None,
                "sent_7d": week["sent"], "failed_7d": week["failed"],
                "bad_recipient_7d": week["recipient"],
            }
        return out
    finally:
        conn.close()
