"""
Forwarding the "Help & feedback" widget's submissions to the Hub.

The widget itself is served by the Hub (`/static/help-widget.js`) so that one
copy serves all five apps. It posts HERE rather than to the Hub directly —
pubpulse-hub `docs/decisions.md` D3: going through the app it is embedded in
reuses this app's own CSRF token and the existing INTERNAL_API_SECRET, and
leaves the identity hub with no cross-origin write surface at all.

**This route's whole job is to say who is submitting.** The browser sends the
message; the pub, the email and the name are filled in here from the session.
A pub_id trusted from a form would let any signed-in customer file feedback as
any other — and the Hub's "Your conversations" page keys off exactly those
values, so it would be a way to read a stranger's support replies too.

It validates nothing else. Lengths, enums and the screenshot are the Hub's
rules, checked in the Hub; a second copy here is a second thing to drift.

The integration recipe is pubpulse-hub `docs/help-widget-integration.md`.
"""

import flask
import requests

from app import config
from app.extensions import limiter
from app.rota_auth import register_identity
from app.venue_scope import register_venue_scope

help_feedback_bp = flask.Blueprint(
    "help_feedback", __name__, url_prefix="/v/<slug>/help"
)
register_venue_scope(help_feedback_bp)
register_identity(help_feedback_bp)

# NO register_venue_gate, deliberately. That gate locks a whole venue when the
# subscription is not active, and the billing blueprint is already exempt from
# it for the same reason this one is: someone who has just been locked out is
# exactly the person who needs to ask why. A help form that stops working when
# there is a problem is a help form that is missing when it matters.

# What the widget is allowed to send. A whitelist, so a future version of the
# widget cannot start posting a field this app forwards blindly.
FORWARDED = ("type", "title", "body", "route", "app_version", "context")

HUB_TIMEOUT = 15


@help_feedback_bp.route("/feedback", methods=["POST"])
@limiter.limit("20 per hour")
def submit():
    """Take the widget's multipart form, add who it is from, hand it to the Hub.

    Still behind this app's CSRF protection — it is a browser POST on our own
    origin, so it is an ordinary CSRF target, and it must never end up on the
    exempt blueprint with /internal. Flask-WTF takes the token from the
    `csrf_token` field or the `X-CSRFToken` header; the widget sends both.
    """
    person = flask.g.get("person")
    if person is None:
        # Includes a support-readonly session, which has no person row: a
        # PubPulse support session must not be able to file feedback as the
        # customer whose account it is looking at.
        return {"error": "not signed in"}, 401

    venue = flask.g.get("venue")
    # The owner person carries pub_id; an invited staff member does not, so
    # fall back to the venue's copy of it and then to the shared family cookie.
    pub_id = person["pub_id"] or (venue and venue["pub_id"]) or flask.session.get("pub_id")
    if not pub_id:
        return {"error": "This venue isn't linked to a PubPulse account yet."}, 400

    email = person["email"] or flask.session.get("landlord_email")
    if not email:
        # Everything about Help & feedback is answered by email, so there is
        # no point accepting something we could never reply to.
        return {
            "error": "Add an email address to your profile first — "
                     "that's where the answer goes."
        }, 400

    data = {name: flask.request.form.get(name, "") for name in FORWARDED}
    data["app"] = "rotapulse"
    data["pub_id"] = pub_id
    data["user_email"] = email
    data["user_name"] = person["name"]
    data["app_user_ref"] = person["id"]

    # Streamed straight through, never saved here and never re-encoded: the
    # Hub sniffs the magic bytes, bounds the dimensions and re-encodes it
    # (pubpulse-hub D11). An app that kept its own copy would have created a
    # second store of customer screenshots with none of that done to them.
    files = {}
    screenshot = flask.request.files.get("screenshot")
    if screenshot and screenshot.filename:
        files["screenshot"] = (
            screenshot.filename, screenshot.stream, screenshot.mimetype,
        )

    try:
        response = requests.post(
            f"{config.PUBPULSE_HUB_URL}/internal/feedback",
            data=data, files=files,
            headers={"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"},
            timeout=HUB_TIMEOUT,
        )
    except requests.RequestException:
        return {"error": "Couldn't reach PubPulse just now — please try again "
                         "in a moment."}, 502

    # The Hub's answer, passed back untouched: the widget shows errors[0] to
    # the customer, and re-wording them here would mean two places deciding
    # what a too-long title is called.
    try:
        return response.json(), response.status_code
    except ValueError:
        return {"error": "Couldn't reach PubPulse just now — please try again "
                         "in a moment."}, 502
