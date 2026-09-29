"""Two Subscription-page behaviours kept in step across the family
(29 September 2026):

1. A venue that has checked out before gets no second free trial
   (billing.is_returning_customer, which upgrade() uses to decide the trial).
   The subscription page and the locked screen word themselves on the same
   function, so they can never promise a trial Checkout won't give.
2. Once subscribed, the footer under the Next payment amount (which includes
   VAT) no longer says prices exclude VAT.

Stripe is monkeypatched throughout — no network."""

from types import SimpleNamespace

import pytest

from app import billing as billing_module
from app import db as db_module
from tests.conftest import login_as_pub

WELCOME_BACK = "Welcome back. You've had your free trial, so you'll be charged when you"
TRIAL_PROMISE = "Subscribing starts your free"


@pytest.fixture
def prices(monkeypatch):
    monkeypatch.setattr(
        billing_module.config, "STRIPE_PRICE_ROTA_BANDS",
        ["price_band1", "price_band2", "price_band3", "price_band4"],
    )


def _set(app, venue, **cols):
    with app.app_context():
        conn = db_module.get_db()
        assignments = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(
            f"UPDATE rota_subscription SET {assignments} WHERE venue_id = ?",
            (*cols.values(), venue["id"]),
        )
        conn.commit()


def _checkout_trial_days(client, venue, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        billing_module.stripe.checkout.Session, "create",
        lambda **kw: captured.update(kw) or SimpleNamespace(url="https://checkout.example/x"),
    )
    monkeypatch.setattr(billing_module, "apply_referral_metadata", lambda *a, **k: None)
    resp = client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": 1})
    assert resp.status_code == 303
    return captured["subscription_data"].get("trial_period_days")


def _page(client, venue):
    return client.get(f"/v/{venue['slug']}/billing/subscription").get_data(as_text=True)


def test_is_returning_customer_reads_either_id():
    assert not billing_module.is_returning_customer(None)
    assert not billing_module.is_returning_customer(
        {"stripe_customer_id": None, "stripe_subscription_id": None})
    assert billing_module.is_returning_customer(
        {"stripe_customer_id": "cus_x", "stripe_subscription_id": None})
    assert billing_module.is_returning_customer(
        {"stripe_customer_id": None, "stripe_subscription_id": "sub_x"})


def test_new_venue_is_promised_the_trial_and_checkout_gives_it(app, client, venue, prices, monkeypatch):
    _set(app, venue, plan="inactive", stripe_customer_id=None, stripe_subscription_id=None)
    login_as_pub(client, venue["pub_id"])
    body = _page(client, venue)
    assert TRIAL_PROMISE in body
    assert WELCOME_BACK not in body
    assert "Subscribe now" in body
    assert _checkout_trial_days(client, venue, monkeypatch) == billing_module.config.ROTAPULSE_TRIAL_DAYS


@pytest.mark.parametrize("ids", [
    {"stripe_customer_id": "cus_before", "stripe_subscription_id": None},
    # _forget_deleted_customer() cleared the customer id; the sub id survived.
    {"stripe_customer_id": None, "stripe_subscription_id": "sub_before"},
])
def test_returning_venue_is_told_it_will_be_charged_and_checkout_agrees(app, client, venue, prices, monkeypatch, ids):
    _set(app, venue, plan="inactive", **ids)
    login_as_pub(client, venue["pub_id"])
    body = _page(client, venue)
    if ids["stripe_subscription_id"]:
        # With a surviving subscription id the page treats the venue as active
        # (Change band); the plan chooser isn't shown, so there's no promise.
        assert TRIAL_PROMISE not in body
    else:
        assert WELCOME_BACK in body
        assert "VAT — cancel any time." in body
        assert TRIAL_PROMISE not in body
        assert "Subscribe now" in body
    assert "Start free trial" not in body
    assert _checkout_trial_days(client, venue, monkeypatch) is None


def test_unsubscribed_footer_still_says_prices_exclude_vat(app, client, venue, prices):
    _set(app, venue, plan="inactive", stripe_customer_id=None, stripe_subscription_id=None)
    login_as_pub(client, venue["pub_id"])
    body = _page(client, venue)
    assert "All prices exclude VAT, charged at the prevailing rate." in body
    assert "The Next payment amount includes VAT" not in body


def test_subscribed_footer_says_the_next_payment_includes_vat(app, client, venue, prices, monkeypatch):
    _set(app, venue, plan="active", stripe_customer_id="cus_live", stripe_subscription_id="sub_live",
         subscription_status="active")
    monkeypatch.setattr(billing_module.config, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(billing_module.stripe.Invoice, "create_preview",
                        lambda **kw: SimpleNamespace(amount_due=3600))
    login_as_pub(client, venue["pub_id"])
    body = _page(client, venue)
    assert "£36.00 including VAT" in body
    assert "The Next payment amount includes VAT, charged at the prevailing rate." in body
    assert "All prices exclude VAT" not in body


def test_locked_screen_uses_the_same_rule(app, client, venue, prices):
    """The locked screen has no plan buttons, but it too must only mention
    the free days to a venue checkout would give them to."""
    _set(app, venue, plan="inactive", stripe_customer_id=None, stripe_subscription_id=None)
    login_as_pub(client, venue["pub_id"])
    body = client.get(f"/v/{venue['slug']}/dashboard/").get_data(as_text=True)
    assert "isn't set up yet" in body and "days are free" in body

    _set(app, venue, stripe_customer_id="cus_before")
    body = client.get(f"/v/{venue['slug']}/dashboard/").get_data(as_text=True)
    assert "is locked" in body
    assert "days are free" not in body
