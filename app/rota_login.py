"""
Local password login for anyone invited into RotaPulse (staff, and any
delegated rota_admin who isn't the venue owner) — the venue owner never
uses this, they're recognised via the shared PubPulse cookie instead (see
app/venues.py and app/rota_auth.py).

Login is password-based, not passwordless OTP, per spec §4: the identifier
field uses autocomplete="username" and the password field
autocomplete="current-password" so device/browser password managers offer
to save and auto-fill correctly. The long PERMANENT_SESSION_LIFETIME
(app/config.py) is what makes this "frictionless" day-to-day — see that
module's docstring for why no separate refresh-token table exists.

Password reset mirrors pubpulse/app/auth.py's forgot_password/reset_password
near-verbatim: same SHA-256-of-random-token lookup (a reset token is high-
entropy randomness, not a low-entropy password, so a fast deterministic hash
is fine and is what makes a direct WHERE token_hash = ? lookup possible at
all), same non-enumeration messaging, same 1-hour expiry.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import flask
from werkzeug.security import check_password_hash, generate_password_hash

from app import config
from app.db import get_db
from app.extensions import limiter
from app.notifications import send_email, send_sms
from app.venue_scope import register_venue_gate, register_venue_scope

login_bp = flask.Blueprint("rota_login", __name__, url_prefix="/v/<slug>")
register_venue_scope(login_bp)
register_venue_gate(login_bp)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _safe_next(target: str | None) -> bool:
    """Only accept a root-relative local path — one that starts with a single
    '/', isn't protocol-relative ('//...') or a '/\\...' variant browsers
    normalise to that, and carries no scheme. Blocks open-redirect abuse of
    the ?next= parameter to an attacker-controlled absolute URL."""
    if not target or not target.startswith("/"):
        return False
    if target.startswith("//") or target.startswith("/\\"):
        return False
    return True


def _find_person_by_identifier(db, venue_id, identifier):
    return db.execute(
        """SELECT person.* FROM person
           JOIN venue_membership ON venue_membership.person_id = person.id
           WHERE venue_membership.venue_id = ? AND venue_membership.status = 'active'
           AND (person.email = ? OR person.mobile = ?)
           AND person.password_hash IS NOT NULL""",
        (venue_id, identifier, identifier),
    ).fetchone()


@login_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 100 per hour", methods=["POST"])
def login():
    venue = flask.g.venue
    if flask.request.method == "POST":
        identifier = flask.request.form.get("identifier", "").strip().lower()
        password = flask.request.form.get("password", "")
        db = get_db()
        person = _find_person_by_identifier(db, venue["id"], identifier)
        if person and check_password_hash(person["password_hash"], password):
            flask.session.permanent = True
            flask.session["rotapulse_person_id"] = person["id"]
            next_param = flask.request.args.get("next")
            dest = next_param if _safe_next(next_param) else flask.url_for("staff_portal.home", slug=venue["slug"])
            resp = flask.redirect(dest)
            # Remembers which venue's LOCAL login to send this device back to
            # from the bare "/" entry point (the PWA's start_url) if the
            # session itself is ever gone by then — a plain cookie, not tied
            # to session validity, so it survives a session expiring/being
            # cleared even though the login itself would need to happen
            # again. Without this, a fully logged-out visit to "/" has no
            # slug to work with and falls back to the owner-only PricePulse
            # login, which is wrong for staff (see app/venues.py::entry).
            resp.set_cookie(
                "rotapulse_slug", venue["slug"], max_age=60 * 60 * 24 * 365,
                httponly=True, secure=config.SESSION_COOKIE_SECURE, samesite="Lax",
            )
            return resp
        flask.flash("Invalid email/mobile or password.", "error")
    return flask.render_template("login/login.html", venue=venue)


@login_bp.route("/logout", methods=["POST"])
def logout():
    flask.session.pop("rotapulse_person_id", None)
    return flask.redirect(flask.url_for("rota_login.login", slug=flask.g.slug))


@login_bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per minute; 30 per hour", methods=["POST"])
def forgot_password():
    venue = flask.g.venue
    if flask.request.method == "POST":
        identifier = flask.request.form.get("identifier", "").strip().lower()
        db = get_db()
        person = _find_person_by_identifier(db, venue["id"], identifier)
        if person and (person["email"] or person["mobile"]):
            raw_token = secrets.token_urlsafe(32)
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            db.execute(
                "INSERT INTO rota_password_reset_token (person_id, token_hash, expires_at) VALUES (?, ?, ?)",
                (person["id"], _hash_token(raw_token), expires_at),
            )
            db.commit()
            reset_url = flask.url_for("rota_login.reset_password", slug=venue["slug"], token=raw_token, _external=True)
            # Real report, 2026-08-19: someone invited by SMS with no email
            # ever captured had no self-service reset path at all -- this
            # was email-only, so the flash message below said "we've sent a
            # reset link" while silently sending nothing. SMS fallback only
            # (not both) when there's no email, mirroring how login already
            # accepts either identifier -- whichever contact detail actually
            # exists is the one that gets used.
            if person["email"]:
                send_email(
                    person["email"],
                    "Reset your RotaPulse password",
                    "Click the link below to set a new password. This link expires in "
                    f"1 hour and can only be used once.\n\n{reset_url}",
                )
            else:
                send_sms(
                    person["mobile"],
                    f"RotaPulse password reset: {reset_url} (expires in 1 hour, one-time use).",
                )
        flask.flash("If an account exists for that email or mobile, we've sent a reset link.")
        return flask.redirect(flask.url_for("rota_login.login", slug=venue["slug"]))
    return flask.render_template("login/forgot_password.html", venue=venue)


@login_bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("5 per minute; 30 per hour", methods=["POST"])
def reset_password(token):
    venue = flask.g.venue
    db = get_db()
    token_row = db.execute(
        "SELECT * FROM rota_password_reset_token WHERE token_hash = ?", (_hash_token(token),)
    ).fetchone()
    now_iso = datetime.now(timezone.utc).isoformat()
    valid = token_row is not None and token_row["used_at"] is None and token_row["expires_at"] > now_iso

    if not valid:
        flask.flash("That reset link is invalid or has expired — request a new one.", "error")
        return flask.redirect(flask.url_for("rota_login.forgot_password", slug=venue["slug"]))

    if flask.request.method == "POST":
        password = flask.request.form.get("password", "")
        confirm_password = flask.request.form.get("confirm_password", "")
        if len(password) < 8:
            flask.flash("Password must be at least 8 characters.", "error")
            return flask.render_template("login/reset_password.html", venue=venue, token=token)
        if password != confirm_password:
            flask.flash("Passwords don't match.", "error")
            return flask.render_template("login/reset_password.html", venue=venue, token=token)

        db.execute(
            "UPDATE person SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), token_row["person_id"]),
        )
        db.execute("UPDATE rota_password_reset_token SET used_at = ? WHERE id = ?", (now_iso, token_row["id"]))
        db.commit()
        flask.flash("Password updated — log in below.")
        return flask.redirect(flask.url_for("rota_login.login", slug=venue["slug"]))

    return flask.render_template("login/reset_password.html", venue=venue, token=token)
