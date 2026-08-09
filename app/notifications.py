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
import sys
from email.message import EmailMessage

from app import config


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
        return True
    except Exception:
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
        return True
    except Exception:
        # A delivery failure (bad number even after normalising, Twilio
        # outage, etc.) must never crash the caller's request — confirmed
        # live: an unhandled TwilioRestException here 500'd
        # admin_config.create_staff AFTER the invite row was already
        # committed, leaving an admin looking at an error page for a record
        # that actually existed underneath it.
        print(f"[sms:failed] To: {to}\n{body}", flush=True, file=sys.stderr)
        return False
