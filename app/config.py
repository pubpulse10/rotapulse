"""
Environment-sourced configuration.

SECRET_KEY has no insecure fallback — the app refuses to start without one.
It must also be the *same* value as PricePulse's and TaskPulse's SECRET_KEY
in production — that pairing (plus a shared SESSION_COOKIE_DOMAIN) is the
entire cross-app SSO mechanism: one PubPulse account, recognised by every
app via the same signed session cookie, no token service needed for
*reading* identity. This is also what lets TaskPulse read
session['rotapulse_person_id'] directly for the clock-in recognition
differentiator (see app/internal.py and the taskpulse repo's
app/rotapulse_client.py).
"""

import os
from datetime import timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # python-dotenv is a local-dev convenience, not a hard requirement

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set. Sessions can't be signed without it, so the "
        "app won't start. Set it in your environment or in a .env file — "
        "see .env.example."
    )

SESSION_COOKIE_SAMESITE = "Lax"
# This session doubles as the "refresh token" for staff logins (spec §4's
# "most staff rarely see the login screen day-to-day"). Revocation for a
# departed staff member is handled by flipping app_access.status, checked on
# every request via rota_auth, not by any separate token-invalidation.
# Rolling: SESSION_REFRESH_EACH_REQUEST re-issues the cookie on every request,
# so active staff stay logged in indefinitely, but a stolen/idle cookie
# expires 30 days after its last use rather than lasting six months.
PERMANENT_SESSION_LIFETIME = timedelta(days=30)
SESSION_REFRESH_EACH_REQUEST = True
# Only force HTTPS-only cookies once actually deployed — forcing it during
# local http:// development would silently break every session.
SESSION_COOKIE_SECURE = os.environ.get("FLASK_ENV") == "production"
# Unset locally. In production this must be ".pubpulse.co.uk" and must
# match PricePulse's/TaskPulse's SESSION_COOKIE_DOMAIN exactly.
SESSION_COOKIE_DOMAIN = os.environ.get("SESSION_COOKIE_DOMAIN") or None

# Where an unauthenticated visitor with no shared pub session is sent to log
# into the shared PubPulse account — RotaPulse has no login page of its own
# for the venue-owner path (see app/venues.py). Invited staff/rota_admins
# use RotaPulse's own local login instead (app/rota_login.py).
PRICEPULSE_LOGIN_URL = os.environ.get(
    "PRICEPULSE_LOGIN_URL", "https://pricepulse.pubpulse.co.uk/login"
)

# Authenticates this app's push to the PubPulse Hub whenever a venue's
# Stripe subscription changes, AND TaskPulse's inbound call to this app's
# /internal/clock-status (see app/internal.py). Must be identical across
# all family env vars.
INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET")
PUBPULSE_HUB_URL = os.environ.get("PUBPULSE_HUB_URL", "https://app.pubpulse.co.uk")

# Free trial length (days) for a newly-provisioned venue before a
# subscription is required — matches TaskPulse's own default/reasoning.
ROTAPULSE_TRIAL_DAYS = int(os.environ.get("ROTAPULSE_TRIAL_DAYS", "30"))

# Stripe — same PubPulse Stripe account as the sibling apps, RotaPulse's own
# webhook signing secret (separate registered endpoint) and its own tiered
# Price (billing_scheme=tiered, tiers_mode=volume — see app/billing.py and
# scripts/create_stripe_price.py). All optional at import time: a
# not-yet-configured deployment should still start, just without the
# subscribe button actually working.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_STAFF_TIERED = os.environ.get("STRIPE_PRICE_STAFF_TIERED")  # legacy, retired
# Fixed staff bands (the pick-a-band model): one flat monthly Stripe price per
# band. Index 0..3 line up with STAFF_TIERS' four bands (1-5, 6-10, 11-17,
# 18-25+). Replaces the old STRIPE_PRICE_STAFF_TIERED volume price.
STRIPE_PRICE_ROTA_BANDS = [
    os.environ.get("STRIPE_PRICE_ROTA_BAND1"),
    os.environ.get("STRIPE_PRICE_ROTA_BAND2"),
    os.environ.get("STRIPE_PRICE_ROTA_BAND3"),
    os.environ.get("STRIPE_PRICE_ROTA_BAND4"),
]

# Same Brevo SMTP relay account as the sibling apps — see
# app/notifications.py. Unset MAIL_SERVER means the message is logged
# instead of sent, same dev-fallback pattern as the sibling apps.
MAIL_SERVER = os.environ.get("MAIL_SERVER")
MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
MAIL_FROM = os.environ.get("MAIL_FROM")

# Twilio — new to the PubPulse family (also earmarked for the future
# DiaryPulse app per the spec). Unset means send_sms() logs instead of
# sending, same dev-fallback pattern as MAIL_SERVER.
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")

# Who gets notified when a venue starts a trial or converts to paid — same
# "unset means silently skipped" pattern as the sibling apps.
SUBSCRIBER_NOTIFY_EMAIL = os.environ.get("SUBSCRIBER_NOTIFY_EMAIL")

# Geolocation clock-in/out verification (spec §6.1).
CLOCK_IN_RADIUS_METRES = int(os.environ.get("CLOCK_IN_RADIUS_METRES", "30"))
POSTCODES_IO_URL = os.environ.get("POSTCODES_IO_URL", "https://api.postcodes.io")

# Weather strip (spec §10) — Open-Meteo, free and keyless.
OPEN_METEO_URL = os.environ.get("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast")
WEATHER_CACHE_HOURS = int(os.environ.get("WEATHER_CACHE_HOURS", "6"))

# Invite links (spec §4) expire after this many days.
INVITE_TOKEN_EXPIRY_DAYS = int(os.environ.get("INVITE_TOKEN_EXPIRY_DAYS", "7"))

# Staff-count pricing tiers (spec §9.1) — (max_staff_inclusive, monthly_pence).
# A venue above the top band's max still pays the top band's price (per
# user decision) rather than being blocked from adding more staff.
STAFF_TIERS = [
    (5, 1000),
    (10, 2000),
    (17, 3400),
    (25, 4800),
]


def apply(app):
    app.secret_key = SECRET_KEY
    app.config["SESSION_COOKIE_SAMESITE"] = SESSION_COOKIE_SAMESITE
    app.config["PERMANENT_SESSION_LIFETIME"] = PERMANENT_SESSION_LIFETIME
    app.config["SESSION_REFRESH_EACH_REQUEST"] = SESSION_REFRESH_EACH_REQUEST
    app.config["SESSION_COOKIE_SECURE"] = SESSION_COOKIE_SECURE
    app.config["SESSION_COOKIE_DOMAIN"] = SESSION_COOKIE_DOMAIN
