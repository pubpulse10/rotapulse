"""The 26-50 band and the 50-staff cap (Nov 2026 pricing change).

Bands: 1-5 £10 | 6-10 £20 | 11-17 £34 | 18-25 £48 | 26-50 £68 | 51+ price on
application. Above 50 the app refuses to make another billable staff member
active, on every route that can: invite, approval, reinstate, the staff record
(which creates the pay record) and the Hub access push. Admin-only accounts
(no rota_staff_detail row) still never count.
"""
from types import SimpleNamespace

import pytest

from app import config
from app import db as db_module
from app.billing import count_billable_staff, staff_cap_reached, tier_for_count
from tests.conftest import login_as_pub

CAP_TEXT = "Your team is over 50. Contact us for pricing"
HUB_HEADERS = {"Authorization": "Bearer test-secret"}


def _bulk_staff(app, venue_id, n, prefix="Staff"):
    """n fully-active billable staff, inserted directly — create_active_staff
    hashes a password per person, far too slow at fifty a test."""
    ids = []
    with app.app_context():
        conn = db_module.get_db()
        app_id = db_module.get_app_id(conn, "rotapulse")
        for i in range(n):
            person_id = conn.execute(
                "INSERT INTO person (name, email) VALUES (?, ?)", (f"{prefix} {i}", f"{prefix}{i}@example.com".lower())
            ).lastrowid
            membership_id = conn.execute(
                "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')",
                (person_id, venue_id),
            ).lastrowid
            conn.execute(
                """INSERT INTO app_access (venue_membership_id, app_id, permission_level, status, accepted_at, approved_at)
                   VALUES (?, ?, 'staff', 'active', datetime('now'), datetime('now'))""",
                (membership_id, app_id),
            )
            conn.execute(
                "INSERT INTO rota_staff_detail (venue_membership_id, hourly_pay_rate, availability) VALUES (?, 11, '{}')",
                (membership_id,),
            )
            ids.append(membership_id)
        conn.commit()
    return ids


def _pending(app, venue_id, name="New Hire"):
    """onboarding.accept()'s end state: everything but the approval tap."""
    with app.app_context():
        conn = db_module.get_db()
        person_id = conn.execute(
            "INSERT INTO person (name, email) VALUES (?, ?)", (name, f"{name.replace(' ', '')}@example.com")
        ).lastrowid
        membership_id = conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')",
            (person_id, venue_id),
        ).lastrowid
        app_id = db_module.get_app_id(conn, "rotapulse")
        access_id = conn.execute(
            """INSERT INTO app_access (venue_membership_id, app_id, permission_level, status, accepted_at)
               VALUES (?, ?, 'staff', 'pending_approval', datetime('now'))""",
            (membership_id, app_id),
        ).lastrowid
        conn.execute(
            "INSERT INTO rota_staff_detail (venue_membership_id, hourly_pay_rate, availability) VALUES (?, 10, '{}')",
            (membership_id,),
        )
        conn.commit()
    return access_id, membership_id


def _status(app, membership_id):
    with app.app_context():
        conn = db_module.get_db()
        return conn.execute(
            """SELECT venue_membership.status AS m, app_access.status AS a FROM venue_membership
               JOIN app_access ON app_access.venue_membership_id = venue_membership.id
               WHERE venue_membership.id = ?""",
            (membership_id,),
        ).fetchone()


def _live_sub(app, venue_id, band):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_subscription SET stripe_subscription_id = 'sub_live1', "
            "stripe_subscription_item_id = 'si_live1', current_tier = ? WHERE venue_id = ?",
            (band, venue_id),
        )
        conn.commit()


def _current_tier(app, venue_id):
    with app.app_context():
        conn = db_module.get_db()
        return conn.execute(
            "SELECT current_tier FROM rota_subscription WHERE venue_id = ?", (venue_id,)
        ).fetchone()["current_tier"]


@pytest.fixture
def band_prices(monkeypatch):
    from app import billing as billing_module
    monkeypatch.setattr(
        billing_module.config, "STRIPE_PRICE_ROTA_BANDS",
        ["price_band1", "price_band2", "price_band3", "price_band4", "price_band5"],
    )


