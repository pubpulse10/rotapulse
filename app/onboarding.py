"""
Stages 3-4 of the six-stage onboarding flow (spec §4): the invited person
opens their unique link, completes their own profile, and sets a password.
Stage 1 (admin creates the record + invite) and stage 2 (invite sent) live
in app/admin_config.py, since only an admin can trigger them.

Token-authenticated, not session-authenticated — the person doesn't have an
account yet. Uses the same SHA-256-of-random-token lookup idiom as
app/rota_login.py's password reset.

Sensitive-data consent (spec §3's own instruction) is a standalone checkbox
here, separate from any general terms acknowledgement — ticking it is what
enables home_address/avatar/photo-capture for this person at all. Declining
it still completes onboarding (name/availability only), consistent with
data minimisation rather than coercing consent to proceed.
"""

import hashlib
import json

import flask
from werkzeug.security import generate_password_hash

from app.db import get_db
from app.media import save_avatar
from app.notifications import send_email
from app.roles import active_roles
from app.venue_scope import register_venue_scope

onboard_bp = flask.Blueprint("onboarding", __name__, url_prefix="/v/<slug>/onboard")
register_venue_scope(onboard_bp)

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _tell_admins_someone_is_waiting(db, venue, staff_name):
    """Email the venue's admins that someone is sitting in the approval
    queue. Real report, 2026-09-21 (The Queens Head): staff completed their
    invites and nothing told the owner they were waiting, so they sat there
    unable to log in and it looked like the accounts were broken.

    Deliberately NOT through notification_settings.notify_admins(): that is
    the owner-configurable monitoring system, and it is silent unless the
    venue has already enabled that event type AND picked recipients — which
    a venue setting itself up for the first time has not. This one has to
    arrive by default, like the invite itself. Best-effort: a failed send
    must never break the staff member's onboarding (send_email already
    swallows its own errors and returns False)."""
    from app.admin_config import _eligible_notification_recipients

    approvals_url = flask.url_for("admin_config.pending_approval", slug=venue["slug"], _external=True)
    for admin in _eligible_notification_recipients(db, venue["id"]):
        if not admin["email"]:
            continue
        send_email(
            admin["email"],
            f"{staff_name} is waiting for approval on RotaPulse",
            f"{staff_name} has finished setting up their RotaPulse account at {venue['name']}.\n\n"
            f"They can't log in until you approve them:\n{approvals_url}\n",
        )


def _find_access_by_token(db, token):
    return db.execute(
        "SELECT * FROM app_access WHERE invite_token_hash = ?", (_hash_token(token),)
    ).fetchone()


@onboard_bp.route("/<token>", methods=["GET", "POST"])
def accept(token):
    venue = flask.g.venue
    db = get_db()
    access = _find_access_by_token(db, token)

    import datetime as _dt

    valid = (
        access is not None
        and access["status"] == "invited"
        and access["invite_expires_at"] > _dt.datetime.now(_dt.timezone.utc).isoformat()
    )
    if not valid:
        flask.flash("That invite link is invalid or has expired — ask your admin to resend it.", "error")
        return flask.redirect(flask.url_for("rota_login.login", slug=venue["slug"]))

    membership = db.execute(
        "SELECT * FROM venue_membership WHERE id = ?", (access["venue_membership_id"],)
    ).fetchone()
    person = db.execute("SELECT * FROM person WHERE id = ?", (membership["person_id"],)).fetchone()
    roles = active_roles(db, venue["id"], include_ids=[membership["job_role_id"]])

    if flask.request.method == "POST":
        form = flask.request.form
        password = form.get("password", "")
        confirm_password = form.get("confirm_password", "")
        consent = bool(form.get("consent"))
        home_address = form.get("home_address", "").strip()
        availability = {day: bool(form.get(f"available_{day}")) for day in DAYS}

        if len(password) < 8:
            flask.flash("Password must be at least 8 characters.", "error")
            return flask.render_template("onboard/accept.html", venue=venue, person=person, roles=roles, days=DAYS)
        if password != confirm_password:
            flask.flash("Passwords don't match.", "error")
            return flask.render_template("onboard/accept.html", venue=venue, person=person, roles=roles, days=DAYS)

        avatar_url = None
        avatar_file = flask.request.files.get("avatar")
        if consent and avatar_file and avatar_file.filename:
            avatar_url = save_avatar(avatar_file)

        db.execute(
            """UPDATE person SET password_hash = ?,
               consent_given_at = CASE WHEN ? THEN datetime('now') ELSE NULL END,
               avatar_url = COALESCE(?, avatar_url)
               WHERE id = ?""",
            (generate_password_hash(password), consent, avatar_url, person["id"]),
        )
        db.execute(
            """INSERT INTO rota_staff_detail (venue_membership_id, home_address, availability)
               VALUES (?, ?, ?)
               ON CONFLICT(venue_membership_id) DO UPDATE SET home_address = excluded.home_address,
               availability = excluded.availability""",
            (membership["id"], home_address if consent else None, json.dumps(availability)),
        )
        db.execute(
            "UPDATE app_access SET status = 'pending_approval', accepted_at = datetime('now') WHERE id = ?",
            (access["id"],),
        )
        db.commit()
        _tell_admins_someone_is_waiting(db, venue, person["name"])

        flask.flash(
            "Profile complete — an admin needs to approve your account before you can log in."
        )
        return flask.redirect(flask.url_for("rota_login.login", slug=venue["slug"]))

    return flask.render_template("onboard/accept.html", venue=venue, person=person, roles=roles, days=DAYS)
