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
    assert resp.get_json() == {"ok": True, "deleted": {}}
