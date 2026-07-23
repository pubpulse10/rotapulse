from types import SimpleNamespace

from app import db as db_module
from app.billing import count_billable_staff, tier_for_count
from tests.conftest import create_active_staff


def test_tier_for_count_bands():
    assert tier_for_count(1) == 1
    assert tier_for_count(5) == 1
    assert tier_for_count(6) == 2
    assert tier_for_count(10) == 2
    assert tier_for_count(11) == 3
    assert tier_for_count(17) == 3
    assert tier_for_count(18) == 4
    assert tier_for_count(25) == 4


def test_tier_caps_at_top_band_above_25():
    assert tier_for_count(40) == 4


def test_count_billable_staff_excludes_admin_only_accounts(app, venue):
    create_active_staff(app, venue["id"], name="Real Staff")
    # The venue fixture's owner has app_admin+rota_admin but no
    # rota_staff_detail row — must not count toward billing.
    with app.app_context():
        assert count_billable_staff(venue["id"]) == 1


def test_webhook_checkout_completed_uses_getattr_not_get(app, client, venue, monkeypatch):
    """Regression test for the exact bug class that silently broke both
    sibling apps' webhooks: a Stripe SDK event object only supports
    attribute/[] access, never .get(). Mocking with SimpleNamespace (not a
    dict) means this test would fail the same way production did if the
    handler ever regresses back to using .get()."""
    import stripe

    from app import billing as billing_module

    fake_checkout = SimpleNamespace(
        client_reference_id=str(venue["id"]),
        customer="cus_test123",
        subscription="sub_test123",
        customer_details=SimpleNamespace(email="owner@example.com"),
        customer_email=None,
    )
    fake_event = {"type": "checkout.session.completed", "data": {"object": fake_checkout}}

    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a, **k: fake_event)
    monkeypatch.setattr(
        stripe.Subscription, "retrieve",
        lambda _id: SimpleNamespace(items=SimpleNamespace(data=[SimpleNamespace(id="si_test123")])),
    )

    resp = client.post("/billing/webhook", data=b"{}", headers={"Stripe-Signature": "test"})
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        sub = conn.execute("SELECT * FROM rota_subscription WHERE venue_id = ?", (venue["id"],)).fetchone()
        assert sub["plan"] == "active"
        assert sub["stripe_customer_id"] == "cus_test123"
        assert sub["stripe_subscription_item_id"] == "si_test123"