@pytest.fixture
def stripe_modify(monkeypatch):
    import stripe
    calls = []
    monkeypatch.setattr(
        stripe.Subscription, "modify",
        lambda sub_id, items=None, proration_behavior=None: calls.append(
            {"sub_id": sub_id, "items": items, "proration_behavior": proration_behavior}) or SimpleNamespace(),
    )
    return calls


# --- the price table ---

def test_staff_tiers_are_the_shared_hr_pulse_table():
    assert config.STAFF_TIERS == [(5, 1000), (10, 2000), (17, 3400), (25, 4800), (50, 6800)]
    assert config.MAX_BILLABLE_STAFF == 50


def test_tier_boundaries_including_the_new_band():
    assert tier_for_count(25) == 4
    assert tier_for_count(26) == 5
    assert tier_for_count(50) == 5
    # Over the cap can only arise outside the app's own routes; it is priced at
    # the top band rather than crashing.
    assert tier_for_count(51) == 5


def test_the_cap_message_reads_as_agreed():
    assert config.STAFF_CAP_MESSAGE.startswith(CAP_TEXT)
    assert "info@pubpulse.co.uk" in config.STAFF_CAP_MESSAGE


# --- 25 -> 26 moves up to band 5, and back down, prorated both ways ---

def test_approving_the_26th_moves_to_band_5_prorated(app, client, venue, band_prices, stripe_modify):
    _live_sub(app, venue["id"], band=4)
    _bulk_staff(app, venue["id"], 25)
    access_id, _m = _pending(app, venue["id"])

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/approve")
    assert resp.status_code == 302

    assert stripe_modify == [{
        "sub_id": "sub_live1",
        "items": [{"id": "si_live1", "price": "price_band5", "quantity": 1}],
        "proration_behavior": "always_invoice",
    }]
    assert _current_tier(app, venue["id"]) == 5


def test_26_back_to_25_moves_down_to_band_4_prorated(app, client, venue, band_prices, stripe_modify):
    _live_sub(app, venue["id"], band=5)
    ids = _bulk_staff(app, venue["id"], 26)

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/admin/staff/{ids[0]}/leave")
    assert resp.status_code == 302

    assert stripe_modify[0]["items"] == [{"id": "si_live1", "price": "price_band4", "quantity": 1}]
    assert stripe_modify[0]["proration_behavior"] == "always_invoice"
    assert _current_tier(app, venue["id"]) == 4


def test_subscription_page_offers_band_5_and_poa(app, client, venue, band_prices):
    login_as_pub(client, venue["pub_id"])
    html = client.get(f"/v/{venue['slug']}/billing/subscription").get_data(as_text=True)
    assert "up to 50 staff" in html
    assert "£68.00/month + VAT" in html
    assert "51+ staff: price on application" in html
    assert "info@pubpulse.co.uk" in html


def test_checkout_for_band_5_uses_the_band_5_price(app, client, venue, band_prices, monkeypatch):
    import stripe
    captured = {}
    monkeypatch.setattr(
        stripe.checkout.Session, "create",
        lambda **kw: captured.update(kw) or SimpleNamespace(url="https://checkout.example/x"),
    )
    _bulk_staff(app, venue["id"], 30)
    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": "5"})
    assert resp.status_code == 303
    assert captured["line_items"] == [{"price": "price_band5", "quantity": 1}]


def test_band_6_does_not_exist(app, client, venue, band_prices):
    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/billing/upgrade", data={"band": "6"})
    assert resp.status_code == 400


# --- 50 -> 51 is blocked on every route ---

def test_approving_the_51st_is_refused(app, client, venue, band_prices, stripe_modify):
    _live_sub(app, venue["id"], band=5)
    _bulk_staff(app, venue["id"], 50)
    access_id, membership_id = _pending(app, venue["id"])

    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/approve", follow_redirects=True)

    assert CAP_TEXT in resp.get_data(as_text=True)
    assert _status(app, membership_id)["a"] == "pending_approval"
    assert stripe_modify == []
    with app.app_context():
        assert count_billable_staff(venue["id"]) == 50


