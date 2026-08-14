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
    if pub_id is not None:
        venue = _venue_for_pub(get_db(), pub_id)
        if venue:
            return flask.redirect(flask.url_for("rota_grid.week", slug=venue["slug"]))
        return flask.redirect(flask.url_for("venues.setup"))

    # This is also the PWA's start_url — what a home-screen icon opens with
    # no venue slug in the URL to work from. A staff member never has
    # session['pub_id'] (they log in locally, not via PricePulse — see
    # app/rota_login.py), so without this check they'd always fall through
    # to the owner-only PricePulse login below, which is wrong for them and
    # actively confusing (real report, 2026-08-13). If they're still
    # logged in locally, resolve their venue directly from their own
    # session instead.
    person_id = flask.session.get("rotapulse_person_id")
    if person_id is not None:
        membership = get_db().execute(
            """SELECT venue.slug FROM venue_membership
               JOIN venue ON venue.id = venue_membership.venue_id
               WHERE venue_membership.person_id = ? AND venue_membership.status = 'active'
               ORDER BY venue_membership.id LIMIT 1""",
            (person_id,),
        ).fetchone()
        if membership:
            return flask.redirect(flask.url_for("staff_portal.home", slug=membership["slug"]))

    return _login_redirect()


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

        # New venues start LOCKED (no cardless trial) — the free trial is now
        # Stripe-managed and only starts once the owner completes Checkout with
        # a card. current_venue_plan() reads this as 'inactive' until then.
        db.execute(
            "INSERT INTO rota_subscription (venue_id, plan, trial_ends_at) VALUES (?, 'inactive', NULL)",
            (venue_id,),
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

        subscribe_url = flask.url_for("billing.subscription", slug=slug, _external=True)
        if landlord_email:
            send_email(
                landlord_email,
                "Welcome to RotaPulse — subscribe to start your free trial",
                f"You've set up {venue_name} on RotaPulse.\n\n"
                f"To start using it, subscribe to begin your {config.ROTAPULSE_TRIAL_DAYS}-day "
                "free trial. You'll add a card but won't be charged until the trial ends, and you "
                f"can cancel any time before then:\n{subscribe_url}\n",
            )
        if config.SUBSCRIBER_NOTIFY_EMAIL:
            send_email(
                config.SUBSCRIBER_NOTIFY_EMAIL,
                f"New RotaPulse venue (awaiting subscription): {venue_name}",
                f"Venue: {venue_name}\nLandlord email: {landlord_email or '(not available)'}\n"
                "Status: created, not yet subscribed",
            )

        flask.flash(
            f"Almost there — subscribe to start your {config.ROTAPULSE_TRIAL_DAYS}-day free trial "
            "and unlock RotaPulse."
        )
        return flask.redirect(flask.url_for("billing.subscription", slug=slug))

    return flask.render_template("venues/setup.html", trial_days=config.ROTAPULSE_TRIAL_DAYS)
