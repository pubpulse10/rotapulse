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

# Flask-WTF expires a CSRF token 3600s (one hour) after the page carrying it
# was rendered. That default is simply wrong for this app's central action: a
# staff member opens the shift page to clock in, the phone goes in a pocket
# for the rest of the shift, and the clock-out POST hours later is rejected
# with a bare "Bad Request — The CSRF token has expired". Confirmed live —
# clocked in 08:06, clock-out attempt 10:31, rejected — and it would have hit
# every shift longer than an hour, i.e. nearly all of them.
#
# None means the token instead stays valid for the life of the session. That
# gives up almost nothing: validate_csrf only ever accepts a token that
# matches session['csrf_token'], so the token is useless to anyone who
# doesn't also hold the session cookie — and that cookie is itself a rolling
# 30 days (above). The one-hour limit was bounding a window the session
# lifetime already bounds; all it actually bounded was how long a landlord's
# staff could stay clocked in.
WTF_CSRF_TIME_LIMIT = None

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
    # Set before CSRFProtect(app), whose init_app only setdefault()s this key,
    # so an explicit None here survives rather than being replaced by 3600.
    app.config["WTF_CSRF_TIME_LIMIT"] = WTF_CSRF_TIME_LIMIT


def pin_https_scheme(app):
    """Guarantee url_for(_external=True) emits https in production.

    Must be called AFTER ProxyFix is installed — it wraps whatever is already
    on app.wsgi_app.

    Every external URL this app hands out is built by url_for(_external=True),
    which takes its scheme from request.scheme, which ProxyFix derives from
    X-Forwarded-Proto. But in production ProxyFix never sees that header:
    waitress defaults to clear_untrusted_proxy_headers=True with trusted_proxy
    unset, so it STRIPS every X-Forwarded-* header before calling the WSGI
    app. ProxyFix is left with nothing to read and the scheme stays http.

    Confirmed against a real waitress 3.0.2 server rather than inferred: a
    request sent with "X-Forwarded-Proto: https" reaches the app as None.
    Flask's test client can't show this — it has no server layer, which is
    why the wiring looked correct in every local test.

    The cost was Stripe Checkout sessions created with http success_url /
    cancel_url, plus every staff invite link, password-reset link and
    open-shift claim URL texted to staff — all carrying single-use tokens
    over cleartext.

    Same stripping is what broke rate-limit keying: X-Forwarded-For was being
    removed too, so get_remote_address() returned the proxy and per-IP limits
    never tripped. The CF-Connecting-IP workaround in app/extensions.py works
    precisely because waitress clears only the standard X-Forwarded-* set, not
    Cloudflare's own header. (DiaryPulse was never affected by any of this —
    it runs gunicorn, which passes the headers through. That, not anything in
    the app code, is the entire difference between the four apps.)

    Fixing it at the waitress layer would mean setting --trusted-proxy, but
    Render's internal proxy address isn't stable enough to pin to. Injecting
    the header here is independent of that: at the edge production is
    https-only, so http is never the right answer. Side benefit — a client
    can no longer send "X-Forwarded-Proto: http" to downgrade a generated link.

    Left off outside production so local http:// development is unaffected.
    Same helper in PricePulse/TaskPulse — change one, change all.
    """
    if os.environ.get("FLASK_ENV") != "production":
        return

    # Covers url_for(_external=True) called with no request context (e.g. the
    # shift-notification cron entrypoint), where there is no header to read.
    app.config["PREFERRED_URL_SCHEME"] = "https"

    proxied = app.wsgi_app

    def force_https(environ, start_response):
        environ["HTTP_X_FORWARDED_PROTO"] = "https"
        return proxied(environ, start_response)

    app.wsgi_app = force_https
