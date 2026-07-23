"""
Venue provisioning + the shared-identity entry point for the venue OWNER.

RotaPulse has no login page of its own for this path — a visitor's identity
comes entirely from the shared PubPulse session cookie (session['pub_id'],
set by PricePulse's login/register). This module is where that identity
turns into "which venue do I land on": an existing one for this pub_id, or
a form to create one.

Unlike TaskPulse, setting up a venue here also creates the owner's own
PERSON/VENUE_MEMBERSHIP/APP_ACCESS rows (both app_admin and rota_admin,
status='active', no invite/approval loop — the owner IS the approver). This
person's password_hash stays NULL forever; they're recognised only via the
shared cookie (person.pub_id anchor), never via RotaPulse's own local login.
Invited staff and delegated rota_admins use that local login instead — see
app/onboarding.py and app/rota_login.py.

V1 scope: one venue per subscribing pub (matches TaskPulse's own pattern) —
the schema doesn't enforce that (no UNIQUE on venue.pub_id), so supporting
more than one later is an application-logic change here, not a migration.
"""

import re
from datetime import date, timedelta

import flask

from app import config
from app.date_format import format_uk_date
from app.db import get_app_id, get_db
from app.geocoding import geocode_postcode
from app.notifications import send_email

venues_bp = flask.Blueprint("venues", __name__)


def _login_redirect():
    return flask.redirect(f"{config.PRICEPULSE_LOGIN_URL}?next={flask.request.url}")


def _venue_for_pub(db, pub_id):
    return db.execute("SELECT * FROM venue WHERE pub_id = ? ORDER BY id LIMIT 1", (pub_id,)).fetchone()


def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "venue"


def _unique_slug(db, base_slug):
    slug = base_slug
    suffix = 1
    while db.execute("SELECT 1 FROM venue WHERE slug = ?", (slug,)).fetchone():
        suffix += 1
        slug = f"{base_slug}-{suffix}"
    return slug


@venues_bp.route("/")
def entry():
    pub_id = flask.session.get("pub_id")
    if pub_id is None:
        return _login_redirect()

    venue = _venue_for_pub(get_db(), pub_id)
    if venue:
        return flask.redirect(flask.url_for("rota_grid.week", slug=venue["slug"]))
    return flask.redirect(flask.url_for("venues.setup"))


@venues_bp.route("/setup", methods=["GET", "POST"])
def setup():
    pub_id = flask.session.get("pub_id")
    if pub_id is None:
        return _login_redirect()

    db = get_db()
    existing = _venue_for_pub(db, pub_id)
    if existing:
        return flask.redirect(flask.url_for("rota_grid.week", slug=existing["slug"]))

    if flask.request.method == "POST":
        form = flask.request.form
        venue_name = form.get("venue_name", "").strip()
        owner_name = form.get("owner_name", "").strip()
        postcode = form.get("postcode", "").strip()
        if not venue_name:
            flask.flash("Enter your venue's name.", "error")
            return flask.render_template("venues/setup.html", trial_days=config.ROTAPULSE_TRIAL_DAYS)
        if not owner_name:
            flask.flash("Enter your own name.", "error")
            return flask.render_template("venues/setup.html", trial_days=config.ROTAPULSE_TRIAL_DAYS)

        coords = geocode_postcode(postcode) if postcode else None
        slug = _unique_slug(db, _slugify(venue_name))

        cur = db.execute(
            "INSERT INTO venue (pub_id, name, postcode, latitude, longitude, slug) VALUES (?, ?, ?, ?, ?, ?)",
            (pub_id, venue_name, postcode or None, coords[0] if coords else None, coords[1] if coords else None, slug),
        )
        venue_id = cur.lastrowid
        db.execute("INSERT INTO venue_settings (venue_id) VALUES (?)", (venue_id,))

        trial_ends_at = (date.today() + timedelta(days=config.ROTAPULSE_TRIAL_DAYS)).isoformat()
        db.execute(
            "INSERT INTO rota_subscription (venue_id, plan, trial_ends_at) VALUES (?, 'inactive', ?)",
            (venue_id, trial_ends_at),
        )

        landlord_email = flask.session.get("landlord_email")
        owner_cur = db.execute(
            "INSERT INTO person (name, email, pub_id) VALUES (?, ?, ?)",
            (owner_name, landlord_email, pub_id),
        )
        person_id = owner_cur.lastrowid
        membership_cur = db.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')",
            (person_id, venue_id),
        )
        membership_id = membership_cur.lastrowid
        app_id = get_app_id(db, "rotapulse")
        for level in ("app_admin", "rota_admin"):
            db.execute(
                """INSERT INTO app_access
                   (venue_membership_id, app_id, permission_level, status, accepted_at, approved_at)
                   VALUES (?, ?, ?, 'active', datetime('now'), datetime('now'))""",
                (membership_id, app_id, level),
            )
        db.commit()

        if landlord_email:
            send_email(
                landlord_email,
                "Welcome to RotaPulse — your free trial has started",
                f"You've set up {venue_name} on RotaPulse.\n\n"
                f"You're on a {config.ROTAPULSE_TRIAL_DAYS}-day free trial, no card needed yet. "
                f"Your trial ends on {format_uk_date(trial_ends_at)}.\n\n"
                "Before then, subscribe to keep using RotaPulse without a gap — you can do that "
                f"any time from your subscription page.",
            )
        if config.SUBSCRIBER_NOTIFY_EMAIL:
            send_email(
                config.SUBSCRIBER_NOTIFY_EMAIL,
                f"New RotaPulse trial: {venue_name}",
                f"Venue: {venue_name}\nLandlord email: {landlord_email or '(not available)'}\n"
                f"Trial ends: {format_uk_date(trial_ends_at)}",
            )

        flask.flash(
            f"Welcome to RotaPulse — you have {config.ROTAPULSE_TRIAL_DAYS} free days "
            "before a subscription is needed."
        )
        return flask.redirect(flask.url_for("rota_grid.week", slug=slug))

    return flask.render_template("venues/setup.html", trial_days=config.ROTAPULSE_TRIAL_DAYS)
