"""
Identity resolution for RotaPulse's two login paths (see module docstring
in app/venues.py for the full reasoning):

- The venue owner (app_admin + rota_admin) is recognised purely from the
  shared PubPulse session cookie (session['pub_id']) joined through
  person.pub_id — no local password, ever, for this path.
- Anyone invited (staff, or a delegated rota_admin who isn't the owner)
  is recognised from RotaPulse's own local session key,
  session['rotapulse_person_id'], set at login (app/rota_login.py).

register_identity() populates g.person / g.membership / g.permission_levels
on every request for a slug-scoped blueprint (after venue_scope has already
resolved g.venue). require_permission(*levels) is the gate individual
routes use.
"""

from functools import wraps

import flask

from app.db import get_db


def register_identity(blueprint):
    @blueprint.before_request
    def _resolve_identity():
        flask.g.person = None
        flask.g.membership = None
        flask.g.permission_levels = set()

        if flask.session.get("pricepulse_admin") is True:
            # Family-admin support session (see app/family_admin.py) — set by
            # PricePulse's own /admin/login and readable here because all
            # four PubPulse apps share one session cookie. Doesn't
            # correspond to a real person/venue_membership row, so
            # g.person/g.membership stay None; require_permission grants
            # this a read-only bypass on admin-tier routes only.
            flask.g.permission_levels = {"support_readonly"}
            return

        venue = flask.g.get("venue")
        if venue is None:
            return

        db = get_db()
        person = None

        person_id = flask.session.get("rotapulse_person_id")
        if person_id:
            person = db.execute("SELECT * FROM person WHERE id = ?", (person_id,)).fetchone()

        if person is None and flask.session.get("pub_id") is not None:
            person = db.execute(
                """SELECT person.* FROM person
                   JOIN venue_membership ON venue_membership.person_id = person.id
                   WHERE person.pub_id = ? AND venue_membership.venue_id = ?""",
                (flask.session["pub_id"], venue["id"]),
            ).fetchone()

        if person is None:
            return

        membership = db.execute(
            "SELECT * FROM venue_membership WHERE person_id = ? AND venue_id = ? AND status = 'active'",
            (person["id"], venue["id"]),
        ).fetchone()
        if membership is None:
            return

        flask.g.person = person
        flask.g.membership = membership
        flask.g.permission_levels = {
            row["permission_level"]
            for row in db.execute(
                """SELECT permission_level FROM app_access
                   WHERE venue_membership_id = ? AND status = 'active'
                   AND app_id = (SELECT id FROM app WHERE key = 'rotapulse')""",
                (membership["id"],),
            ).fetchall()
        }


def require_permission(*levels):
    """Gates a view to one or more permission_level values. Redirects to
    the appropriate login depending on which tiers are acceptable here:
    app_admin-only routes (venue configuration) go to the shared PubPulse
    login since that's an owner-only path in V1; anything that accepts
    rota_admin or staff goes to RotaPulse's own local login."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not flask.g.permission_levels & set(levels):
                if (
                    "support_readonly" in flask.g.permission_levels
                    and {"app_admin", "rota_admin"} & set(levels)
                    and "staff" not in levels
                ):
                    # Family-admin read-only bypass — GET only, hard-blocked
                    # otherwise. Excludes any route that also accepts
                    # "staff", since those (e.g. staff_portal.py) dereference
                    # g.person unguarded and support sessions have none.
                    if flask.request.method != "GET":
                        flask.abort(403)
                    return view(*args, **kwargs)
                if "app_admin" in levels and len(levels) == 1:
                    from app import config

                    return flask.redirect(f"{config.PRICEPULSE_LOGIN_URL}?next={flask.request.url}")
                return flask.redirect(flask.url_for("rota_login.login", next=flask.request.path))
            return view(*args, **kwargs)

        return wrapped

    return decorator