def test_approving_the_50th_is_allowed(app, client, venue, band_prices, stripe_modify):
    _bulk_staff(app, venue["id"], 49)
    access_id, membership_id = _pending(app, venue["id"])
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/approve")
    assert _status(app, membership_id)["a"] == "active"


def test_admin_only_accounts_do_not_count_toward_the_cap(app, client, venue, band_prices):
    # 49 billable staff, plus the owner and a manager with no pay record:
    # neither admin counts, so there is still room for one more.
    _bulk_staff(app, venue["id"], 49)
    with app.app_context():
        conn = db_module.get_db()
        pid = conn.execute("INSERT INTO person (name, email) VALUES ('Mgr', 'mgr@example.com')").lastrowid
        mid = conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')", (pid, venue["id"])
        ).lastrowid
        conn.execute(
            "INSERT INTO app_access (venue_membership_id, app_id, permission_level, status) VALUES (?, ?, 'rota_admin', 'active')",
            (mid, db_module.get_app_id(conn, "rotapulse")),
        )
        conn.commit()
        assert count_billable_staff(venue["id"]) == 49
        assert staff_cap_reached(venue["id"]) is False
    access_id, membership_id = _pending(app, venue["id"])
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/admin/staff/{access_id}/approve")
    assert _status(app, membership_id)["a"] == "active"


def test_inviting_at_50_is_refused(app, client, venue):
    _bulk_staff(app, venue["id"], 50)
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/admin/staff/create",
        data={"name": "One Too Many", "email": "toomany@example.com", "invite_method": "email"},
        follow_redirects=True,
    )
    assert CAP_TEXT in resp.get_data(as_text=True)
    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT 1 FROM person WHERE email = 'toomany@example.com'").fetchone() is None


def test_inviting_at_49_is_allowed(app, client, venue):
    _bulk_staff(app, venue["id"], 49)
    login_as_pub(client, venue["pub_id"])
    client.post(
        f"/v/{venue['slug']}/admin/staff/create",
        data={"name": "Fiftieth", "email": "fiftieth@example.com", "invite_method": "email"},
    )
    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT 1 FROM person WHERE email = 'fiftieth@example.com'").fetchone() is not None


def test_reinstating_a_leaver_at_50_is_refused(app, client, venue, band_prices, stripe_modify):
    _live_sub(app, venue["id"], band=5)
    ids = _bulk_staff(app, venue["id"], 51)
    login_as_pub(client, venue["pub_id"])
    client.post(f"/v/{venue['slug']}/admin/staff/{ids[0]}/leave")  # 51 -> 50
    stripe_modify.clear()

    resp = client.post(f"/v/{venue['slug']}/admin/staff/{ids[0]}/reinstate", follow_redirects=True)

    assert CAP_TEXT in resp.get_data(as_text=True)
    assert _status(app, ids[0])["m"] == "left"
    assert stripe_modify == []


def test_reinstating_another_venues_leaver_is_still_a_404(app, client, venue):
    login_as_pub(client, venue["pub_id"])
    resp = client.post(f"/v/{venue['slug']}/admin/staff/999999/reinstate")
    assert resp.status_code == 404


def test_saving_a_pay_record_that_would_make_the_51st_is_refused(app, client, venue):
    """A Hub-added person has access but no pay record; saving their staff
    record creates one and so makes them billable."""
    _bulk_staff(app, venue["id"], 50)
    with app.app_context():
        conn = db_module.get_db()
        pid = conn.execute("INSERT INTO person (name, email) VALUES ('Hubby', 'hubby@example.com')").lastrowid
        mid = conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'active')", (pid, venue["id"])
        ).lastrowid
        conn.execute(
            "INSERT INTO app_access (venue_membership_id, app_id, permission_level, status) VALUES (?, ?, 'staff', 'active')",
            (mid, db_module.get_app_id(conn, "rotapulse")),
        )
        conn.commit()

    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/admin/staff/{mid}/edit", data={"name": "Hubby", "hourly_pay_rate": "12"},
        follow_redirects=True,
    )
    assert CAP_TEXT in resp.get_data(as_text=True)
    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT 1 FROM rota_staff_detail WHERE venue_membership_id = ?", (mid,)).fetchone() is None
        assert count_billable_staff(venue["id"]) == 50


