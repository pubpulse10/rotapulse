"""
Rewardful affiliate tagging on RotaPulse's own Stripe Customer — part 3 of
the family's Rewardful brief, porting PricePulse's apply_referral_metadata
(its D55/D56).

PricePulse captures a pub's referral at registration and serves it from
GET /internal/pubs/<pub_id>/referral. This app writes the affiliate's link
TOKEN (the referral id only as a fallback) onto the Stripe Customer it
creates or reuses for a venue, keyed by the venue's pub_id: after Checkout
for a new Customer, BEFORE the Session for a returning one.

Nothing here reaches the network. conftest.py stubs the PricePulse lookup out
for every test; the autouse `pricepulse` fixture below puts the real lookup
back over a fake requests.get, and every Stripe call is monkeypatched.
"""

import logging
from types import SimpleNamespace

import pytest
import requests
import stripe

from app import billing as billing_module
from app import db as db_module
from tests.conftest import TEST_PUB_ID, login_as_pub

SECRET = "test-internal-secret"
PRICEPULSE_URL = "https://pricepulse.example"
# Same shapes as PricePulse's own tests (taken from Rewardful's JS API docs).
REF = "98288128-0d5f-45a9-88b3-ef95b229f798"
AFF_ID = "b533bfca-7c70-4dec-9691-e136a8d9a26c"
TOKEN = "james007"
TOKEN_TAG = {"referral": TOKEN, "pubpulse_pub_id": str(TEST_PUB_ID)}


@pytest.fixture(autouse=True)
def pricepulse(no_real_pricepulse_lookup, monkeypatch):
    """Stands in for PricePulse's referral endpoint. Swaps conftest's stub
    back for the real lookup, so the HTTP handling itself is under test, and
    fakes requests.get underneath it. `status`, `body` and `error` shape the
    answer; `calls` records every request. requests.post is faked too: with
    the secret set, activation also pushes to the Hub."""
    state = SimpleNamespace(
        calls=[],
        status=200,
        body={"pub_id": TEST_PUB_ID, "referral_id": REF, "affiliate_id": AFF_ID, "affiliate_token": TOKEN},
        error=None,
    )

    def fake_json():
        if isinstance(state.body, Exception):
            raise state.body
        return state.body

    def fake_get(url, headers=None, timeout=None, **kwargs):
        state.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if state.error:
            raise state.error
        return SimpleNamespace(status_code=state.status, json=fake_json)

    monkeypatch.setattr(billing_module, "_fetch_referral", no_real_pricepulse_lookup)
    monkeypatch.setattr(billing_module.requests, "get", fake_get)
    monkeypatch.setattr(billing_module.requests, "post", lambda *a, **k: SimpleNamespace(status_code=200))
    monkeypatch.setattr(billing_module.config, "INTERNAL_API_SECRET", SECRET)
    monkeypatch.setattr(billing_module.config, "PRICEPULSE_INTERNAL_URL", PRICEPULSE_URL)
    return state


def _customer(**fields):
    """A real StripeObject rather than a SimpleNamespace, so attribute access
    behaves like the SDK's — a missing metadata key raises, and .get() is not
    what it looks like."""
    return stripe.Customer.construct_from(
        {"id": "cus_new", "object": "customer", **fields}, "sk_test_x"
    )


@pytest.fixture
def stripe_calls(monkeypatch):
    """Records Customer.retrieve/modify and Checkout Session creation in the
    order they happen. `customer` is what retrieve returns; tests replace it,
    or set `retrieve_error` / `modify_error` to make that call fail."""
    state = SimpleNamespace(
        calls=[],
        customer=_customer(metadata={}),
        retrieve_error=None,
        modify_error=None,
        checkout_kwargs=None,
    )

    def fake_retrieve(customer_id):
        state.calls.append(("retrieve", customer_id))
        if state.retrieve_error:
            raise state.retrieve_error
        return state.customer

    def fake_modify(customer_id, **kwargs):
        state.calls.append(("modify", customer_id, kwargs.get("metadata")))
        if state.modify_error:
            raise state.modify_error
        return state.customer

    def fake_session_create(**kwargs):
        state.calls.append(("checkout", kwargs.get("customer")))
        state.checkout_kwargs = kwargs
        return SimpleNamespace(url="https://checkout.stripe.com/fake-session")

    monkeypatch.setattr(billing_module.stripe.Customer, "retrieve", fake_retrieve)
    monkeypatch.setattr(billing_module.stripe.Customer, "modify", fake_modify)
    monkeypatch.setattr(billing_module.stripe.checkout.Session, "create", fake_session_create)
    monkeypatch.setattr(
        billing_module.stripe.Subscription, "retrieve",
        lambda subscription_id: SimpleNamespace(
            items=SimpleNamespace(data=[SimpleNamespace(id="si_new")]),
            current_period_end=1786512000,
            status="trialing",
        ),
    )
    return state


