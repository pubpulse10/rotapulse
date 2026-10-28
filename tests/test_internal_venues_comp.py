"""
PricePulse's "Give free access to all apps" button calls /internal/venues/comp
on every sibling. Granting must create the venue, with its owner holding
app_admin + rota_admin, when the pub has none, so the owner never has to go
through the setup page (which talks about a card).
"""

from app import config
from app import db as db_module


def _headers():
    return {"Authorization": "Bearer test-secret"}


def _subs(app, pub_id):
    with app.app_context():
        return db_module.get_db().execute(
            """SELECT venue.id, venue.name, rota_subscription.plan,
                      rota_subscription.subscription_status
               FROM venue JOIN rota_subscription ON rota_subscription.venue_id = venue.id
               WHERE venue.pub_id = ?""",
            (pub_id,),
        ).fetchall()


def test_comp_requires_bearer_auth(client):
    resp = client.post("/internal/venues/comp", json={"pub_id": 500, "grant": True})
    assert resp.status_code == 401


def test_grant_creates_missing_venue_with_owner_and_comps_it(app, client, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    resp = client.post(
        "/internal/venues/comp",
        json={"pub_id": 500, "grant": True, "venue_name": "Rake and Rascal",
              "owner_name": "Sam Rake", "owner_email": "sam@example.com"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.get_json()["created"] is True
    subs = _subs(app, 500)
    assert [(r["name"], r["plan"], r["subscription_status"]) for r in subs] == [
        ("Rake and Rascal", "active", "comp")
    ]
    with app.app_context():
        levels = {r["permission_level"] for r in db_module.get_db().execute(
            """SELECT app_access.permission_level FROM app_access
               JOIN venue_membership ON venue_membership.id = app_access.venue_membership_id
               JOIN person ON person.id = venue_membership.person_id
               WHERE person.pub_id = 500 AND person.email = 'sam@example.com'
                 AND app_access.status = 'active'"""
        ).fetchall()}
    assert levels == {"app_admin", "rota_admin"}


def test_grant_comps_existing_venue_without_creating(app, client, venue, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    resp = client.post(
        "/internal/venues/comp",
        json={"pub_id": venue["pub_id"], "grant": True, "venue_name": "Ignored"},
        headers=_headers(),
    )
    assert resp.get_json()["created"] is False
    subs = _subs(app, venue["pub_id"])
    assert len(subs) == 1 and subs[0]["subscription_status"] == "comp"


def test_revoke_removes_comp_and_never_creates(app, client, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "test-secret")
    client.post("/internal/venues/comp", json={"pub_id": 500, "grant": True}, headers=_headers())
    client.post("/internal/venues/comp", json={"pub_id": 500, "grant": False}, headers=_headers())
    assert [(r["plan"], r["subscription_status"]) for r in _subs(app, 500)] == [("inactive", None)]

    resp = client.post(
        "/internal/venues/comp", json={"pub_id": 501, "grant": False}, headers=_headers()
    )
    assert resp.get_json() == {"ok": True, "venues": [], "created": False}
    assert _subs(app, 501) == []
