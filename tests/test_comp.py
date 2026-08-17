"""
Complimentary subscriptions: full access with no Stripe object behind it.

How INTERNAL venues are carried — e.g. The Cock, where Enorme Ltd is both
seller and customer, so a real subscription would be the same legal person
billing itself. Leaving no Stripe object is the point: nothing reaches the VAT
return and there are no £0 invoices to explain.

The security shape is the part worth guarding: comp is a WRITE performed by a
family-admin support session, whose every other non-GET is hard-blocked. It
must be reachable by that session and by nobody else — least of all the venue
owner, who would otherwise comp themselves.
"""

from app import db as db_module
from app.billing import current_venue_plan
from tests.conftest import login_as_pub


def _login_as_family_admin(client):
    with client.session_transaction() as sess:
        sess["pricepulse_admin"] = True


def _sub(app, venue_id):
    with app.app_context():
        return db_module.get_db().execute(
            "SELECT plan, subscription_status FROM rota_subscription WHERE venue_id = ?",
            (venue_id,),
        ).fetchone()


def test_owner_cannot_comp_their_own_venue(app, client, venue):
    """The whole reason comp lives in the family-admin blueprint rather than
    the venue admin area: admin_required also admits the owner."""
    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/admin/venues/{venue['id']}/comp", data={"action": "grant"})
    assert resp.status_code == 403


def test_anonymous_cannot_comp(app, client, venue):
    resp = client.post(f"/admin/venues/{venue['id']}/comp", data={"action": "grant"})
    assert resp.status_code == 403


def test_family_admin_can_grant_comp(app, client, venue):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_subscription SET plan = 'inactive' WHERE venue_id = ?",
            (venue["id"],),
        )
        conn.commit()

    _login_as_family_admin(client)
    resp = client.post(f"/admin/venues/{venue['id']}/comp", data={"action": "grant"})
    assert resp.status_code == 302

    row = _sub(app, venue["id"])
    assert row["plan"] == "active"
    assert row["subscription_status"] == "comp"
    with app.app_context():
        assert current_venue_plan(venue["id"]) == "active"


def test_comp_survives_a_stray_subscription_deleted_webhook(app, client, venue, monkeypatch):
    """A comped venue has no Stripe subscription, so nothing should ever be
    able to revoke it. current_venue_plan checks comp ahead of plan precisely
    so a stray event can't take a venue's access away."""
    _login_as_family_admin(client)
    client.post(f"/admin/venues/{venue['id']}/comp", data={"action": "grant"})

    with app.app_context():
        conn = db_module.get_db()
        # Simulate a webhook having flipped plan to inactive underneath.
        conn.execute(
            "UPDATE rota_subscription SET plan = 'inactive' WHERE venue_id = ?",
            (venue["id"],),
        )
        conn.commit()
        assert current_venue_plan(venue["id"]) == "active"


def test_revoking_comp_without_stripe_removes_access(app, client, venue):
    _login_as_family_admin(client)
    client.post(f"/admin/venues/{venue['id']}/comp", data={"action": "grant"})
    client.post(f"/admin/venues/{venue['id']}/comp", data={"action": "revoke"})

    row = _sub(app, venue["id"])
    assert row["plan"] == "inactive"
    assert row["subscription_status"] is None


def test_revoking_comp_does_not_cancel_a_real_subscription(app, client, venue):
    """If a paid subscription is underneath, revoking the comp must only drop
    the marker — Stripe's webhook owns the plan, and removing a courtesy must
    never cut off access someone is actually paying for."""
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_subscription SET stripe_subscription_id = 'sub_paid' "
            "WHERE venue_id = ?",
            (venue["id"],),
        )
        conn.commit()

    _login_as_family_admin(client)
    client.post(f"/admin/venues/{venue['id']}/comp", data={"action": "grant"})
    client.post(f"/admin/venues/{venue['id']}/comp", data={"action": "revoke"})

    row = _sub(app, venue["id"])
    assert row["subscription_status"] is None
    assert row["plan"] == "active"  # paid access retained


def test_comped_venue_reads_as_free_on_the_subscription_page(app, client, venue):
    """No Stripe object means no invoice preview, so the page must know a comp
    is free rather than falling through to "No payment scheduled"."""
    from app.billing import subscription_summary

    summary = subscription_summary("comp", None, None)
    assert summary["is_free"] is True
    assert summary["status_label"] == "Complimentary — no charge"