def _set_subscription(app, venue_id, **cols):
    with app.app_context():
        conn = db_module.get_db()
        assignments = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(
            f"UPDATE rota_subscription SET {assignments} WHERE venue_id = ?",
            (*cols.values(), venue_id),
        )
        conn.commit()


def _subscription_row(app, venue_id):
    with app.app_context():
        return db_module.get_db().execute(
            "SELECT * FROM rota_subscription WHERE venue_id = ?", (venue_id,)
        ).fetchone()


def _set_band_prices(monkeypatch):
    monkeypatch.setattr(
        billing_module.config, "STRIPE_PRICE_ROTA_BANDS",
        ["price_band1", "price_band2", "price_band3", "price_band4"],
    )


def _complete_checkout(client, monkeypatch, venue_id, customer="cus_new"):
    """Delivers checkout.session.completed for this venue to the webhook."""
    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": SimpleNamespace(
                client_reference_id=str(venue_id),
                customer=customer,
                subscription="sub_new",
                metadata=SimpleNamespace(pubpulse_app="rotapulse"),
                customer_details=None,
                customer_email=None,
            )
        },
    }
    monkeypatch.setattr(
        billing_module.stripe.Webhook, "construct_event", lambda payload, sig, secret: event
    )
    return client.post("/billing/webhook", data=b"{}", headers={"Stripe-Signature": "sig"})


def _warned(caplog):
    return any(r.name == "app.billing" and r.levelno >= logging.WARNING for r in caplog.records)


# --- a new Customer: tagged once Checkout completes --------------------------

def test_completed_checkout_tags_the_customer_with_the_affiliate_token(
    app, client, venue, monkeypatch, stripe_calls
):
    # The token, not the referral id: one referral id attaches to one Customer
    # only, and this pub may already have spent it on a sibling app's.
    _set_subscription(app, venue["id"], plan="inactive")

    resp = _complete_checkout(client, monkeypatch, venue["id"])

    assert resp.status_code == 200
    assert stripe_calls.calls == [("retrieve", "cus_new"), ("modify", "cus_new", TOKEN_TAG)]
    assert _subscription_row(app, venue["id"])["plan"] == "active"


def test_the_lookup_is_keyed_by_the_venues_pub_id_and_authenticated(
    app, client, venue, monkeypatch, pricepulse, stripe_calls
):
    # PricePulse stores the referral against the family account (pub_id), not
    # a RotaPulse venue — asking by venue id would ask about some other pub.
    assert venue["id"] != venue["pub_id"]
    _set_subscription(app, venue["id"], plan="inactive")

    _complete_checkout(client, monkeypatch, venue["id"])

    assert pricepulse.calls == [{
        "url": f"{PRICEPULSE_URL}/internal/pubs/{venue['pub_id']}/referral",
        "headers": {"Authorization": f"Bearer {SECRET}"},
        "timeout": 5,
    }]


def test_the_referral_id_is_the_fallback_when_no_token_was_captured(
    app, client, venue, monkeypatch, pricepulse, stripe_calls
):
    pricepulse.body = {**pricepulse.body, "affiliate_token": None}
    _set_subscription(app, venue["id"], plan="inactive")

    _complete_checkout(client, monkeypatch, venue["id"])

    assert ("modify", "cus_new", {"referral": REF, "pubpulse_pub_id": str(TEST_PUB_ID)}) in stripe_calls.calls


def test_the_return_page_activation_tags_the_customer_too(
    app, client, venue, monkeypatch, stripe_calls
):
    # The landlord's browser can beat the webhook back. Both paths share
    # _activate_from_checkout, so the tag can't depend on which one wins.
    _set_subscription(app, venue["id"], plan="inactive")
    monkeypatch.setattr(
        billing_module.stripe.checkout.Session, "retrieve",
        lambda session_id: SimpleNamespace(
            client_reference_id=str(venue["id"]),
            metadata=SimpleNamespace(pubpulse_app="rotapulse"),
            customer="cus_new", subscription="sub_new",
            status="complete", payment_status="no_payment_required",
        ),
    )

    resp = client.get(f"/v/{venue['slug']}/billing/success?session_id=cs_test_1")

    assert resp.status_code == 200
    assert ("modify", "cus_new", TOKEN_TAG) in stripe_calls.calls
    assert _subscription_row(app, venue["id"])["plan"] == "active"


# --- nothing to tag: no Stripe call at all -----------------------------------

def _not_referred(pp):
    pp.body = {"pub_id": TEST_PUB_ID, "referral_id": None, "affiliate_id": None, "affiliate_token": None}


