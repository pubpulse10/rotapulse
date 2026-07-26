"""
The venue picker for a family-admin support session (see
app/rota_auth.py's "support_readonly" permission level —
session['pricepulse_admin'], shared across the whole PubPulse app
family). Not slug-scoped, since its whole job is listing every venue
rather than resolving one from the URL — register_identity() never runs
for this blueprint, so the flag is checked directly here.
"""

import flask

from app.db import get_all_venues, get_db

family_admin_bp = flask.Blueprint("family_admin", __name__, url_prefix="/admin")


@family_admin_bp.route("/venues")
def venues():
    if flask.session.get("pricepulse_admin") is not True:
        flask.abort(403)
    return flask.render_template("family_admin_venues.html", venues=get_all_venues(get_db()))
