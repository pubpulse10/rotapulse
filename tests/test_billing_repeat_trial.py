"""
Two guards on /billing/upgrade, both about money.

1. A venue that has been through Checkout before must not be handed another
   free trial. Without this, cancel-and-re-subscribe yields an endless string
   of 30-day trials — the landlord never pays and nothing in the app notices.

2. A venue with a live subscription must not be able to start a second one.
   The subscription page already routes an active venue to change_band, but
   that is a template decision; /billing/upgrade is a plain POST target that a
   double-click, a back-button re-submit or a stale second tab still reaches.
   A second Checkout means a second Stripe subscription and a double charge.

PricePulse grew both guards in Aug 2026 (app/billing.py, upgrade()); this is
the same pair, ported. Stripe is monkeypatched throughout — no network.
"""

from types import SimpleNamespace

from app import billing as billing_module
from app import db as db_module
from tests.conftest import login_as_pub


def _set_band_prices(monkeypatch):
    monkeypatch.setattr(
        billing_module.config, "STRIPE_PRICE_ROTA_BANDS",
        ["price_band1", "price_band2", "price_band3", "price_band4"],
    )


def _capture_checkout(monkeypatch):
    """Stand in for Stripe Checkout and hand back the kwargs it was called
    with, so a test can assert on what we asked Stripe to create."""
    captured = {}
    monkeypatch.setattr(
        billing_module.stripe.checkout.Session,
        "create",
        lambda **kw: captured.update(kw) or SimpleNamespace(url="https://checkout.example/x"),
    )
    return captured


def _set_subscription(app, venue_id, **cols):
    with app.app_context():
        conn = db_module.get_db()
        assignments = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(
            f"UPDATE rota_subscription SET {assignments} WHERE venue_id = ?",
            (*cols.values(), venue_id),
        )
        conn.commit()


def _read_subscription(app, venue_id):
    with app.app_context():
        return db_module.get_db().execute(
            "SELECT * FROM rota_subscription WHERE venue_id = ?", (venue_id,)
        ).fetchone()


# --------------------------------------------------------------------------- #
# 1. The repeat free trial
# --------------------------------------------------------------------------- #

def test_a_brand_new_venue_is_offered_the_free_trial(app, client, venue, monkeypatch):
    """The baseline the other tests are measured against: no Stripe ids stored,
    so this venue has never checked out and does get the 30 days."""
    _set_band_prices(monkeypatch)
    _set_subscription(app, venue["id"], plan="inactive")
    captured = _capture_checkout(monkeypatch)

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": 1})

    assert resp.status_code == 303
    assert captured["subscription_data"]["trial_period_days"] == billing_module.config.ROTAPULSE_TRIAL_DAYS


def test_a_returning_customer_gets_no_second_free_trial(app, client, venue, monkeypatch):
    """Cancel then re-subscribe: plan is back to inactive, but the Stripe
    customer id is still on file, so this is not a first-time customer."""
    _set_band_prices(monkeypatch)
    _set_subscription(app, venue["id"], plan="inactive", stripe_customer_id="cus_seen_before")
    captured = _capture_checkout(monkeypatch)

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": 1})

    assert resp.status_code == 303
    assert "trial_period_days" not in captured["subscription_data"]


def test_a_cleared_customer_id_still_blocks_the_trial_via_the_subscription_id(app, client, venue, monkeypatch):
    """_forget_deleted_customer() nulls stripe_customer_id but leaves the
    subscription id, so the customer id alone would let a returning venue slip
    back into a free trial. Either id is evidence of a prior checkout."""
    _set_band_prices(monkeypatch)
    _set_subscription(
        app, venue["id"], plan="inactive",
        stripe_customer_id=None, stripe_subscription_id="sub_from_last_time",
    )
    captured = _capture_checkout(monkeypatch)

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": 1})

    assert resp.status_code == 303
    assert "trial_period_days" not in captured["subscription_data"]


# --------------------------------------------------------------------------- #
# 2. The second concurrent subscription
# --------------------------------------------------------------------------- #

def test_an_active_subscriber_cannot_start_a_second_subscription(app, client, venue, monkeypatch):
    _set_band_prices(monkeypatch)
    _set_subscription(
        app, venue["id"], plan="active",
        stripe_customer_id="cus_live", stripe_subscription_id="sub_live",
    )

    def fail_if_called(**kwargs):  # pragma: no cover - asserts it is never reached
        raise AssertionError("Checkout must not be started for an active subscriber")

    monkeypatch.setattr(billing_module.stripe.checkout.Session, "create", fail_if_called)

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": 2})

    assert resp.status_code == 302
    assert "/billing/subscription" in resp.headers["Location"]


def test_a_blocked_resubmit_does_not_rewrite_the_recorded_band(app, client, venue, monkeypatch):
    """The band write used to happen before any guard. Left there it would let
    a blocked re-submit move an active subscriber's recorded band while nothing
    changed at Stripe — the display and the real subscription would disagree."""
    _set_band_prices(monkeypatch)
    _set_subscription(
        app, venue["id"], plan="active", current_tier=1,
        stripe_customer_id="cus_live", stripe_subscription_id="sub_live",
    )

    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": 4})

    assert _read_subscription(app, venue["id"])["current_tier"] == 1


def test_an_active_plan_with_no_stripe_subscription_can_still_check_out(app, client, venue, monkeypatch):
    """The guard needs something to duplicate. A venue left on plan='active'
    with no subscription id — a comped venue, or one whose Stripe Customer was
    deleted in the Dashboard — must keep its route back to a real subscription,
    which is the lockout _forget_deleted_customer exists to prevent."""
    _set_band_prices(monkeypatch)
    _set_subscription(app, venue["id"], plan="active", stripe_subscription_id=None)
    captured = _capture_checkout(monkeypatch)

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": 1})

    assert resp.status_code == 303
    assert captured["line_items"][0]["price"] == "price_band1"