def test_editing_someone_already_counted_at_50_still_saves(app, client, venue):
    ids = _bulk_staff(app, venue["id"], 50)
    login_as_pub(client, venue["pub_id"])
    resp = client.post(
        f"/v/{venue['slug']}/admin/staff/{ids[0]}/edit", data={"name": "Renamed", "hourly_pay_rate": "13"},
        follow_redirects=True,
    )
    assert CAP_TEXT not in resp.get_data(as_text=True)
    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute(
            "SELECT person.name FROM person JOIN venue_membership ON venue_membership.person_id = person.id "
            "WHERE venue_membership.id = ?", (ids[0],)).fetchone()["name"] == "Renamed"


def _former_hub_member(app, venue_id, hub_person_id=777):
    """A Hub person who used to work here: pay record kept, access revoked."""
    with app.app_context():
        conn = db_module.get_db()
        pid = conn.execute(
            "INSERT INTO person (name, email, hub_person_id) VALUES ('Returner', 'returner@example.com', ?)",
            (hub_person_id,),
        ).lastrowid
        mid = conn.execute(
            "INSERT INTO venue_membership (person_id, venue_id, status) VALUES (?, ?, 'left')", (pid, venue_id)
        ).lastrowid
        conn.execute(
            "INSERT INTO app_access (venue_membership_id, app_id, permission_level, status) VALUES (?, ?, 'staff', 'revoked')",
            (mid, db_module.get_app_id(conn, "rotapulse")),
        )
        conn.execute(
            "INSERT INTO rota_staff_detail (venue_membership_id, hourly_pay_rate, availability) VALUES (?, 11, '{}')",
            (mid,),
        )
        conn.commit()
    return mid


def _hub_push(client, venue, hub_person_id=777, status="active"):
    return client.post(
        "/internal/access",
        json={"pub_id": venue["pub_id"], "person_id": hub_person_id, "name": "Returner",
              "email": "returner@example.com", "level": "staff", "status": status},
        headers=HUB_HEADERS,
    )


def test_hub_push_reactivating_the_51st_is_refused(app, client, venue, monkeypatch, band_prices, stripe_modify):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    _live_sub(app, venue["id"], band=5)
    _bulk_staff(app, venue["id"], 50)
    mid = _former_hub_member(app, venue["id"])

    resp = _hub_push(client, venue)

    assert resp.status_code == 409
    assert CAP_TEXT in resp.get_json()["message"]
    assert _status(app, mid)["m"] == "left"
    assert _status(app, mid)["a"] == "revoked"
    assert stripe_modify == []


def test_hub_push_reactivating_under_the_cap_is_allowed(app, client, venue, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    _bulk_staff(app, venue["id"], 49)
    mid = _former_hub_member(app, venue["id"])
    assert _hub_push(client, venue).status_code == 200
    assert _status(app, mid)["a"] == "active"
    with app.app_context():
        assert count_billable_staff(venue["id"]) == 50


def test_hub_push_revoking_at_the_cap_is_never_refused(app, client, venue, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    _bulk_staff(app, venue["id"], 50)
    _former_hub_member(app, venue["id"])
    assert _hub_push(client, venue, status="inactive").status_code == 200


def test_hub_push_of_a_brand_new_person_has_no_pay_record_so_is_not_counted(app, client, venue, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    _bulk_staff(app, venue["id"], 50)
    resp = client.post(
        "/internal/access",
        json={"pub_id": venue["pub_id"], "person_id": 4242, "name": "Fresh", "email": "fresh@example.com",
              "level": "staff", "status": "active"},
        headers=HUB_HEADERS,
    )
    assert resp.status_code == 200
    with app.app_context():
        assert count_billable_staff(venue["id"]) == 50