def _unknown_pub(pp):
    pp.status = 404
    pp.body = {"error": "not found"}


def _bad_secret(pp):
    pp.status = 401
    pp.body = {"error": "unauthorised"}


def _times_out(pp):
    pp.error = requests.Timeout("PricePulse took longer than 5 seconds")


def _unreachable(pp):
    pp.error = requests.ConnectionError("connection refused")


def _not_json(pp):
    pp.body = ValueError("Expecting value: line 1 column 1 (char 0)")


@pytest.mark.parametrize(
    "answer, warns",
    [
        (_not_referred, False),
        (_unknown_pub, True),
        (_bad_secret, True),
        (_times_out, True),
        (_unreachable, True),
        (_not_json, True),
    ],
    ids=["not-referred", "404-unknown-pub", "401-bad-secret", "timeout", "unreachable", "not-json"],
)
def test_no_customer_call_when_pricepulse_has_no_referral_to_give(
    app, client, venue, monkeypatch, caplog, pricepulse, stripe_calls, answer, warns
):
    # An unreferred pub is the common case and stays quiet; anything else is
    # logged. Either way the subscription is recorded as normal.
    _set_subscription(app, venue["id"], plan="inactive")
    answer(pricepulse)

    with caplog.at_level(logging.WARNING, logger="app.billing"):
        resp = _complete_checkout(client, monkeypatch, venue["id"])

    assert resp.status_code == 200
    assert len(pricepulse.calls) == 1
    assert stripe_calls.calls == []
    assert _subscription_row(app, venue["id"])["plan"] == "active"
    assert _warned(caplog) is warns


def test_nothing_is_looked_up_without_the_internal_secret(
    app, client, venue, monkeypatch, pricepulse, stripe_calls
):
    # Local dev, or a deploy that isn't wired to the family yet.
    monkeypatch.setattr(billing_module.config, "INTERNAL_API_SECRET", None)
    _set_subscription(app, venue["id"], plan="inactive")

    resp = _complete_checkout(client, monkeypatch, venue["id"])

    assert resp.status_code == 200
    assert pricepulse.calls == []
    assert stripe_calls.calls == []
    assert _subscription_row(app, venue["id"])["plan"] == "active"


def test_a_venue_without_a_pub_id_is_skipped(app, client, venue, monkeypatch, pricepulse, stripe_calls):
    # pub_id is the only thing PricePulse knows the pub by.
    with app.app_context():
        conn = db_module.get_db()
        conn.execute("UPDATE venue SET pub_id = NULL WHERE id = ?", (venue["id"],))
        conn.commit()
    _set_subscription(app, venue["id"], plan="inactive")

    resp = _complete_checkout(client, monkeypatch, venue["id"])

    assert resp.status_code == 200
    assert pricepulse.calls == []
    assert stripe_calls.calls == []
    assert _subscription_row(app, venue["id"])["plan"] == "active"


@pytest.mark.parametrize(
    "pub_id, customer_id",
    [(TEST_PUB_ID, None), (None, "cus_new"), (0, "cus_new")],
    ids=["no-customer", "no-pub", "dev-venue-pub-0"],
)
def test_returns_before_any_call_without_a_customer_or_a_pub(pricepulse, stripe_calls, pub_id, customer_id):
    # pub_id 0 is scripts/init_db.py's seeded dev venue, never a real pub.
    billing_module.apply_referral_metadata(pub_id, customer_id)

    assert pricepulse.calls == []
    assert stripe_calls.calls == []


# --- what's already on the Customer ------------------------------------------

@pytest.mark.parametrize(
    "already_there", [TOKEN, "11111111-2222-3333-4444-555555555555"], ids=["our-token", "a-referral-uuid"]
)
def test_a_referral_already_on_the_customer_is_never_overwritten(
    app, client, venue, monkeypatch, caplog, stripe_calls, already_there
):
    # Our token: nothing to do. A UUID: Rewardful has already swapped the
    # token for its own referral id, or someone attributed this Customer by
    # hand. Either way it's attributed — leave it alone, and don't warn about
    # the difference, because Rewardful ALWAYS rewrites the value.
    _set_subscription(app, venue["id"], plan="inactive")
    stripe_calls.customer = _customer(metadata={"referral": already_there})

    with caplog.at_level(logging.WARNING, logger="app.billing"):
        _complete_checkout(client, monkeypatch, venue["id"])

    assert stripe_calls.calls == [("retrieve", "cus_new")]
    assert not _warned(caplog)


def test_a_deleted_customer_is_skipped(app, client, venue, monkeypatch, stripe_calls):
    _set_subscription(app, venue["id"], plan="inactive")
    stripe_calls.customer = _customer(deleted=True)

    _complete_checkout(client, monkeypatch, venue["id"])

    assert stripe_calls.calls == [("retrieve", "cus_new")]


