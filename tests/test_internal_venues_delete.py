from app import db as db_module
from tests.conftest import create_active_staff


def _headers():
    return {"Authorization": "Bearer test-secret"}


def test_venues_delete_requires_bearer_auth(client, venue):
    resp = client.post("/internal/venues/delete", json={"pub_id": venue["pub_id"]})
    assert resp.status_code == 401


def test_venues_delete_removes_venue_and_all_children(app, client, venue, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    create_active_staff(app, venue["id"], name="Staff Person")

    resp = client.post(
        "/internal/venues/delete", json={"pub_id": venue["pub_id"]}, headers=_headers()
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT * FROM venue WHERE id = ?", (venue["id"],)).fetchone() is None
        assert conn.execute(
            "SELECT * FROM venue_settings WHERE venue_id = ?", (venue["id"],)
        ).fetchone() is None
        assert conn.execute(
            "SELECT * FROM rota_subscription WHERE venue_id = ?", (venue["id"],)
        ).fetchone() is None
        assert conn.execute(
            "SELECT * FROM venue_membership WHERE venue_id = ?", (venue["id"],)
        ).fetchall() == []
        assert conn.execute(
            "SELECT * FROM app_access WHERE venue_membership_id = ?", (venue["owner_membership_id"],)
        ).fetchall() == []
        assert conn.execute(
            "SELECT * FROM venue_role WHERE venue_id = ?", (venue["id"],)
        ).fetchall() == []


def test_venues_delete_leaves_other_pub_ids_untouched(app, client, venue, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    with app.app_context():
        conn = db_module.get_db()
        cur = conn.execute(
            "INSERT INTO venue (pub_id, name, slug, latitude, longitude) VALUES (?, ?, ?, ?, ?)",
            (999, "Other Venue", "othervenue", 52.6, 1.3),
        )
        other_venue_id = cur.lastrowid
        conn.commit()

    resp = client.post(
        "/internal/venues/delete", json={"pub_id": venue["pub_id"]}, headers=_headers()
    )
    assert resp.status_code == 200

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT * FROM venue WHERE id = ?", (venue["id"],)).fetchone() is None
        assert conn.execute(
            "SELECT * FROM venue WHERE id = ?", (other_venue_id,)
        ).fetchone() is not None


def test_venues_delete_is_idempotent_for_unknown_pub_id(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    resp = client.post(
        "/internal/venues/delete", json={"pub_id": 999999}, headers=_headers()
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "deleted": {}, "cancelled": []}


# --- Stripe cancel on delete ----------------------------------------------
# Deleting a family account used to cancel PricePulse's subscription only.
# RotaPulse's rows went, but its Stripe subscription kept billing, with the
# rota_subscription row that held the id already deleted — so nothing local
# showed the charge or could cancel it. Stripe is monkeypatched throughout;
# these never touch the network.


def _authorise_and_configure_stripe(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_fake")


def _capture_cancels(monkeypatch):
    import stripe

    cancelled = []
    monkeypatch.setattr(stripe.Subscription, "cancel", lambda sub_id: cancelled.append(sub_id))
    return cancelled


def _set_subscription_id(app, venue_id, sub_id):
    with app.app_context():
        conn = db_module.get_db()
        conn.execute(
            "UPDATE rota_subscription SET stripe_subscription_id = ? WHERE venue_id = ?",
            (sub_id, venue_id),
        )
        conn.commit()


def test_venues_delete_cancels_the_live_stripe_subscription(app, client, venue, monkeypatch):
    _authorise_and_configure_stripe(monkeypatch)
    cancelled = _capture_cancels(monkeypatch)
    _set_subscription_id(app, venue["id"], "sub_live1")

    resp = client.post(
        "/internal/venues/delete", json={"pub_id": venue["pub_id"]}, headers=_headers()
    )
    assert resp.status_code == 200
    assert cancelled == ["sub_live1"]
    assert resp.get_json()["cancelled"] == ["sub_live1"]

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT * FROM venue WHERE id = ?", (venue["id"],)).fetchone() is None


def test_venues_delete_skips_a_venue_with_no_subscription_id(app, client, venue, monkeypatch):
    """A venue that never subscribed has nothing to cancel — Stripe must not
    be called at all, and the delete still happens."""
    _authorise_and_configure_stripe(monkeypatch)
    cancelled = _capture_cancels(monkeypatch)

    resp = client.post(
        "/internal/venues/delete", json={"pub_id": venue["pub_id"]}, headers=_headers()
    )
    assert resp.status_code == 200
    assert cancelled == []
    assert resp.get_json()["cancelled"] == []

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT * FROM venue WHERE id = ?", (venue["id"],)).fetchone() is None


def test_venues_delete_proceeds_when_stripe_cancel_fails(app, client, venue, monkeypatch):
    """Stripe being down (or the subscription already gone) must not leave the
    venue's data behind — PricePulse has already deleted the account by the
    time it calls us, so a refusal here would strand the rows."""
    import stripe

    _authorise_and_configure_stripe(monkeypatch)
    _set_subscription_id(app, venue["id"], "sub_live1")

    def boom(_sub_id):
        raise stripe.error.APIConnectionError("Stripe is down")

    monkeypatch.setattr(stripe.Subscription, "cancel", boom)

    resp = client.post(
        "/internal/venues/delete", json={"pub_id": venue["pub_id"]}, headers=_headers()
    )
    assert resp.status_code == 200
    # Not reported as cancelled, because it wasn't.
    assert resp.get_json()["cancelled"] == []

    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT * FROM venue WHERE id = ?", (venue["id"],)).fetchone() is None
        assert conn.execute(
            "SELECT * FROM rota_subscription WHERE venue_id = ?", (venue["id"],)
        ).fetchone() is None
