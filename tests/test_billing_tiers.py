from types import SimpleNamespace

from app import db as db_module
from app.billing import count_billable_staff, tier_for_count
from tests.conftest import create_active_staff

# A fixed Stripe current_period_end timestamp, used wherever a test needs a
# real renewal date — 2026-08-12 in UTC. Matches the sibling apps' own tests.
FAKE_PERIOD_END_TS = 1786512000
FAKE_PERIOD_END_ISO = "2026-08-12"


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


def test_webhook_checkout_completed_captures_renewal_date(app, client, venue, monkeypatch):
    """The same stripe.Subscription.retrieve() call already made to get
    stripe_subscription_item_id also yields current_period_end — no extra
    API call needed, just read another field off the object already in
    scope."""
    import stripe

    from app import billing as billing_module

    fake_checkout = SimpleNamespace(
        client_reference_id=str(venue["id"]), customer="cus_test123", subscription="sub_test123",
    )
    fake_event = {"type": "checkout.session.completed", "data": {"object": fake_checkout}}

    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a, **k: fake_event)
    monkeypatch.setattr(
        stripe.Subscription, "retrieve",
        lambda _id: SimpleNamespace(
            items=SimpleNamespace(data=[SimpleNamespace(id="si_test123")]),
            current_period_end=FAKE_PERIOD_END_TS,
        ),
    )

    resp = client.post("/billing/webhook", data=b"{}", headers={"Stripe-Signature": "test"})
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        sub = conn.execute("SELECT current_period_end FROM rota_subscription WHERE venue_id = ?", (venue["id"],)).fetchone()
    assert sub["current_period_end"] == FAKE_PERIOD_END_ISO


def test_webhook_subscription_updated_captures_renewal_without_extra_api_call(app, client, venue, monkeypatch):
    """customer.subscription.updated already carries current_period_end
    directly on the event object — no Subscription.retrieve() call should
    ever be made here, unlike checkout.session.completed."""
    import stripe

    from app import billing as billing_module

    def fail_if_called(*args, **kwargs):
        raise AssertionError("Subscription.retrieve should not be called for customer.subscription.updated")

    monkeypatch.setattr(stripe.Subscription, "retrieve", fail_if_called)

    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_subscription SET stripe_subscription_id = ? WHERE venue_id = ?",
            ("sub_existing", venue["id"]),
        )
        conn.commit()

    fake_event = {
        "type": "customer.subscription.updated",
        "data": {"object": SimpleNamespace(
            id="sub_existing", status="active", current_period_end=FAKE_PERIOD_END_TS,
        )},
    }
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a, **k: fake_event)

    resp = client.post("/billing/webhook", data=b"{}", headers={"Stripe-Signature": "test"})
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        sub = conn.execute(
            "SELECT current_period_end FROM rota_subscription WHERE venue_id = ?", (venue["id"],)
        ).fetchone()
    assert sub["current_period_end"] == FAKE_PERIOD_END_ISO


def test_webhook_pushes_entitlement_with_renewal_and_stripe_ids(app, client, venue, monkeypatch):
    import stripe

    from app import billing as billing_module

    monkeypatch.setattr(billing_module.config, "INTERNAL_API_SECRET", "some-secret")
    monkeypatch.setattr(billing_module.config, "PUBPULSE_HUB_URL", "https://hub.example.com")

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(billing_module.requests, "post", fake_post)

    fake_checkout = SimpleNamespace(
        client_reference_id=str(venue["id"]), customer="cus_test123", subscription="sub_test123",
    )
    fake_event = {"type": "checkout.session.completed", "data": {"object": fake_checkout}}
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a, **k: fake_event)
    monkeypatch.setattr(
        stripe.Subscription, "retrieve",
        lambda _id: SimpleNamespace(
            items=SimpleNamespace(data=[SimpleNamespace(id="si_test123")]),
            current_period_end=FAKE_PERIOD_END_TS,
        ),
    )

    client.post("/billing/webhook", data=b"{}", headers={"Stripe-Signature": "test"})

    assert captured["url"] == "https://hub.example.com/internal/entitlements"
    assert captured["json"]["pub_id"] == venue["pub_id"]
    assert captured["json"]["app_key"] == "rotapulse"
    assert captured["json"]["renewal_at"] == FAKE_PERIOD_END_ISO
    assert captured["json"]["stripe_customer_id"] == "cus_test123"
    assert captured["json"]["stripe_subscription_id"] == "sub_test123"
    assert captured["headers"]["Authorization"] == "Bearer some-secret"
