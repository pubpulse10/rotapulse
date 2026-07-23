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

import smtplib
import sys
from email.message import EmailMessage

from app import config


def send_email(to: str, subject: str, body: str) -> None:
    if not config.MAIL_SERVER:
        print(f"[email:dev-fallback] To: {to} | Subject: {subject}\n{body}", flush=True, file=sys.stderr)
        return

    msg = EmailMessage()
    msg["From"] = config.MAIL_FROM or config.MAIL_USERNAME
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(config.MAIL_SERVER, config.MAIL_PORT) as smtp:
        smtp.starttls()
        if config.MAIL_USERNAME:
            smtp.login(config.MAIL_USERNAME, config.MAIL_PASSWORD)
        smtp.send_message(msg)


def send_sms(to: str, body: str) -> None:
    if not (config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and config.TWILIO_FROM_NUMBER):
        print(f"[sms:dev-fallback] To: {to}\n{body}", flush=True, file=sys.stderr)
        return

    from twilio.rest import Client

    client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    client.messages.create(to=to, from_=config.TWILIO_FROM_NUMBER, body=body)
