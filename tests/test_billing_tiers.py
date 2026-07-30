from types import SimpleNamespace

from app import db as db_module
from app.billing import count_billable_staff, tier_for_count
from tests.conftest import create_active_staff, login_as_pub


def _give_venue_a_live_stripe_subscription(app, venue_id, sub_id="sub_live1", item_id="si_live1", band=1):
    """Mimics what checkout.session.completed's webhook handler + band choice
    would have already set — enforce_band() no-ops without these, so tests that
    exercise the approve/leave -> Stripe path need this first. `band` seeds the
    venue's current band."""
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_subscription SET stripe_subscription_id = ?, stripe_subscription_item_id = ?, "
            "current_tier = ? WHERE venue_id = ?",
            (sub_id, item_id, band, venue_id),
        )
        conn.commit()


def _set_band_prices(monkeypatch):
    """Point the four band price slots at fake ids so billing acts (they're
    env-sourced and unset in tests)."""
    from app import billing as billing_module
    monkeypatch.setattr(
        billing_module.config, "STRIPE_PRICE_ROTA_BANDS",
        ["price_band1", "price_band2", "price_band3", "price_band4"],
    )


def _add_pending_approval_staff(app, venue_id, name="New Hire"):
    """Mirrors onboarding.accept()'s end state: person + active membership +
    a rota_staff_detail row already in place + app_access still
    'pending_approval' — i.e. everything except the admin's approval tap."""
    with app.app_context():
        conn = db_module.get_db()
        person_cur = conn.execute("INSERT INTO person (name, email) VALUES (?, ?)", (name, f"{name}@example.com"))
        person_id = person_cur.lastrowid
        membership_cur = conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')",
            (person_id, venue_id),
        )
        membership_id = membership_cur.lastrowid
        app_id = db_module.get_app_id(conn, "rotapulse")
        access_cur = conn.execute(
            """INSERT INTO app_access (venue_membership_id, app_id, permission_level, status, accepted_at)
               VALUES (?, ?, 'staff', 'pending_approval', datetime('now'))""",
            (membership_id, app_id),
        )
        access_id = access_cur.lastrowid
        conn.execute(
            "INSERT INTO rota_staff_detail (venue_membership_id, hourly_pay_rate, availability) VALUES (?, 10, '{}')",
            (membership_id,),
        )
        conn.commit()
    return access_id, membership_id

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
        metadata=SimpleNamespace(pubpulse_app="rotapulse"),
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
        client_reference_id=str(venue["id"]),
        metadata=SimpleNamespace(pubpulse_app="rotapulse"), customer="cus_test123", subscription="sub_test123",
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
        client_reference_id=str(venue["id"]),
        metadata=SimpleNamespace(pubpulse_app="rotapulse"), customer="cus_test123", subscription="sub_test123",
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


def test_upgrade_uses_the_chosen_band_price(app, client, venue, monkeypatch):
    """The landlord picks a band; Checkout uses that band's flat price (qty 1)
    and the chosen band is recorded."""
    import stripe

    from app import billing as billing_module

    _set_band_prices(monkeypatch)

    captured = {}
    monkeypatch.setattr(
        stripe.checkout.Session, "create",
        lambda **kw: captured.update(kw) or SimpleNamespace(url="https://checkout.example/x"),
    )

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": "2"})
    assert resp.status_code == 303
    assert captured["line_items"] == [{"price": "price_band2", "quantity": 1}]
    assert captured["subscription_data"]["trial_period_days"] == billing_module.config.ROTAPULSE_TRIAL_DAYS
    assert captured["metadata"] == {"pubpulse_app": "rotapulse"}

    with app.app_context():
        conn = db_module.get_db()
        sub = conn.execute(
            "SELECT current_tier FROM rota_subscription WHERE venue_id = ?", (venue["id"],)
        ).fetchone()
    assert sub["current_tier"] == 2


def test_upgrade_rejects_band_below_staff_requirement(app, client, venue, monkeypatch):
    _set_band_prices(monkeypatch)
    for i in range(6):  # 6 billable staff -> needs at least band 2
        create_active_staff(app, venue["id"], name=f"Staff {i}")
    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": "1"})
    assert resp.status_code == 400


def test_approving_staff_bumps_band_when_it_grows_past_current(app, client, venue, monkeypatch):
    """Approving a staff member that pushes the count past the current band
    moves the subscription UP a band — repriced to the higher band's flat price
    at the next bill (proration 'none'), not immediately."""
    import stripe

    _set_band_prices(monkeypatch)
    _give_venue_a_live_stripe_subscription(app, venue["id"], band=1)
    for i in range(5):
        create_active_staff(app, venue["id"], name=f"Existing {i}")
    access_id, _membership_id = _add_pending_approval_staff(app, venue["id"])

    captured = {}
    monkeypatch.setattr(
        stripe.Subscription, "modify",
        lambda sub_id, items=None, proration_behavior=None: captured.update(
            sub_id=sub_id, items=items, proration_behavior=proration_behavior) or SimpleNamespace(),
    )

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/approve")
    assert resp.status_code == 302

    # 5 existing + 1 approved = 6 -> band 2, repriced at next bill.
    assert captured["sub_id"] == "sub_live1"
    assert captured["items"] == [{"id": "si_live1", "price": "price_band2", "quantity": 1}]
    assert captured["proration_behavior"] == "none"

    with app.app_context():
        conn = db_module.get_db()
        sub = conn.execute(
            "SELECT current_tier FROM rota_subscription WHERE venue_id = ?", (venue["id"],)
        ).fetchone()
    assert sub["current_tier"] == 2


def test_marking_staff_left_does_not_downgrade_band(app, client, venue, monkeypatch):
    """No auto-downgrade: a shrinking venue keeps its band (and no Stripe call
    is made) until the landlord lowers it themselves."""
    import stripe

    _set_band_prices(monkeypatch)
    _give_venue_a_live_stripe_subscription(app, venue["id"], band=2)
    _person_id, membership_id, _email = create_active_staff(app, venue["id"], name="Leaver")
    for i in range(5):
        create_active_staff(app, venue["id"], name=f"Remaining {i}")

    called = {"modify": False}
    monkeypatch.setattr(
        stripe.Subscription, "modify",
        lambda *a, **k: called.update(modify=True) or SimpleNamespace(),
    )

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/admin/staff/{membership_id}/leave")
    assert resp.status_code == 302

    # 6 -> 5 is still within band 2: no downgrade, no Stripe call.
    assert called["modify"] is False
    with app.app_context():
        conn = db_module.get_db()
        sub = conn.execute(
            "SELECT current_tier FROM rota_subscription WHERE venue_id = ?", (venue["id"],)
        ).fetchone()
    assert sub["current_tier"] == 2
