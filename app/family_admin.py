"""
The venue picker for a family-admin support session (see
app/rota_auth.py's "support_readonly" permission level —
session['pricepulse_admin'], shared across the whole PubPulse app
family). Not slug-scoped, since its whole job is listing every venue
rather than resolving one from the URL — register_identity() never runs
for this blueprint, so the flag is checked directly here.

Also home to the ONE write a support session is allowed to perform: granting
or revoking a complimentary subscription. See comp() for why it lives here
and not in the venue admin area.
"""

import flask

from app.billing import set_comp
from app.db import get_db

family_admin_bp = flask.Blueprint("family_admin", __name__, url_prefix="/admin")


def _require_family_admin():
    if flask.session.get("pricepulse_admin") is not True:
        flask.abort(403)


@family_admin_bp.route("/venues")
def venues():
    _require_family_admin()
    # Joined rather than using db.get_all_venues() because the picker is the
    # only caller that needs each venue's plan state, to show whether it's
    # already comped.
    rows = get_db().execute(
        """SELECT venue.id, venue.name, venue.slug, venue.pub_id,
                  rota_subscription.plan, rota_subscription.subscription_status
           FROM venue
           LEFT JOIN rota_subscription ON rota_subscription.venue_id = venue.id
           ORDER BY venue.name"""
    ).fetchall()
    return flask.render_template("family_admin_venues.html", venues=rows)


@family_admin_bp.route("/venues/<int:venue_id>/comp", methods=["POST"])
def comp(venue_id):
    """Grant or revoke a complimentary subscription — full access with no
    Stripe object behind it at all.

    Deliberately lives in THIS blueprint rather than the venue admin area:
    require_permission("app_admin") also admits the venue OWNER, who must
    never be able to comp themselves. This is the single non-GET a
    family-admin support session may perform; every other one is hard-blocked
    in app/rota_auth.py, which is the real security boundary.

    Comp is how INTERNAL venues are carried — e.g. The Cock, where Enorme Ltd
    is both seller and customer, so a subscription would be the same legal
    person billing itself. Leaving no Stripe object is precisely the point:
    nothing reaches the VAT return, and there are no £0 invoices to explain.

    Note RotaPulse's band machinery is untouched by this: enforce_band()
    no-ops without a stripe_subscription_id, so a comped venue's staff count
    can change freely without any Stripe call.
    """
    _require_family_admin()
    grant = flask.request.form.get("action") == "grant"
    if not set_comp(get_db(), venue_id, grant):
        flask.abort(404)
    flask.flash(
        "Complimentary access granted." if grant else "Complimentary access removed."
    )
    return flask.redirect(flask.url_for("family_admin.venues"))