@pytest.mark.parametrize("failing_call", ["retrieve", "modify"])
def test_a_stripe_failure_while_tagging_does_not_stop_activation(
    app, client, venue, monkeypatch, caplog, stripe_calls, failing_call
):
    _set_subscription(app, venue["id"], plan="inactive")
    setattr(stripe_calls, f"{failing_call}_error", stripe.error.APIConnectionError("Stripe is down"))

    with caplog.at_level(logging.WARNING, logger="app.billing"):
        resp = _complete_checkout(client, monkeypatch, venue["id"])

    # A 200, so Stripe doesn't retry an activation that worked...
    assert resp.status_code == 200
    row = _subscription_row(app, venue["id"])
    assert row["plan"] == "active"
    assert row["stripe_customer_id"] == "cus_new"
    assert row["stripe_subscription_item_id"] == "si_new"
    # ...and the failure is on record, to attribute by hand in Rewardful.
    assert _warned(caplog)


# --- a returning customer: tagged BEFORE the Checkout Session ----------------

def _returning_customer(app, venue_id, monkeypatch, stripe_calls):
    """Cancelled then back: plan inactive, the old Stripe Customer still on
    file — so no trial, and Checkout charges the moment it completes."""
    _set_band_prices(monkeypatch)
    _set_subscription(app, venue_id, plan="inactive", stripe_customer_id="cus_old")
    stripe_calls.customer = _customer(id="cus_old", metadata={})


def test_a_returning_customer_is_tagged_before_checkout_charges_them(
    app, client, venue, monkeypatch, stripe_calls
):
    # No trial for a returning customer, so Checkout charges at once — the tag
    # has to be on the Customer before that invoice exists, or Rewardful
    # records no commission on it.
    _returning_customer(app, venue["id"], monkeypatch, stripe_calls)

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": 1})

    assert resp.status_code == 303
    assert stripe_calls.calls == [
        ("retrieve", "cus_old"),
        ("modify", "cus_old", TOKEN_TAG),
        ("checkout", "cus_old"),
    ]


def test_tagging_leaves_the_returning_customers_checkout_kwargs_alone(
    app, client, venue, monkeypatch, stripe_calls
):
    # test_billing_tiers.py pins the VAT kwargs; this pins that a referred
    # returning customer's Session still carries every one of them — and that
    # client_reference_id is still the venue id, never anything of Rewardful's.
    _returning_customer(app, venue["id"], monkeypatch, stripe_calls)

    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": 1})

    kw = stripe_calls.checkout_kwargs
    assert kw["customer"] == "cus_old"
    assert kw["client_reference_id"] == str(venue["id"])
    assert kw["metadata"] == {"pubpulse_app": "rotapulse"}
    assert kw["subscription_data"] == {"metadata": {"pubpulse_app": "rotapulse"}}  # no trial
    assert kw["automatic_tax"] == {"enabled": True}
    assert kw["billing_address_collection"] == "required"
    assert kw["customer_update"] == {"address": "auto", "name": "auto"}
    assert kw["tax_id_collection"] == {"enabled": True}
    assert kw["payment_method_collection"] == "always"


def _pricepulse_down(pp, stripe_calls):
    pp.error = requests.Timeout("PricePulse took longer than 5 seconds")


def _stripe_modify_fails(pp, stripe_calls):
    stripe_calls.modify_error = stripe.error.APIConnectionError("Stripe is down")


@pytest.mark.parametrize("failure", [_pricepulse_down, _stripe_modify_fails], ids=["pricepulse-down", "modify-fails"])
def test_a_failed_tag_never_keeps_a_returning_customer_from_checkout(
    app, client, venue, monkeypatch, pricepulse, stripe_calls, failure
):
    _returning_customer(app, venue["id"], monkeypatch, stripe_calls)
    failure(pricepulse, stripe_calls)

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": 1})

    assert resp.status_code == 303
    assert resp.headers["Location"] == "https://checkout.stripe.com/fake-session"
    assert stripe_calls.calls[-1] == ("checkout", "cus_old")


def test_a_first_time_customer_is_not_looked_up_before_checkout(
    app, client, venue, monkeypatch, pricepulse, stripe_calls
):
    # No Customer exists yet — Checkout makes it — so there's nothing to tag
    # until it completes. Its card-trial means the first invoice is £0.
    _set_band_prices(monkeypatch)
    _set_subscription(app, venue["id"], plan="inactive")

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": 1})

    assert resp.status_code == 303
    assert pricepulse.calls == []
    assert stripe_calls.calls == [("checkout", None)]
